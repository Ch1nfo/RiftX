"""Persistent process supervision for local RiftX executions."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.ports import ExecutionRepository
from riftx.domain import Execution, ExecutionStatus, ExecutorType
from riftx.domain.base import new_id, utc_now
from riftx.executors import (
    DirectProcessExecutor,
    ProcessContainmentError,
    ProcessExecutionRequest,
    ProcessHandle,
    ProcessResult,
    ProcessStartError,
    ProcessTreeTerminationError,
    ShellExecutionRequest,
    ShellExecutor,
    UnconfirmedProcessStartError,
    merge_environment,
)
from riftx.executors.process import (
    _kill_windows_process_tree,
    _posix_process_group_exists,
    _terminate_posix_process_group,
)

from .models import ExecutionLaunchRequest, ExecutionOutput, OutputSlice
from .paths import RunnerPaths
from .process_inspector import ProcessInspector, _pid_exists
from .protocols import EffectGuard


@dataclass(slots=True)
class _ManagedExecution:
    handle: ProcessHandle
    task: asyncio.Task[None] | None = None
    cancel_task: asyncio.Task[ProcessResult] | None = None
    start_cleanup_unconfirmed: bool = False


class ProcessTerminationError(RuntimeError):
    """Raised when a detached process cannot be confirmed terminated."""


_PHYSICAL_STOP_PROOF_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
}


class ProcessSupervisor:
    def __init__(
        self,
        repository: ExecutionRepository,
        paths: RunnerPaths,
        *,
        process_executor: DirectProcessExecutor | None = None,
        shell_executor: ShellExecutor | None = None,
        inspector: ProcessInspector | None = None,
        termination_grace_seconds: float = 2.0,
        on_completed: Callable[[Execution], Awaitable[None]] | None = None,
    ) -> None:
        self._repository = repository
        self._paths = paths
        self._process_executor = process_executor or DirectProcessExecutor(defer_activation=True)
        self._shell_executor = shell_executor or ShellExecutor(self._process_executor)
        self._inspector = inspector or ProcessInspector()
        self._termination_grace_seconds = termination_grace_seconds
        self._on_completed = on_completed
        self._managed: dict[str, _ManagedExecution] = {}
        self._detached_containment_stop_tasks: dict[str, asyncio.Task[None]] = {}
        self._detached_containment_cleanup_tasks: dict[str, asyncio.Task[None]] = {}
        self._outcome_locks: dict[str, asyncio.Lock] = {}

    async def start(
        self,
        request: ExecutionLaunchRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> Execution:
        requested_execution_id = request.execution_id
        execution_id = request.execution_id or new_id()
        self._paths.ensure_run_layout(request.run_id)
        output_paths = self._paths.execution(request.run_id, execution_id)
        execution = Execution(
            id=execution_id,
            execution_key=request.execution_key,
            launch_fingerprint=request.launch_fingerprint,
            run_id=request.run_id,
            session_id=request.session_id,
            tool_call_id=request.tool_call_id,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
            owner=request.runner_principal,
            executor_type=request.executor_type,
            argv=request.argv,
            command_text=request.command_text,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            platform_system=platform.system().lower() or os.name,
            platform_release=platform.release(),
            platform_architecture=platform.machine() or "unknown",
            stdout_path=str(output_paths.stdout),
            stderr_path=str(output_paths.stderr),
            status=(
                ExecutionStatus.QUEUED
                if request.session_id is not None
                else ExecutionStatus.CREATED
            ),
        )

        effective_environment = merge_environment(request.env, mode=request.environment_mode)
        execution.executable_path = _resolve_executable(
            request.argv[0] if request.argv else request.shell_path,
            effective_environment,
        )
        # STARTING is the durable admission record.  It must exist before the
        # last Run-status check and before the process backend can spawn, so a
        # concurrent Run stop either blocks here or drains this exact record.
        execution.transition_to(ExecutionStatus.STARTING)
        execution, created = await self._repository.create_if_absent(execution)
        if not created:
            if requested_execution_id is not None and execution.id != requested_execution_id:
                raise ApplicationConflictError(
                    "execution_idempotency_conflict",
                    f"Execution key {request.execution_key!r} is already bound to "
                    f"execution ID {execution.id!r}",
                )
            return execution

        try:
            if effect_guard is not None:
                await effect_guard()
        except BaseException:
            await self._cancel_unstarted(execution)
            raise
        handle: ProcessHandle | None = None
        try:
            handle = await self._start_handle(request, execution, effective_environment)
            execution.argv = handle.request.argv
            execution.executable_path = _resolve_executable(
                handle.request.argv[0], effective_environment
            )
            execution.pid = handle.pid
            execution.process_group_id = handle.process_group_id
            execution.containment_id = handle.containment_identifier
            execution.process_created_at = handle.started_at
            execution, identity_saved = await self._repository.save_if_status(
                execution,
                expected={ExecutionStatus.STARTING},
            )
            if not identity_saved:
                await self._abort_spawned_start(execution, handle)
                return await self.get(execution.id)

            if effect_guard is not None:
                await effect_guard()

            # The launcher remains trusted/gated until the durable PID, PGID
            # and containment identity are stored and the final effect guard
            # passes.  Only this call can exec target code.
            await handle.activate()

            execution.transition_to(ExecutionStatus.RUNNING, at=handle.started_at)
            execution, started = await self._repository.save_if_status(
                execution,
                expected={ExecutionStatus.STARTING},
            )
            if not started:
                result = await handle.cancel(
                    termination_grace_seconds=self._termination_grace_seconds,
                    cleanup_containment=False,
                )
                execution, _ = await self._persist_confirmed_outcome(
                    execution,
                    target=ExecutionStatus.CANCELLED,
                    exit_code=result.exit_code,
                )
                await handle.cleanup_confirmed_containment()
                return execution
        except UnconfirmedProcessStartError as exc:
            return await self._retain_unconfirmed_process_start(
                execution,
                exc.handle,
            )
        except ProcessStartError:
            if handle is not None:
                try:
                    safely_aborted = await handle.abort_gated_start(
                        confirmation_seconds=max(self._termination_grace_seconds, 0.5),
                        cleanup_containment=False,
                    )
                except BaseException:
                    safely_aborted = False
                if not safely_aborted:
                    await self._abort_spawned_start(execution, handle)
                    return await self.get(execution.id)
                current = await self.get(execution.id)
                current, _ = await self._persist_confirmed_outcome(
                    current,
                    target=ExecutionStatus.CANCELLED,
                )
                if current.physical_stop_confirmed_at is None:
                    raise ProcessTerminationError(
                        f"Execution {execution.id!r} was safely aborted, but durable "
                        "physical-stop proof could not be persisted"
                    ) from None
                await handle.cleanup_confirmed_containment()
                return current
            current = await self.get(execution.id)
            if current.status is ExecutionStatus.STARTING:
                current.transition_to(ExecutionStatus.FAILED)
                current, _ = await self._repository.save_if_status(
                    current,
                    expected={ExecutionStatus.STARTING},
                )
            return current
        except BaseException:
            if handle is None:
                await self._cancel_unstarted(execution)
            else:
                await self._abort_spawned_start(execution, handle)
            raise

        managed = _ManagedExecution(handle=handle)
        self._managed[execution.id] = managed
        task = asyncio.create_task(
            self._monitor(execution.id, managed),
            name=f"riftx-execution-{execution.id}",
        )
        task.add_done_callback(_observe_task_exception)
        managed.task = task
        return execution

    async def _retain_unconfirmed_process_start(
        self,
        execution: Execution,
        handle: ProcessHandle,
    ) -> Execution:
        """Keep a spawned-but-unconfirmed launcher cancellable and fail closed."""

        execution.argv = handle.request.argv
        execution.pid = handle.pid
        execution.process_group_id = handle.process_group_id
        execution.containment_id = handle.containment_identifier
        execution.process_created_at = handle.started_at
        # Register before persistence: even a concurrent status CAS must not
        # make this exact native handle unreachable to an in-process retry.
        self._managed[execution.id] = _ManagedExecution(
            handle=handle,
            start_cleanup_unconfirmed=True,
        )
        current = await self.get(execution.id)
        if current.status is ExecutionStatus.STARTING:
            current.argv = execution.argv
            current.executable_path = execution.executable_path
            current.pid = execution.pid
            current.process_group_id = execution.process_group_id
            current.containment_id = execution.containment_id
            current.process_created_at = execution.process_created_at
            current, _ = await self._repository.save_if_status(
                current,
                expected={ExecutionStatus.STARTING},
            )
        return current

    async def _cancel_unstarted(self, execution: Execution) -> Execution:
        current = await self._repository.get(execution.id)
        if current is None:
            return current or execution
        current, _ = await self._persist_confirmed_outcome(
            current,
            target=ExecutionStatus.CANCELLED,
        )
        return current

    async def _abort_spawned_start(
        self,
        execution: Execution,
        handle: ProcessHandle,
    ) -> None:
        try:
            if await handle.abort_gated_start(
                confirmation_seconds=max(self._termination_grace_seconds, 0.5),
                cleanup_containment=False,
            ):
                await self._cancel_unstarted(execution)
                await handle.cleanup_confirmed_containment()
                return
        except BaseException:
            pass
        try:
            await asyncio.shield(
                handle.cancel(
                    termination_grace_seconds=self._termination_grace_seconds,
                    cleanup_containment=False,
                )
            )
        except BaseException:
            # Termination was not confirmed.  Preserve the process identity in
            # the non-terminal STARTING record so a later safety retry can
            # inspect and kill it; never manufacture a CANCELLED acknowledgement.
            current = await self._repository.get(execution.id)
            if current is not None and current.status is ExecutionStatus.STARTING:
                current.argv = execution.argv
                current.executable_path = execution.executable_path
                current.pid = handle.pid
                current.process_group_id = handle.process_group_id
                current.containment_id = handle.containment_identifier
                current.process_created_at = handle.started_at
                await self._repository.save_if_status(
                    current,
                    expected={ExecutionStatus.STARTING},
                )
            return
        await self._cancel_unstarted(execution)
        await handle.cleanup_confirmed_containment()

    async def get(self, execution_id: str) -> Execution:
        execution = await self._repository.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution

    async def wait(self, execution_id: str) -> Execution:
        managed = self._managed.get(execution_id)
        if managed is not None and managed.task is not None:
            await asyncio.shield(_managed_task(managed))
        return await self.get(execution_id)

    async def cancel(self, execution_id: str) -> Execution:
        managed = self._managed.get(execution_id)
        if managed is not None:
            monitor_task = managed.task
            if monitor_task is None or not monitor_task.done():
                cancel_task = managed.cancel_task
                if cancel_task is None:
                    cancel_task = asyncio.create_task(
                        self._cancel_managed_execution(managed),
                        name=f"riftx-cancel-{execution_id}",
                    )
                    managed.cancel_task = cancel_task
                try:
                    # The physical cleanup must survive cancellation of an API
                    # caller.  The monitor shares this task and only persists
                    # CANCELLED after it completes successfully.
                    cancel_result = await asyncio.shield(cancel_task)
                except asyncio.CancelledError:
                    if cancel_task.cancelled() and managed.cancel_task is cancel_task:
                        managed.cancel_task = None
                    raise
                except BaseException:
                    if managed.cancel_task is cancel_task:
                        managed.cancel_task = None
                    raise
                if monitor_task is not None:
                    await asyncio.shield(monitor_task)
                execution = await self.get(execution_id)
                execution, saved = await self._persist_confirmed_outcome(
                    execution,
                    target=ExecutionStatus.CANCELLED,
                    exit_code=cancel_result.exit_code,
                )
                if saved and self._on_completed is not None:
                    await self._on_completed(execution)
                await managed.handle.cleanup_confirmed_containment()
                if monitor_task is None:
                    self._managed.pop(execution_id, None)
                return execution

        execution = await self.get(execution_id)
        if execution.physical_stop_confirmed_at is not None:
            await self._cleanup_confirmed_detached_containment(execution)
            return execution
        if execution.status in {ExecutionStatus.CREATED, ExecutionStatus.QUEUED}:
            execution, saved = await self._persist_confirmed_outcome(
                execution,
                target=ExecutionStatus.CANCELLED,
            )
            if saved:
                return execution
            return await self.cancel(execution_id)
        if execution.status not in {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.FAILED,
            ExecutionStatus.LOST,
        }:
            if _is_explicit_pre_spawn_cancellation(execution):
                execution, _ = await self._persist_confirmed_outcome(
                    execution,
                    target=ExecutionStatus.CANCELLED,
                )
                return execution
            if (
                execution.pid is not None
                or execution.process_group_id is not None
                or execution.containment_id is not None
            ):
                # Terminal status from an older/failed owner is not sufficient
                # physical-stop evidence.  In particular, retrying a historic
                # CANCELLED tombstone must still clean a live recorded group.
                await self._ensure_detached_process_stopped(execution)
                execution = await self._persist_confirmed_cancellation(execution)
                await self._cleanup_confirmed_detached_containment(execution)
                return execution
            raise ProcessTerminationError(
                f"Cannot confirm execution {execution.id!r} stopped because "
                "it has no durable physical-stop proof or process identity"
            )
        if _is_pre_spawn_failure(execution):
            # ProcessStartError is persisted as FAILED before any process
            # identity or start timestamp exists.  This is the only FAILED
            # shape whose physical absence is proven without inspection.
            execution, saved = await self._persist_confirmed_outcome(
                execution,
                target=ExecutionStatus.CANCELLED,
            )
            if saved:
                return execution
            return await self.cancel(execution_id)
        if (
            execution.pid is None
            and execution.process_group_id is None
            and execution.containment_id is None
        ):
            # STARTING can be persisted before the process backend returns its
            # identity. A concurrent stop must not interpret that incomplete
            # record as proof that no process exists: the backend may still
            # spawn after cancellation returns. LOST and FAILED records that
            # show evidence of having started are likewise unprovable without
            # identity. Keep the execution non-terminal so the Run remains
            # fenced and a later retry can confirm the actual process state.
            raise ProcessTerminationError(
                f"Cannot confirm execution {execution.id!r} stopped because "
                "its process identity has not been persisted"
            )
        await self._ensure_detached_process_stopped(execution)
        # The matching process group was terminated and verified, or its
        # physical absence was proven. Both are affirmative stop evidence,
        # including reconciliation from LOST or a FAILED result.
        execution = await self._persist_confirmed_cancellation(execution)
        await self._cleanup_confirmed_detached_containment(execution)
        return execution

    async def _cancel_managed_execution(
        self,
        managed: _ManagedExecution,
    ) -> ProcessResult:
        if managed.start_cleanup_unconfirmed:
            safely_aborted = await managed.handle.abort_gated_start(
                confirmation_seconds=max(self._termination_grace_seconds, 0.5),
                cleanup_containment=False,
            )
            if safely_aborted:
                return ProcessResult(
                    status=ExecutionStatus.CANCELLED,
                    exit_code=managed.handle.process.returncode,
                )
        return await managed.handle.cancel(
            termination_grace_seconds=self._termination_grace_seconds,
            cleanup_containment=False,
        )

    async def _persist_confirmed_cancellation(self, execution: Execution) -> Execution:
        # Reconciliation can advance RUNNING to LOST while kill/confirmation
        # is in flight.  Physical stop evidence remains valid, so converge any
        # still-cancellable durable status with CAS instead of returning a
        # stale LOST/FAILED acknowledgement.
        execution, _ = await self._persist_confirmed_outcome(
            execution,
            target=ExecutionStatus.CANCELLED,
        )
        return execution

    async def _persist_confirmed_outcome(
        self,
        execution: Execution,
        *,
        target: ExecutionStatus,
        exit_code: int | None = None,
    ) -> tuple[Execution, bool]:
        lock = self._outcome_locks.setdefault(execution.id, asyncio.Lock())
        async with lock:
            current = await self._repository.get(execution.id)
            if current is None:
                raise EntityNotFoundError("Execution", execution.id)
            return await self._persist_confirmed_outcome_locked(
                current,
                target=target,
                exit_code=exit_code,
            )

    async def _persist_confirmed_outcome_locked(
        self,
        execution: Execution,
        *,
        target: ExecutionStatus,
        exit_code: int | None,
    ) -> tuple[Execution, bool]:
        confirmation_time = execution.physical_stop_confirmed_at or utc_now()
        for _ in range(8):
            if execution.physical_stop_confirmed_at is not None:
                return execution, False
            expected = execution.status
            candidate = execution.model_copy(deep=True)
            if candidate.can_transition_to(target):
                candidate.transition_to(target, exit_code=exit_code)
            elif candidate.status not in _PHYSICAL_STOP_PROOF_STATUSES:
                if candidate.can_transition_to(ExecutionStatus.CANCELLED):
                    candidate.transition_to(
                        ExecutionStatus.CANCELLED,
                        exit_code=exit_code,
                    )
                else:
                    raise ProcessTerminationError(
                        f"Execution {execution.id!r} was physically stopped, but status "
                        f"{candidate.status.value!r} cannot carry durable stop proof"
                    )
            candidate.physical_stop_confirmed_at = confirmation_time
            execution, saved = await self._repository.save_if_status(
                candidate,
                expected={expected},
            )
            if saved:
                return execution, True
        raise ProcessTerminationError(
            f"Execution {execution.id!r} was physically stopped but its durable "
            "physical-stop proof could not be persisted"
        )

    async def _ensure_detached_process_stopped(self, execution: Execution) -> None:
        if execution.containment_id is not None:
            await self._ensure_detached_containment_stopped(execution)
            return
        if _supports_posix_process_groups():
            await self._best_effort_detached_posix_cleanup(execution)
            # PGID disappearance cannot account for setsid()/double-fork
            # descendants.  It is never affirmative physical-stop evidence.
            raise ProcessTerminationError(
                f"Cannot confirm detached execution {execution.id!r} stopped: "
                "no durable kernel containment identity was recorded"
            )

        leader_matches = await self._matches_detached_process(
            execution,
            phase="before termination",
        )
        if leader_matches:
            await self._terminate_and_confirm_detached_process(execution)
        elif _supports_posix_process_groups() and execution.process_group_id is not None:
            group_alive = await self._detached_process_group_exists(
                execution,
                phase="before termination",
            )
            if group_alive:
                if await self._detached_leader_exists(
                    execution,
                    phase="before termination",
                ):
                    # A false identity match is not proof of physical absence
                    # while both the recorded leader PID and PGID are alive.
                    # This may be a shell shape the inspector cannot safely
                    # recognize or a foreign/reused identity.  Do not kill it,
                    # and do not acknowledge it as stopped.
                    raise ProcessTerminationError(
                        f"Cannot confirm detached execution {execution.id!r} stopped: "
                        f"recorded process group {execution.process_group_id!r} is still "
                        "alive but its leader identity did not match"
                    )
                # A live process group keeps its numeric PGID reserved even
                # after the leader exits.  With the recorded leader PID
                # confirmed absent, this is the original orphaned group and
                # can be safely terminated and verified.
                await self._terminate_and_confirm_detached_process(execution)
        elif _supports_posix_process_groups():
            # Without a recorded PGID, leader absence cannot prove that
            # same-group descendants are gone.  Legacy/incomplete records
            # therefore remain fenced instead of manufacturing stop evidence.
            raise ProcessTerminationError(
                f"Cannot confirm detached execution {execution.id!r} stopped because "
                "its POSIX process group identity is missing"
            )
        # Windows does not expose enough detached tree identity through the
        # portable inspector to prove descendant absence. taskkill above is
        # best-effort cleanup only; a future durable Job Object backend may make
        # this branch auditable. Until then it must always fail closed.
        raise ProcessTerminationError(
            f"Cannot confirm detached execution {execution.id!r} stopped: "
            "reliable kernel-owned process-tree identity and absence evidence is unavailable"
        )

    async def _ensure_detached_containment_stopped(self, execution: Execution) -> None:
        task = self._detached_containment_stop_tasks.get(execution.id)
        if task is None or task.cancelled() or (task.done() and task.exception() is not None):
            task = asyncio.create_task(
                self._stop_detached_containment(execution),
                name=f"riftx-detached-containment-stop-{execution.id}",
            )
            task.add_done_callback(_observe_task_exception)
            self._detached_containment_stop_tasks[execution.id] = task
        # The stop belongs to the supervisor rather than this API request.
        # Caller disconnect/cancellation must not interrupt cgroup.kill,
        # populated=0 confirmation, or cleanup of the verified-empty leaf.
        await asyncio.shield(task)

    async def _stop_detached_containment(self, execution: Execution) -> None:
        containment = self._resolve_detached_containment(execution)
        try:
            if not containment.boundary_exists():
                # A newly resolved object did not witness the leaf becoming
                # empty.  The Runner may now be in a different mount/cgroup
                # namespace or the delegated root may have been replaced, so
                # pathname absence is not physical-stop evidence.
                raise ProcessTerminationError(
                    f"Cannot confirm detached execution {execution.id!r} stopped: "
                    f"containment {execution.containment_id!r} is missing or its "
                    "kernel identity changed"
                )
            await containment.terminate(grace_seconds=self._termination_grace_seconds)
        except (OSError, ProcessContainmentError) as exc:
            raise ProcessTerminationError(
                f"Failed to terminate or verify containment "
                f"{execution.containment_id!r} for execution {execution.id!r}"
            ) from exc

    async def _cleanup_confirmed_detached_containment(self, execution: Execution) -> None:
        if execution.physical_stop_confirmed_at is None or execution.containment_id is None:
            return
        task = self._detached_containment_cleanup_tasks.get(execution.id)
        if task is None or task.cancelled() or (task.done() and task.exception() is not None):
            task = asyncio.create_task(
                self._cleanup_detached_containment(execution),
                name=f"riftx-detached-containment-cleanup-{execution.id}",
            )
            task.add_done_callback(_observe_task_exception)
            self._detached_containment_cleanup_tasks[execution.id] = task
        await asyncio.shield(task)

    async def _cleanup_detached_containment(self, execution: Execution) -> None:
        containment = self._resolve_detached_containment(execution)
        try:
            if not containment.boundary_exists():
                # Durable proof was written before cleanup, so disappearance
                # of the exact leaf is now an idempotent success condition.
                return
            await containment.cleanup()
        except (OSError, ProcessContainmentError) as exc:
            raise ProcessTerminationError(
                f"Execution {execution.id!r} has durable stop proof, but containment "
                f"{execution.containment_id!r} could not be cleaned up"
            ) from exc

    def _resolve_detached_containment(self, execution: Execution):
        manager = getattr(self._process_executor, "containment_manager", None)
        resolver = getattr(manager, "containment_for", None)
        if resolver is None:
            raise ProcessTerminationError(
                f"Cannot confirm detached execution {execution.id!r} stopped: "
                f"recorded containment {execution.containment_id!r} is unavailable"
            )
        try:
            containment = resolver(execution.execution_key)
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not resolve containment for detached execution {execution.id!r}"
            ) from exc
        try:
            boundary_exists = containment.boundary_exists()
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not inspect containment for detached execution {execution.id!r}"
            ) from exc
        if not boundary_exists:
            # Do not materialize a path-derived identifier after the kernel
            # boundary disappeared. The stop/recovery caller will preserve the
            # active durable status and fail closed on this explicit absence.
            return containment
        try:
            current_identifier = containment.identifier
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not identify containment for detached execution {execution.id!r}"
            ) from exc
        if current_identifier != execution.containment_id:
            raise ProcessTerminationError(
                f"Cannot confirm detached execution {execution.id!r} stopped: recorded "
                "containment belongs to a different delegated root or execution key"
            )
        return containment

    async def _detached_containment_is_populated(self, execution: Execution) -> bool:
        containment = self._resolve_detached_containment(execution)
        if not containment.boundary_exists():
            raise ProcessTerminationError(
                f"Could not inspect containment {execution.containment_id!r} for "
                f"execution {execution.id!r}: boundary is missing or its kernel "
                "identity changed"
            )
        try:
            return await containment.is_populated()
        except (OSError, ProcessContainmentError) as exc:
            raise ProcessTerminationError(
                f"Could not inspect containment {execution.containment_id!r} for "
                f"execution {execution.id!r}"
            ) from exc

    async def _best_effort_detached_posix_cleanup(self, execution: Execution) -> None:
        leader_matches = await self._matches_detached_process(
            execution,
            phase="before best-effort termination",
        )
        if leader_matches:
            await self._terminate_and_confirm_detached_process(execution)
            return
        if execution.process_group_id is None:
            raise ProcessTerminationError(
                f"Cannot confirm detached execution {execution.id!r} stopped because its "
                "process group identity is missing and no durable kernel containment "
                "identity was recorded"
            )
        group_alive = await self._detached_process_group_exists(
            execution,
            phase="before best-effort termination",
        )
        if not group_alive:
            return
        if await self._detached_leader_exists(
            execution,
            phase="before best-effort termination",
        ):
            raise ProcessTerminationError(
                f"Cannot safely clean detached execution {execution.id!r}: process group "
                f"{execution.process_group_id!r} is still alive but leader identity did not match"
            )
        await self._terminate_and_confirm_detached_process(execution)

    async def _matches_detached_process(self, execution: Execution, *, phase: str) -> bool:
        try:
            return await self._inspector.matches(execution)
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not verify detached execution {execution.id!r} {phase}"
            ) from exc

    async def _terminate_and_confirm_detached_process(
        self,
        execution: Execution,
    ) -> None:
        process_group_id = execution.process_group_id or execution.pid
        try:
            await _terminate_detached_process(
                process_group_id,
                grace_seconds=self._termination_grace_seconds,
            )
        except Exception as exc:
            raise ProcessTerminationError(
                f"Failed to terminate detached execution {execution.id!r} "
                f"using process group {process_group_id!r}"
            ) from exc
        await self._confirm_detached_process_stopped(execution)

    async def _detached_leader_exists(
        self,
        execution: Execution,
        *,
        phase: str,
    ) -> bool:
        process_id = execution.pid or execution.process_group_id
        if process_id is None:
            return False
        try:
            return await asyncio.to_thread(_pid_exists, process_id)
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not verify detached execution {execution.id!r} leader {phase}"
            ) from exc

    async def _confirm_detached_process_stopped(self, execution: Execution) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self._termination_grace_seconds, 0.5)
        while True:
            group_alive = await self._detached_process_group_exists(
                execution,
                phase="after termination",
            )
            leader_matches = await self._matches_detached_process(
                execution,
                phase="after termination",
            )
            if not group_alive and not leader_matches:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ProcessTerminationError(
                    f"Detached execution {execution.id!r} still has a matching process "
                    f"or live process group {execution.process_group_id!r} after termination"
                )
            await asyncio.sleep(min(0.05, remaining))

    async def _detached_process_group_exists(
        self,
        execution: Execution,
        *,
        phase: str,
    ) -> bool:
        if not _supports_posix_process_groups() or execution.process_group_id is None:
            return False
        try:
            return await asyncio.to_thread(
                _posix_process_group_exists,
                execution.process_group_id,
            )
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not verify detached execution {execution.id!r} process group {phase}"
            ) from exc

    async def read_output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ExecutionOutput:
        if max_bytes < 1 or max_bytes > 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 1048576")
        execution = await self.get(execution_id)
        stdout, stderr = await asyncio.gather(
            asyncio.to_thread(
                _read_output_slice,
                Path(execution.stdout_path),
                stdout_cursor,
                max_bytes,
            ),
            asyncio.to_thread(
                _read_output_slice,
                Path(execution.stderr_path),
                stderr_cursor,
                max_bytes,
            ),
        )
        return ExecutionOutput(stdout=stdout, stderr=stderr)

    async def recover(self) -> list[Execution]:
        recovered: list[Execution] = []
        for execution in await self._repository.list_active():
            if execution.executor_type is ExecutorType.PTY or execution.status not in {
                ExecutionStatus.STARTING,
                ExecutionStatus.RUNNING,
            }:
                continue
            if execution.containment_id is not None:
                try:
                    still_running = await self._detached_containment_is_populated(execution)
                except ProcessTerminationError:
                    # Unknown containment state must remain fenced as active;
                    # LOST would be an unsupported absence claim.
                    recovered.append(execution)
                    continue
            else:
                still_running = await self._inspector.matches(execution)
            if not still_running:
                expected = execution.status
                execution.transition_to(ExecutionStatus.LOST)
                execution, _ = await self._repository.save_if_status(
                    execution,
                    expected={expected},
                )
            recovered.append(execution)
        return recovered

    async def reconcile(self, execution_id: str) -> Execution:
        """Refresh a recovered execution that is no longer a child of this process."""

        execution = await self.get(execution_id)
        if execution.status not in {ExecutionStatus.STARTING, ExecutionStatus.RUNNING}:
            return execution
        if execution.id in self._managed:
            return execution
        if execution.containment_id is not None:
            try:
                if await self._detached_containment_is_populated(execution):
                    return execution
            except ProcessTerminationError:
                return execution
        elif await self._inspector.matches(execution):
            return execution
        expected = execution.status
        execution.transition_to(ExecutionStatus.LOST)
        execution, _ = await self._repository.save_if_status(
            execution,
            expected={expected},
        )
        return execution

    async def close(self, *, cancel_running: bool = False) -> None:
        managed = list(self._managed.items())
        if cancel_running:
            await asyncio.gather(*(self.cancel(execution_id) for execution_id, _ in managed))
        else:
            monitor_tasks = [item.task for _, item in managed if item.task is not None]
            for task in monitor_tasks:
                task.cancel()
            await asyncio.gather(
                *monitor_tasks,
                return_exceptions=True,
            )
            self._managed.clear()

    async def _start_handle(
        self,
        request: ExecutionLaunchRequest,
        execution: Execution,
        effective_environment: dict[str, str],
    ) -> ProcessHandle:
        if request.executor_type is ExecutorType.PROCESS:
            return await self._process_executor.start(
                ProcessExecutionRequest(
                    execution_key=request.execution_key,
                    argv=request.argv,
                    cwd=request.cwd,
                    env=effective_environment,
                    timeout_seconds=request.timeout_seconds,
                    stdout_path=Path(execution.stdout_path),
                    stderr_path=Path(execution.stderr_path),
                )
            )
        if request.shell is None:
            raise ValueError("shell request is missing shell kind")
        return await self._shell_executor.start(
            ShellExecutionRequest(
                execution_key=request.execution_key,
                script=request.command_text or "",
                shell=request.shell,
                shell_path=request.shell_path,
                cwd=request.cwd,
                env=effective_environment,
                timeout_seconds=request.timeout_seconds,
                stdout_path=Path(execution.stdout_path),
                stderr_path=Path(execution.stderr_path),
            )
        )

    async def _monitor(self, execution_id: str, managed: _ManagedExecution) -> None:
        try:
            try:
                result = await managed.handle.wait(
                    termination_grace_seconds=self._termination_grace_seconds,
                    cleanup_containment=False,
                )
            except ProcessTreeTerminationError:
                # Known leader/PGID cleanup is not whole-tree absence evidence.
                # Leave the durable execution active and fenced for explicit
                # recovery; the monitor itself must still consume this expected
                # fail-closed outcome instead of leaking an unobserved task error.
                return
            cancel_task = managed.cancel_task
            if cancel_task is not None:
                try:
                    # Leader exit alone is not cancellation evidence.  A
                    # same-PGID child may still be alive during the TERM grace
                    # period or after a failed SIGKILL confirmation.
                    result = await asyncio.shield(cancel_task)
                except BaseException:
                    return
                await self._finalize(
                    execution_id,
                    result,
                    cancel_confirmed=True,
                    physical_stop_confirmed=(managed.handle.containment_identifier is not None),
                )
                await managed.handle.cleanup_confirmed_containment()
                return
            physical_stop_confirmed = managed.handle.containment_identifier is not None
            await self._finalize(
                execution_id,
                result,
                cancel_confirmed=False,
                physical_stop_confirmed=physical_stop_confirmed,
            )
            if physical_stop_confirmed:
                await managed.handle.cleanup_confirmed_containment()
        finally:
            self._managed.pop(execution_id, None)

    async def _finalize(
        self,
        execution_id: str,
        result: ProcessResult,
        cancel_confirmed: bool,
        physical_stop_confirmed: bool,
    ) -> None:
        execution = await self.get(execution_id)
        if cancel_confirmed:
            target = ExecutionStatus.CANCELLED
        else:
            if result.timed_out:
                target = ExecutionStatus.HARD_TIMEOUT
            elif execution.session_id is not None and result.status is ExecutionStatus.EXITED:
                target = ExecutionStatus.COMPLETED
            else:
                target = result.status
        if physical_stop_confirmed:
            execution, saved = await self._persist_confirmed_outcome(
                execution,
                target=target,
                exit_code=result.exit_code,
            )
            if saved and self._on_completed is not None:
                await self._on_completed(execution)
            return
        if execution.status not in {ExecutionStatus.STARTING, ExecutionStatus.RUNNING}:
            return
        expected = execution.status
        execution.transition_to(target, exit_code=result.exit_code)
        execution, saved = await self._repository.save_if_status(
            execution,
            expected={expected},
        )
        if saved and self._on_completed is not None:
            await self._on_completed(execution)


def _managed_task(managed: _ManagedExecution) -> asyncio.Task[None]:
    if managed.task is None:
        raise RuntimeError("managed execution monitor has not been initialized")
    return managed.task


def _observe_task_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


def _is_pre_spawn_failure(execution: Execution) -> bool:
    return execution.status is ExecutionStatus.FAILED and all(
        value is None
        for value in (
            execution.started_at,
            execution.process_created_at,
            execution.pid,
            execution.process_group_id,
            execution.containment_id,
        )
    )


def _is_explicit_pre_spawn_cancellation(execution: Execution) -> bool:
    return execution.status is ExecutionStatus.CANCELLED and all(
        value is None
        for value in (
            execution.started_at,
            execution.process_created_at,
            execution.pid,
            execution.process_group_id,
            execution.containment_id,
        )
    )


def _resolve_executable(
    executable: str | Path | None,
    environment: dict[str, str],
) -> str | None:
    if executable is None:
        return None
    path = Path(executable)
    if path.is_absolute():
        return str(path.resolve(strict=False))
    return shutil.which(str(executable), path=environment.get("PATH"))


def _read_output_slice(path: Path, cursor: int, max_bytes: int) -> OutputSlice:
    if cursor < 0:
        raise ValueError("output cursor must not be negative")
    if not path.exists():
        return OutputSlice(data=b"", cursor=cursor, next_cursor=cursor, eof=True)
    size = path.stat().st_size
    if cursor > size:
        raise ValueError(f"output cursor {cursor} is beyond file size {size}")
    with path.open("rb") as stream:
        stream.seek(cursor)
        data = stream.read(max_bytes)
    next_cursor = cursor + len(data)
    return OutputSlice(
        data=data,
        cursor=cursor,
        next_cursor=next_cursor,
        eof=next_cursor >= size,
    )


async def _terminate_detached_process(
    process_group_id: int | None, *, grace_seconds: float
) -> None:
    if process_group_id is None:
        return
    if _supports_posix_process_groups():
        await _terminate_posix_process_group(
            process_group_id,
            grace_seconds=grace_seconds,
        )
    else:
        await _kill_windows_process_tree(
            process_group_id,
            timeout_seconds=max(grace_seconds, 0.5),
        )


def _supports_posix_process_groups() -> bool:
    return os.name == "posix"
