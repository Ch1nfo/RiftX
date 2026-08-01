"""Cross-platform native terminal lifecycle and durable transcript management."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
)
from riftx.application.ports import (
    ExecutionRepository,
    RunEventRepository,
    TerminalRepository,
)
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    RunnerPrincipal,
    TerminalOwner,
    TerminalSession,
    TerminalStatus,
)
from riftx.domain.base import new_id, utc_now
from riftx.executors import merge_environment
from riftx.executors.containment import ProcessContainmentManager
from riftx.executors.process import ProcessStartError, ProcessTreeTerminationError

from .models import OutputSlice, TerminalLaunchRequest
from .paths import RunnerPaths
from .protocols import EffectGuard
from .supervisor import ProcessTerminationError
from .terminal_backend import (
    NativeTerminalBackend,
    NativeTerminalHandle,
    UnconfirmedTerminalStartError,
)
from .terminal_identity import require_terminal_start_replay_matches


class TerminalController(Protocol):
    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> TerminalSession: ...

    async def get(self, session_id: str) -> TerminalSession: ...

    async def get_execution(self, session_id: str) -> Execution: ...

    async def read(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> OutputSlice: ...

    async def write(
        self,
        session_id: str,
        data: bytes,
        *,
        actor: TerminalOwner,
    ) -> None: ...

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalSession: ...

    async def interrupt(self, session_id: str, *, actor: TerminalOwner) -> None: ...

    async def take_over(self, session_id: str) -> TerminalSession: ...

    async def release(self, session_id: str) -> TerminalSession: ...

    async def attach_transcript_artifact(
        self,
        session_id: str,
        artifact_id: str,
    ) -> TerminalSession: ...

    async def close(self, session_id: str) -> TerminalSession: ...


@dataclass(slots=True)
class _ManagedTerminal:
    handle: NativeTerminalHandle
    execution_id: str
    monitor_task: asyncio.Task[None] | None = None
    close_requested: bool = False
    termination_task: asyncio.Task[None] | None = None
    physical_stop_confirmed: asyncio.Event = field(default_factory=asyncio.Event)


_PHYSICAL_STOP_PROOF_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
}


class TerminalSupervisor:
    """Own native PTY/ConPTY sessions while state and transcripts remain durable."""

    def __init__(
        self,
        *,
        terminal_repository: TerminalRepository,
        execution_repository: ExecutionRepository,
        event_repository: RunEventRepository,
        paths: RunnerPaths,
        termination_grace_seconds: float = 2.0,
        native_backend: NativeTerminalBackend | None = None,
        platform_name: str | None = None,
        on_completed: Callable[[Execution], Awaitable[None]] | None = None,
        containment_manager: ProcessContainmentManager | None = None,
        autodetect_containment: bool = True,
        require_containment: bool = False,
    ) -> None:
        self._terminals = terminal_repository
        self._executions = execution_repository
        self._events = event_repository
        self._paths = paths
        self._termination_grace_seconds = termination_grace_seconds
        self._native_backend = native_backend
        self._platform_name = platform_name or os.name
        self._on_completed = on_completed
        self._containment_manager = containment_manager
        self._autodetect_containment = autodetect_containment
        self._require_containment = require_containment
        self._managed: dict[str, _ManagedTerminal] = {}
        self._detached_close_tasks: dict[str, asyncio.Task[Execution]] = {}
        self._containment_cleanup_tasks: dict[str, asyncio.Task[None]] = {}
        self._finalization_locks: dict[str, asyncio.Lock] = {}

    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> TerminalSession:
        backend = self._backend()
        self._ensure_required_containment_available(backend)
        explicit_identity = request.session_id is not None
        session_id = request.session_id or new_id()
        execution_id = request.execution_id or new_id()
        request = request.model_copy(
            update={"session_id": session_id, "execution_id": execution_id}
        )
        self._paths.ensure_run_layout(request.run_id)
        terminal_paths = self._paths.terminal(request.run_id, session_id)
        terminal_paths.directory.mkdir(parents=True, exist_ok=True)
        terminal_paths.transcript.touch(exist_ok=True)

        execution = Execution(
            id=execution_id,
            execution_key=request.execution_key or f"terminal:{session_id}",
            launch_fingerprint=request.launch_fingerprint,
            run_id=request.run_id,
            session_id=request.agent_session_id,
            tool_call_id=request.tool_call_id,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
            owner=request.runner_principal,
            executor_type=ExecutorType.PTY,
            argv=request.argv,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            platform_system=platform.system().lower() or os.name,
            platform_release=platform.release(),
            platform_architecture=platform.machine() or "unknown",
            stdout_path=str(terminal_paths.transcript),
            stderr_path=str(terminal_paths.transcript),
        )
        environment = merge_environment(request.env, mode=request.environment_mode)
        execution.executable_path = _resolve_terminal_executable(request.argv[0], environment)
        # CREATED is an explicit durable proof that no PTY backend has been
        # admitted yet.  The terminal projection is created first, then a
        # CREATED -> STARTING CAS selects the only caller allowed to dispatch.
        # This also lets a create failure race a concurrent stop without
        # either side manufacturing physical-stop proof after dispatch.
        try:
            execution, created = await self._executions.create_if_absent(execution)
        except RepositoryConflictError as exc:
            raise ApplicationConflictError(
                "execution_idempotency_conflict",
                str(exc),
                details={
                    "execution_id": execution.id,
                    "execution_key": execution.execution_key,
                },
            ) from exc
        terminal: TerminalSession | None = None
        if not created:
            if explicit_identity and execution.id != execution_id:
                raise ApplicationConflictError(
                    "execution_idempotency_conflict",
                    f"Terminal execution key {execution.execution_key!r} is already bound "
                    f"to execution ID {execution.id!r}",
                )
            self._require_exact_execution_replay(execution, request)
            existing_terminal = await self._terminals.get_by_execution(execution.id)
            if execution.status is ExecutionStatus.CREATED and execution.launch_fingerprint is None:
                await self._shield_predispatch_cleanup(
                    execution.id,
                    existing_terminal,
                    expected={ExecutionStatus.CREATED},
                )
                raise ApplicationConflictError(
                    "execution_idempotency_conflict",
                    f"Legacy terminal execution {execution.id!r} has no durable "
                    "launch fingerprint for CREATED admission",
                    details={"execution_id": execution.id},
                )
            if existing_terminal is not None:
                self._require_exact_terminal_replay(
                    existing_terminal,
                    execution,
                    request,
                )
                if execution.status is ExecutionStatus.CREATED:
                    # CREATED proves no caller has won dispatch yet.  An exact
                    # retry may adopt this projection and compete on the same
                    # execution-ID CAS without replaying an effect.
                    terminal = existing_terminal
                elif existing_terminal.status is TerminalStatus.CREATED:
                    if execution.physical_stop_confirmed_at is not None:
                        return await self._close_terminal_projection(existing_terminal)
                    raise ApplicationConflictError(
                        "terminal_start_in_progress",
                        f"Terminal {existing_terminal.id!r} has a durable admission "
                        "whose dispatch outcome is not yet confirmed",
                        details={
                            "session_id": existing_terminal.id,
                            "execution_id": execution.id,
                            "execution_status": execution.status.value,
                        },
                    )
                if terminal is None:
                    if effect_guard is not None:
                        await effect_guard()
                    return existing_terminal
            if terminal is None and execution.status is ExecutionStatus.STARTING:
                # Compatibility recovery for the old ordering, which wrote
                # STARTING immediately before the non-deleting Terminal store.
                # Missing projection is therefore durable pre-dispatch proof.
                await self._shield_predispatch_cleanup(
                    execution.id,
                    None,
                    expected={ExecutionStatus.STARTING},
                )
                if execution.launch_fingerprint is None:
                    raise ApplicationConflictError(
                        "execution_idempotency_conflict",
                        f"Legacy terminal execution {execution.id!r} has no durable "
                        "session launch fingerprint",
                        details={"execution_id": execution.id},
                    )
                return await self._create_closed_terminal_projection(
                    terminal=TerminalSession(
                        id=session_id,
                        run_id=request.run_id,
                        execution_id=execution.id,
                        runner_id=request.node_id,
                        shell=request.argv[0],
                        cwd=str(request.cwd),
                        owner=request.owner,
                        cols=request.cols,
                        rows=request.rows,
                    ),
                    execution=await self._require_execution(execution.id),
                    request=request,
                )
            if terminal is None and execution.status is not ExecutionStatus.CREATED:
                raise ApplicationConflictError(
                    "terminal_execution_exists",
                    f"Terminal execution {execution.execution_key!r} already exists",
                )
        if terminal is None:
            terminal = TerminalSession(
                id=session_id,
                run_id=request.run_id,
                execution_id=execution.id,
                runner_id=request.node_id,
                shell=request.argv[0],
                cwd=str(request.cwd),
                owner=request.owner,
                cols=request.cols,
                rows=request.rows,
            )
            create_task = asyncio.create_task(
                self._terminals.create(terminal),
                name=f"riftx-terminal-projection-create-{session_id}",
            )
            create_interrupted = await _wait_for_shielded_task(create_task)
            try:
                terminal = create_task.result()
            except BaseException as create_error:
                durable_terminal = await self._terminals.get_by_execution(execution.id)
                if durable_terminal is not None:
                    self._require_exact_terminal_replay(
                        durable_terminal,
                        execution,
                        request,
                    )
                    if not _is_terminal_projection_conflict(create_error):
                        latest = await self._require_execution(execution.id)
                        if latest.status is ExecutionStatus.CREATED:
                            await self._shield_predispatch_cleanup(
                                latest.id,
                                durable_terminal,
                                expected={ExecutionStatus.CREATED},
                            )
                    # Exact callers that first observed absence may adopt a
                    # concurrently created projection and contend on the
                    # execution-ID CAS. A non-conflict post-commit error
                    # instead settles CREATED and never dispatches.
                    if create_interrupted:
                        raise asyncio.CancelledError from None
                    if _is_terminal_projection_conflict(create_error):
                        terminal = durable_terminal
                    else:
                        raise
                else:
                    await self._shield_predispatch_cleanup(
                        execution.id,
                        None,
                        expected={ExecutionStatus.CREATED},
                    )
                    if create_interrupted:
                        raise asyncio.CancelledError from None
                    raise

            if create_interrupted:
                await self._shield_predispatch_cleanup(
                    execution.id,
                    terminal,
                    expected={ExecutionStatus.CREATED},
                )
                raise asyncio.CancelledError

        starting = execution.model_copy(deep=True)
        starting.transition_to(ExecutionStatus.STARTING)
        claim_task = asyncio.create_task(
            self._executions.save_if_status(
                starting,
                expected={ExecutionStatus.CREATED},
            ),
            name=f"riftx-terminal-admission-claim-{execution.id}",
        )
        claim_interrupted = await _wait_for_shielded_task(claim_task)
        try:
            execution, admitted = claim_task.result()
        except BaseException:
            latest = await self._require_execution(execution.id)
            if latest.status is ExecutionStatus.CREATED:
                await self._shield_predispatch_cleanup(
                    latest.id,
                    terminal,
                    expected={ExecutionStatus.CREATED},
                )
            raise
        if claim_interrupted:
            if admitted:
                await self._shield_predispatch_cleanup(
                    execution.id,
                    terminal,
                    expected={ExecutionStatus.STARTING},
                )
            raise asyncio.CancelledError
        if not admitted:
            if execution.physical_stop_confirmed_at is not None:
                await self._close_terminal_projection(terminal)
            raise ApplicationConflictError(
                "terminal_start_cancelled",
                f"Terminal {terminal.id!r} was cancelled before it could start",
                details={"session_id": terminal.id, "run_id": terminal.run_id},
            )

        try:
            if effect_guard is not None:
                await effect_guard()
        except BaseException:
            await self._shield_predispatch_cleanup(
                execution.id,
                terminal,
                expected={ExecutionStatus.STARTING},
            )
            raise
        execution = await self._require_execution(execution.id)
        if execution.status is not ExecutionStatus.STARTING:
            if execution.physical_stop_confirmed_at is not None:
                await self._close_terminal_projection(terminal)
            raise ApplicationConflictError(
                "terminal_start_cancelled",
                f"Terminal {terminal.id!r} was cancelled before backend dispatch",
                details={"session_id": terminal.id, "run_id": terminal.run_id},
            )
        backend_request = request.model_copy(
            update={"session_id": session_id, "execution_id": execution.id}
        )
        backend_start = asyncio.create_task(
            backend.start(
                backend_request,
                transcript_path=terminal_paths.transcript,
                environment=environment,
            ),
            name=f"riftx-terminal-native-start-{session_id}",
        )
        start_interrupted = False
        while not backend_start.done():
            try:
                await asyncio.shield(backend_start)
            except asyncio.CancelledError:
                # ConPTY starts in ``asyncio.to_thread``. Cancelling this
                # coroutine does not stop that thread and can otherwise lose
                # a successfully spawned native handle. Always collect the
                # backend outcome before propagating caller cancellation.
                start_interrupted = True
            except Exception:
                break
        try:
            handle = backend_start.result()
        except UnconfirmedTerminalStartError as exc:
            retained = await self._retain_unconfirmed_terminal_start(
                execution,
                terminal,
                exc.handle,
            )
            if start_interrupted:
                raise asyncio.CancelledError from None
            return retained
        except Exception:
            execution.transition_to(ExecutionStatus.FAILED)
            await self._executions.save_if_status(
                execution,
                expected={ExecutionStatus.STARTING},
            )
            terminal.transition_to(TerminalStatus.CLOSED)
            await self._terminals.save(terminal)
            if start_interrupted:
                raise asyncio.CancelledError from None
            raise

        if start_interrupted:
            execution.pid = handle.pid
            execution.process_group_id = _handle_process_group_id(handle)
            execution.containment_id = _handle_containment_identifier(handle)
            execution.process_created_at = utc_now()
            await self._abort_spawned_terminal(execution, terminal, handle)
            raise asyncio.CancelledError

        try:
            execution.pid = handle.pid
            execution.process_group_id = _handle_process_group_id(handle)
            execution.containment_id = _handle_containment_identifier(handle)
            started_at = utc_now()
            execution.process_created_at = started_at
            execution, identity_saved = await self._executions.save_if_status(
                execution,
                expected={ExecutionStatus.STARTING},
            )
            if not identity_saved:
                await self._terminate_unadmitted_terminal(handle, terminal)
                raise ApplicationConflictError(
                    "terminal_start_cancelled",
                    f"Terminal {terminal.id!r} was cancelled before it could open",
                    details={"session_id": terminal.id, "run_id": terminal.run_id},
                )

            if effect_guard is not None:
                await effect_guard()

            # Unix PTY target code remains behind the child activation gate
            # until durable native identity and the final Run guard both win.
            await _activate_terminal_handle(handle)

            execution.transition_to(ExecutionStatus.RUNNING, at=started_at)
            execution, started = await self._executions.save_if_status(
                execution,
                expected={ExecutionStatus.STARTING},
            )
            if not started:
                await self._terminate_unadmitted_terminal(handle, terminal)
                raise ApplicationConflictError(
                    "terminal_start_cancelled",
                    f"Terminal {terminal.id!r} was cancelled before it could open",
                    details={"session_id": terminal.id, "run_id": terminal.run_id},
                )
            terminal.transition_to(TerminalStatus.OPEN)
            await self._terminals.save(terminal)
        except BaseException:
            await self._abort_spawned_terminal(execution, terminal, handle)
            raise

        managed = _ManagedTerminal(handle=handle, execution_id=execution.id)
        self._managed[session_id] = managed
        managed.monitor_task = asyncio.create_task(
            self._monitor(session_id, managed),
            name=f"riftx-terminal-monitor-{session_id}",
        )
        await self._events.append(
            request.run_id,
            "terminal.opened",
            {
                "session_id": session_id,
                "execution_id": execution.id,
                "argv": request.argv,
                "cwd": str(request.cwd),
                "owner": terminal.owner.value,
                "cols": terminal.cols,
                "rows": terminal.rows,
                "backend": "conpty" if self._platform_name == "nt" else "pty",
            },
        )
        return terminal

    async def _retain_unconfirmed_terminal_start(
        self,
        execution: Execution,
        terminal: TerminalSession,
        handle: NativeTerminalHandle,
    ) -> TerminalSession:
        """Retain a spawned PTY whose backend cleanup could not be proven."""

        execution.pid = handle.pid
        execution.process_group_id = _handle_process_group_id(handle)
        execution.containment_id = _handle_containment_identifier(handle)
        execution.process_created_at = utc_now()
        # Keep the exact handle reachable before attempting the state CAS.  A
        # failed CAS must not turn a native process into an unowned orphan.
        self._managed[terminal.id] = _ManagedTerminal(
            handle=handle,
            execution_id=execution.id,
            close_requested=True,
        )
        current = await self._executions.get(execution.id)
        if current is not None and current.status is ExecutionStatus.STARTING:
            current.pid = execution.pid
            current.process_group_id = execution.process_group_id
            current.containment_id = execution.containment_id
            current.process_created_at = execution.process_created_at
            await self._executions.save_if_status(
                current,
                expected={ExecutionStatus.STARTING},
            )
        durable = await self._terminals.get(terminal.id)
        return durable or terminal

    async def _require_execution(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise ProcessTerminationError(
                f"Terminal execution {execution_id!r} disappeared during admission"
            )
        return execution

    @staticmethod
    def _require_exact_execution_replay(
        execution: Execution,
        request: TerminalLaunchRequest,
    ) -> None:
        expected_key = request.execution_key or f"terminal:{request.session_id}"
        fields: tuple[tuple[str, object, object], ...] = (
            ("execution_id", execution.id, request.execution_id),
            ("execution_key", execution.execution_key, expected_key),
            ("run_id", execution.run_id, request.run_id),
            ("agent_session_id", execution.session_id, request.agent_session_id),
            ("tool_call_id", execution.tool_call_id, request.tool_call_id),
            ("attempt_group", execution.attempt_group, request.attempt_group),
            ("node_id", execution.node_id, request.node_id),
            ("runner_principal", execution.owner, request.runner_principal),
            ("executor_type", execution.executor_type, ExecutorType.PTY),
            ("argv", execution.argv, request.argv),
            ("tool_id", execution.tool_id, request.tool_id),
            ("tool_version", execution.tool_version, request.tool_version),
            ("cwd", execution.cwd, str(request.cwd)),
            ("env", execution.env_diff, request.env),
        )
        mismatched = [name for name, persisted, requested in fields if persisted != requested]
        if (
            execution.launch_fingerprint is not None
            and execution.launch_fingerprint != request.launch_fingerprint
        ):
            mismatched.append("launch_fingerprint")
        if mismatched:
            raise ApplicationConflictError(
                "execution_idempotency_conflict",
                f"Terminal execution {execution.id!r} conflicts with requested launch "
                f"fields: {', '.join(sorted(set(mismatched)))}",
                details={
                    "execution_id": execution.id,
                    "mismatched_fields": sorted(set(mismatched)),
                },
            )

    @staticmethod
    def _require_exact_terminal_replay(
        terminal: TerminalSession,
        execution: Execution,
        request: TerminalLaunchRequest,
    ) -> None:
        try:
            require_terminal_start_replay_matches(terminal, execution, request)
        except ValueError as exc:
            raise ApplicationConflictError(
                "execution_idempotency_conflict",
                str(exc),
                details={
                    "execution_id": execution.id,
                    "session_id": terminal.id,
                },
            ) from exc

    async def _cancel_predispatch_execution(
        self,
        execution_id: str,
        *,
        expected: set[ExecutionStatus],
    ) -> tuple[Execution, bool]:
        execution = await self._require_execution(execution_id)
        if execution.physical_stop_confirmed_at is not None:
            return execution, False
        if execution.status not in expected:
            return execution, False
        if not _has_no_terminal_dispatch_identity(execution):
            raise ProcessTerminationError(
                f"Refusing pre-dispatch cleanup for terminal execution {execution.id!r} "
                "because durable dispatch identity already exists"
            )
        candidate = execution.model_copy(deep=True)
        candidate.transition_to(ExecutionStatus.CANCELLED, exit_code=0)
        candidate.physical_stop_confirmed_at = utc_now()
        execution, saved = await self._executions.save_if_status(
            candidate,
            expected={execution.status},
        )
        if saved:
            return execution, True
        if execution.physical_stop_confirmed_at is not None:
            return execution, False
        return execution, False

    async def _shield_predispatch_cleanup(
        self,
        execution_id: str,
        terminal: TerminalSession | None,
        *,
        expected: set[ExecutionStatus],
    ) -> None:
        cleanup_task = asyncio.create_task(
            self._cancel_predispatch_execution(
                execution_id,
                expected=expected,
            ),
            name=f"riftx-terminal-predispatch-cleanup-{execution_id}",
        )
        interrupted = await _wait_for_shielded_task(cleanup_task)
        execution, _ = cleanup_task.result()
        if execution.physical_stop_confirmed_at is None:
            raise ProcessTerminationError(
                f"Could not persist pre-dispatch physical-stop proof for terminal "
                f"execution {execution_id!r}; durable status is "
                f"{execution.status.value!r}"
            )
        if terminal is None:
            if interrupted:
                raise asyncio.CancelledError
            return
        await self._close_terminal_projection(terminal)
        if interrupted:
            raise asyncio.CancelledError

    async def _close_terminal_projection(
        self,
        terminal: TerminalSession,
    ) -> TerminalSession:
        durable_terminal = await self._terminals.get(terminal.id)
        if durable_terminal is None:
            return terminal
        if durable_terminal is not None and durable_terminal.status in {
            TerminalStatus.CREATED,
            TerminalStatus.OPEN,
            TerminalStatus.LOST,
        }:
            candidate = durable_terminal.model_copy(deep=True)
            candidate.transition_to(TerminalStatus.CLOSED)
            durable_terminal, _ = await self._terminals.save_if_status(
                candidate,
                expected={durable_terminal.status},
            )
        return durable_terminal

    async def _create_closed_terminal_projection(
        self,
        *,
        terminal: TerminalSession,
        execution: Execution,
        request: TerminalLaunchRequest,
    ) -> TerminalSession:
        closed = terminal.model_copy(deep=True)
        closed.transition_to(TerminalStatus.CLOSED)
        create_task = asyncio.create_task(
            self._terminals.create(closed),
            name=f"riftx-terminal-closed-projection-create-{closed.id}",
        )
        interrupted = await _wait_for_shielded_task(create_task)
        try:
            durable = create_task.result()
        except BaseException:
            durable = await self._terminals.get_by_execution(execution.id)
            if durable is None:
                raise
            self._require_exact_terminal_replay(durable, execution, request)
        if interrupted:
            raise asyncio.CancelledError
        return durable

    async def _terminate_unadmitted_terminal(
        self,
        handle: NativeTerminalHandle,
        terminal: TerminalSession,
    ) -> None:
        await handle.terminate(
            self._termination_grace_seconds,
            cleanup_containment=False,
        )
        await handle.close_output()
        finalized, _ = await self._finalize_execution(
            terminal.execution_id,
            exit_code=0,
            cancel_confirmed=True,
            physical_stop_confirmed=True,
        )
        if finalized is None or finalized.physical_stop_confirmed_at is None:
            raise ProcessTerminationError(
                f"Terminal execution {terminal.execution_id!r} stopped, but durable "
                "physical-stop proof could not be persisted"
            )
        durable_terminal = await self._terminals.get(terminal.id)
        if durable_terminal is not None and durable_terminal.status is TerminalStatus.CREATED:
            durable_terminal.transition_to(TerminalStatus.CLOSED)
            await self._terminals.save(durable_terminal)
        await self._cleanup_confirmed_handle_containment(
            terminal.execution_id,
            handle,
        )

    async def _abort_spawned_terminal(
        self,
        execution: Execution,
        terminal: TerminalSession,
        handle: NativeTerminalHandle,
    ) -> None:
        # Register the handle before awaiting cleanup. If termination fails or
        # this startup task is cancelled again, a durable CANCEL handler can
        # still find and retry the exact native handle instead of falsely
        # treating STARTING/CREATED state as stopped.
        managed = self._managed.get(terminal.id)
        if managed is None:
            managed = _ManagedTerminal(
                handle=handle,
                execution_id=execution.id,
                close_requested=True,
            )
            self._managed[terminal.id] = managed
            managed.monitor_task = asyncio.create_task(
                self._monitor(terminal.id, managed),
                name=f"riftx-terminal-monitor-{terminal.id}",
            )
        try:
            termination_task = self._get_or_start_termination(terminal.id, managed)
            await asyncio.shield(termination_task)
            if managed.monitor_task is not None:
                await asyncio.shield(managed.monitor_task)
        except BaseException:
            # Keep STARTING and persist identity when termination cannot be
            # confirmed; the Run safety transition must remain fenced and the
            # registered handle remains available for a later retry.
            current = await self._executions.get(execution.id)
            if current is not None and current.status is ExecutionStatus.STARTING:
                current.pid = handle.pid
                current.process_group_id = _handle_process_group_id(handle)
                current.containment_id = _handle_containment_identifier(handle)
                current.process_created_at = execution.process_created_at or utc_now()
                await self._executions.save_if_status(
                    current,
                    expected={ExecutionStatus.STARTING},
                )
            return

    async def get(self, session_id: str) -> TerminalSession:
        terminal = await self._terminals.get(session_id)
        if terminal is None:
            raise EntityNotFoundError("Terminal Session", session_id)
        return terminal

    async def get_execution(self, session_id: str) -> Execution:
        terminal = await self.get(session_id)
        execution = await self._executions.get(terminal.execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", terminal.execution_id)
        return execution

    async def read(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> OutputSlice:
        if max_bytes < 1 or max_bytes > 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 1048576")
        execution = await self.get_execution(session_id)
        return await asyncio.to_thread(
            _read_output_slice,
            Path(execution.stdout_path),
            cursor,
            max_bytes,
        )

    async def write(
        self,
        session_id: str,
        data: bytes,
        *,
        actor: TerminalOwner,
    ) -> None:
        terminal = await self.get(session_id)
        self._require_writer(terminal, actor)
        await self._require_managed(session_id).handle.write(data)

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalSession:
        terminal = await self.get(session_id)
        terminal.resize(cols, rows)
        await self._require_managed(session_id).handle.resize(cols, rows)
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.resized",
            {"session_id": terminal.id, "cols": cols, "rows": rows},
        )
        return terminal

    async def interrupt(self, session_id: str, *, actor: TerminalOwner) -> None:
        terminal = await self.get(session_id)
        self._require_writer(terminal, actor)
        await self._require_managed(session_id).handle.interrupt()
        await self._events.append(
            terminal.run_id,
            "terminal.interrupted",
            {"session_id": terminal.id, "actor": actor.value},
        )

    async def take_over(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        execution = await self.get_execution(session_id)
        cursor = await asyncio.to_thread(_output_size, Path(execution.stdout_path))
        terminal.take_over(cursor=cursor)
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.taken_over",
            {"session_id": terminal.id, "owner": terminal.owner.value},
        )
        return terminal

    async def release(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        execution = await self.get_execution(session_id)
        cursor = await asyncio.to_thread(_output_size, Path(execution.stdout_path))
        terminal.release(cursor=cursor)
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.released",
            {"session_id": terminal.id, "owner": terminal.owner.value},
        )
        return terminal

    async def attach_transcript_artifact(
        self,
        session_id: str,
        artifact_id: str,
    ) -> TerminalSession:
        terminal = await self.get(session_id)
        if terminal.transcript_artifact_id is not None:
            return terminal
        terminal.transcript_artifact_id = artifact_id
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.transcript_archived",
            {"session_id": terminal.id, "artifact_id": artifact_id},
        )
        return terminal

    async def close(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        managed = self._managed.get(session_id)
        if managed is not None:
            managed.close_requested = True
            termination_task = self._get_or_start_termination(session_id, managed)
            # Keep the physical cleanup alive if an API request disconnects or
            # its caller is cancelled.  The monitor shares the affirmative
            # confirmation event and cannot persist CANCELLED before success.
            await asyncio.shield(termination_task)
            if managed.monitor_task is not None:
                await asyncio.shield(managed.monitor_task)
            else:
                await self._finalize_unmonitored_terminal_cancel(
                    session_id,
                    managed,
                )
            return await self.get(session_id)
        execution = await self._executions.get(terminal.execution_id)
        if execution is None:
            raise ProcessTerminationError(
                f"Cannot confirm terminal {terminal.id!r} stopped because its "
                "durable execution is missing"
            )
        if execution.physical_stop_confirmed_at is not None:
            if terminal.status in {
                TerminalStatus.CREATED,
                TerminalStatus.OPEN,
                TerminalStatus.LOST,
            }:
                terminal.transition_to(TerminalStatus.CLOSED)
                await self._terminals.save(terminal)
            await self._cleanup_confirmed_detached_containment(execution)
            return terminal
        if (
            execution.status is ExecutionStatus.CANCELLED
            and terminal.status is TerminalStatus.CLOSED
            and _is_explicit_pre_spawn_absence(execution)
        ):
            execution, _ = await self._finalize_execution(
                execution.id,
                exit_code=execution.exit_code or 0,
                cancel_confirmed=True,
                physical_stop_confirmed=True,
            )
            if execution is None or execution.physical_stop_confirmed_at is None:
                raise ProcessTerminationError(
                    f"Could not persist pre-spawn physical-stop proof for terminal "
                    f"execution {terminal.execution_id!r}"
                )
            return terminal
        if execution.containment_id is not None and self._platform_name == "posix":
            return await self._close_detached_contained_terminal(terminal, execution)
        if execution is not None and execution.status is ExecutionStatus.FAILED:
            if _is_explicit_pre_spawn_failure(execution) and terminal.status in {
                TerminalStatus.CREATED,
                TerminalStatus.CLOSED,
            }:
                execution, _ = await self._finalize_execution(
                    execution.id,
                    exit_code=execution.exit_code or 0,
                    cancel_confirmed=True,
                    physical_stop_confirmed=True,
                )
                if (
                    execution is None
                    or execution.status is not ExecutionStatus.CANCELLED
                    or execution.physical_stop_confirmed_at is None
                ):
                    raise ProcessTerminationError(
                        f"Could not persist confirmed cancellation for failed terminal "
                        f"execution {terminal.execution_id!r}"
                    )
                if terminal.status is TerminalStatus.CREATED:
                    terminal.transition_to(TerminalStatus.CLOSED)
                    await self._terminals.save(terminal)
                return terminal
            # A native PTY cannot be safely reattached after its owning Runner
            # loses the handle. FAILED is an application outcome, not evidence
            # that the underlying process is gone. Preserve it until the owner
            # can terminate the real handle or an operator reconciles the host.
            raise ProcessTerminationError(
                f"Cannot confirm failed terminal execution {execution.id!r} stopped "
                "because its native terminal handle is not attached"
            )
        raise ProcessTerminationError(
            f"Cannot confirm terminal execution {execution.id!r} stopped because its "
            f"native terminal handle is not attached (execution={execution.status.value}, "
            f"terminal={terminal.status.value})"
        )

    async def _finalize_unmonitored_terminal_cancel(
        self,
        session_id: str,
        managed: _ManagedTerminal,
    ) -> None:
        """Finalize a startup-cleanup quarantine after an explicit stop retry."""

        execution, saved = await self._finalize_execution(
            managed.execution_id,
            exit_code=0,
            cancel_confirmed=True,
            physical_stop_confirmed=True,
        )
        if execution is None or execution.physical_stop_confirmed_at is None:
            raise ProcessTerminationError(
                f"Terminal execution {managed.execution_id!r} stopped, but durable "
                "physical-stop proof could not be persisted"
            )
        await managed.handle.close_output()
        terminal = await self.get(session_id)
        if terminal.status in {
            TerminalStatus.CREATED,
            TerminalStatus.OPEN,
            TerminalStatus.LOST,
        }:
            terminal.transition_to(TerminalStatus.CLOSED)
            await self._terminals.save(terminal)
        await self._cleanup_confirmed_handle_containment(
            execution.id,
            managed.handle,
        )
        await self._events.append(
            terminal.run_id,
            "terminal.closed",
            {
                "session_id": terminal.id,
                "execution_id": terminal.execution_id,
                "exit_code": 0,
                "requested": True,
                "physical_stop_confirmed": True,
            },
        )
        if saved and self._on_completed is not None:
            await self._on_completed(execution)
        self._managed.pop(session_id, None)

    async def close_execution(self, execution_id: str) -> Execution:
        """Close a PTY by its durable execution identity.

        A crash can leave the execution row durable while the separate terminal
        state file is absent.  The execution's exact containment identity can
        still stop that native tree, but a missing terminal row or containment
        boundary is never treated as evidence that it already stopped.
        """

        execution = await self._executions.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        if execution.executor_type is not ExecutorType.PTY:
            raise ProcessTerminationError(
                f"Execution {execution.id!r} is not a native terminal execution"
            )

        terminal = await self._terminals.get_by_execution(execution.id)
        if execution.physical_stop_confirmed_at is not None:
            if terminal is not None and terminal.status in {
                TerminalStatus.CREATED,
                TerminalStatus.OPEN,
                TerminalStatus.LOST,
            }:
                terminal.transition_to(TerminalStatus.CLOSED)
                await self._terminals.save(terminal)
            await self._cleanup_confirmed_detached_containment(execution)
            return execution

        if execution.status is ExecutionStatus.CREATED and _has_no_terminal_dispatch_identity(
            execution
        ):
            await self._shield_predispatch_cleanup(
                execution.id,
                terminal,
                expected={ExecutionStatus.CREATED},
            )
            return await self._require_execution(execution.id)

        if terminal is not None:
            await self.close(terminal.id)
            closed = await self._executions.get(execution.id)
            if closed is None:
                raise ProcessTerminationError(
                    f"Terminal execution {execution.id!r} disappeared after close"
                )
            return closed

        if execution.status is ExecutionStatus.CANCELLED and _is_explicit_pre_spawn_absence(
            execution
        ):
            finalized, _ = await self._finalize_execution(
                execution.id,
                exit_code=execution.exit_code or 0,
                cancel_confirmed=True,
                physical_stop_confirmed=True,
            )
            if finalized is None or finalized.physical_stop_confirmed_at is None:
                raise ProcessTerminationError(
                    f"Could not persist pre-spawn physical-stop proof for terminal "
                    f"execution {execution.id!r}"
                )
            return finalized

        if execution.containment_id is not None and self._platform_name == "posix":
            return await asyncio.shield(self._get_or_start_detached_close(execution))

        raise ProcessTerminationError(
            f"Cannot confirm terminal execution {execution.id!r} stopped because "
            "its terminal state is missing and no durable kernel containment "
            "identity is available"
        )

    async def _close_detached_contained_terminal(
        self,
        terminal: TerminalSession,
        execution: Execution,
    ) -> TerminalSession:
        await asyncio.shield(self._get_or_start_detached_close(execution))
        closed = await self._terminals.get(terminal.id)
        if closed is None:
            raise ProcessTerminationError(
                f"Terminal execution {execution.id!r} stopped, but terminal state "
                f"{terminal.id!r} is missing"
            )
        return closed

    def _get_or_start_detached_close(self, execution: Execution) -> asyncio.Task[Execution]:
        task = self._detached_close_tasks.get(execution.id)
        if task is None or task.cancelled() or (task.done() and task.exception() is not None):
            task = asyncio.create_task(
                self._close_detached_contained_execution(execution),
                name=f"riftx-detached-terminal-close-{execution.id}",
            )
            task.add_done_callback(_observe_task_exception)
            self._detached_close_tasks[execution.id] = task
        return task

    async def _close_detached_contained_execution(self, execution: Execution) -> Execution:
        containment = self._resolve_detached_containment(execution)
        # Unlike an attached handle, a newly constructed resolver has not
        # itself observed this leaf empty. Missing state is therefore not stop
        # evidence and must never manufacture a durable cancellation.
        if not containment.boundary_exists():
            raise ProcessTerminationError(
                f"Cannot confirm terminal execution {execution.id!r} stopped because "
                f"containment {execution.containment_id!r} is missing"
            )
        try:
            await containment.terminate(grace_seconds=self._termination_grace_seconds)
        except Exception as exc:
            raise ProcessTerminationError(
                f"Failed to terminate or verify containment "
                f"{execution.containment_id!r} for terminal execution {execution.id!r}"
            ) from exc

        # This task is supervisor-owned and callers await it through shield().
        # Once cgroup.kill/populated=0 succeeds, finish every durable write
        # before cleanup so an HTTP disconnect cannot strand ambiguous state.
        finalized, _ = await self._finalize_execution(
            execution.id,
            exit_code=execution.exit_code or 0,
            cancel_confirmed=True,
            physical_stop_confirmed=True,
        )
        if finalized is None or finalized.physical_stop_confirmed_at is None:
            raise ProcessTerminationError(
                f"Could not persist physical-stop proof for detached terminal "
                f"execution {execution.id!r}"
            )

        terminal = await self._terminals.get_by_execution(execution.id)
        if terminal is not None:
            if terminal.status in {
                TerminalStatus.CREATED,
                TerminalStatus.OPEN,
                TerminalStatus.LOST,
            }:
                terminal.transition_to(TerminalStatus.CLOSED)
                await self._terminals.save(terminal)
        await self._cleanup_confirmed_detached_containment(finalized)
        return finalized

    async def _cleanup_confirmed_handle_containment(
        self,
        execution_id: str,
        handle: NativeTerminalHandle,
    ) -> None:
        await asyncio.shield(
            self._get_or_start_containment_cleanup(
                execution_id,
                handle.cleanup_confirmed_containment,
            )
        )

    async def _cleanup_confirmed_detached_containment(
        self,
        execution: Execution,
    ) -> None:
        if execution.physical_stop_confirmed_at is None or execution.containment_id is None:
            return
        await asyncio.shield(
            self._get_or_start_containment_cleanup(
                execution.id,
                lambda: self._cleanup_detached_containment(execution),
            )
        )

    def _get_or_start_containment_cleanup(
        self,
        execution_id: str,
        cleanup: Callable[[], Awaitable[None]],
    ) -> asyncio.Task[None]:
        task = self._containment_cleanup_tasks.get(execution_id)
        if task is None or task.cancelled() or (task.done() and task.exception() is not None):
            task = asyncio.create_task(
                cleanup(),
                name=f"riftx-terminal-containment-cleanup-{execution_id}",
            )
            task.add_done_callback(_observe_task_exception)
            self._containment_cleanup_tasks[execution_id] = task
        return task

    async def _cleanup_detached_containment(self, execution: Execution) -> None:
        containment = self._resolve_detached_containment(execution)
        try:
            if not containment.boundary_exists():
                # Durable proof was written before cleanup. Disappearance of
                # this exact leaf is therefore an idempotent success condition.
                return
            await containment.cleanup()
        except Exception as exc:
            raise ProcessTerminationError(
                f"Terminal execution {execution.id!r} has durable stop proof, but "
                f"containment {execution.containment_id!r} could not be cleaned up"
            ) from exc

    def _resolve_detached_containment(self, execution: Execution):
        manager = self._containment_manager_for_recovery()
        resolver = getattr(manager, "containment_for", None)
        if resolver is None:
            raise ProcessTerminationError(
                f"Cannot confirm detached terminal execution {execution.id!r} stopped: "
                f"recorded containment {execution.containment_id!r} is unavailable"
            )
        try:
            containment = resolver(execution.execution_key)
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not resolve containment for detached terminal execution {execution.id!r}"
            ) from exc
        try:
            boundary_exists = containment.boundary_exists()
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not inspect containment for detached terminal execution {execution.id!r}"
            ) from exc
        if not boundary_exists:
            # The caller will fail closed with the explicit missing-boundary
            # diagnostic. Do not ask a path-derived identifier to materialize
            # after its kernel object has disappeared.
            return containment
        try:
            current_identifier = containment.identifier
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not identify containment for detached terminal execution {execution.id!r}"
            ) from exc
        if current_identifier != execution.containment_id:
            raise ProcessTerminationError(
                f"Cannot confirm detached terminal execution {execution.id!r} stopped: "
                "containment belongs to a different delegated root or execution key"
            )
        return containment

    def _containment_manager_for_recovery(self) -> ProcessContainmentManager | None:
        if self._containment_manager is not None:
            return self._containment_manager
        if self._platform_name != "posix":
            return None
        backend = self._backend()
        manager = getattr(backend, "containment_manager", None)
        if manager is not None:
            self._containment_manager = manager
        return manager

    async def recover(
        self,
        *,
        node_id: str | None = None,
        owner: RunnerPrincipal | None = None,
    ) -> list[TerminalSession]:
        recovered: list[TerminalSession] = []
        for terminal in await self._terminals.list_active():
            execution = await self._executions.get(terminal.execution_id)
            if node_id is not None and (execution is None or execution.node_id != node_id):
                continue
            if owner is not None and (execution is None or execution.owner != owner):
                continue
            if execution is not None and execution.physical_stop_confirmed_at is not None:
                # A crash can occur after the execution proof commits but
                # before the separate terminal row is closed. Repair that
                # projection first, then remove the already-confirmed empty
                # containment. Never manufacture proof from terminal status.
                terminal.transition_to(TerminalStatus.CLOSED)
                await self._terminals.save(terminal)
                await self._cleanup_confirmed_detached_containment(execution)
                recovered.append(terminal)
                continue
            if execution is not None and execution.status is ExecutionStatus.CREATED:
                # CREATED is the durable pre-dispatch phase introduced for
                # projection admission. A restart can close it affirmatively;
                # it must not become a LOST row with an endless monitor.
                await self._shield_predispatch_cleanup(
                    execution.id,
                    terminal,
                    expected={ExecutionStatus.CREATED},
                )
                recovered.append(await self.get(terminal.id))
                continue
            if execution is not None and execution.status in {
                ExecutionStatus.STARTING,
                ExecutionStatus.RUNNING,
            }:
                if execution.containment_id is not None:
                    try:
                        containment = self._resolve_detached_containment(execution)
                        if not containment.boundary_exists():
                            # Missing/replaced kernel state is uncertainty, not
                            # a process-absence or LOST verdict. Preserve both
                            # active rows so cancellation remains fenced and a
                            # correctly namespaced owner can retry later.
                            recovered.append(terminal)
                            continue
                    except ProcessTerminationError:
                        recovered.append(terminal)
                        continue
                expected = execution.status
                execution.transition_to(ExecutionStatus.LOST)
                execution, saved = await self._executions.save_if_status(
                    execution,
                    expected={expected},
                )
                if not saved:
                    # A concurrent confirmed cancellation wins over this late
                    # restart observation. Do not overwrite it or persist a
                    # stale terminal LOST state derived from the old snapshot.
                    continue
            terminal.transition_to(TerminalStatus.LOST)
            await self._terminals.save(terminal)
            await self._events.append(
                terminal.run_id,
                "terminal.lost",
                {"session_id": terminal.id, "reason": "runner_restarted"},
            )
            recovered.append(terminal)
        return recovered

    async def close_all(self) -> None:
        await asyncio.gather(
            *(self.close(session_id) for session_id in list(self._managed)),
            return_exceptions=True,
        )

    async def _monitor(self, session_id: str, managed: _ManagedTerminal) -> None:
        try:
            try:
                exit_code = await managed.handle.wait(cleanup_containment=False)
            except ProcessTreeTerminationError:
                # A leader/PGID exit without whole-tree absence proof must not
                # become a durable terminal outcome. Preserve the active row
                # for an explicit safety retry or operator reconciliation.
                return

            cancel_confirmed = managed.close_requested
            if cancel_confirmed:
                # A leader exiting while TERM is in flight is not enough.  The
                # backend's terminate() must affirmatively confirm its whole
                # owned process group/tree before cancellation becomes durable.
                await managed.physical_stop_confirmed.wait()

            execution, saved = await self._finalize_execution(
                managed.execution_id,
                exit_code=exit_code,
                cancel_confirmed=cancel_confirmed,
                physical_stop_confirmed=True,
            )
            if execution is None or execution.physical_stop_confirmed_at is None:
                raise ProcessTerminationError(
                    f"Terminal execution {managed.execution_id!r} stopped, but durable "
                    "physical-stop proof could not be persisted"
                )
            await managed.handle.close_output()
            terminal = await self.get(session_id)
            if terminal.status in {
                TerminalStatus.CREATED,
                TerminalStatus.OPEN,
                TerminalStatus.LOST,
            }:
                terminal.transition_to(TerminalStatus.CLOSED)
                await self._terminals.save(terminal)
            await self._events.append(
                terminal.run_id,
                "terminal.closed",
                {
                    "session_id": terminal.id,
                    "execution_id": terminal.execution_id,
                    "exit_code": exit_code,
                    "requested": cancel_confirmed,
                    "physical_stop_confirmed": (
                        managed.physical_stop_confirmed.is_set() if cancel_confirmed else None
                    ),
                },
            )
            if saved and execution is not None and self._on_completed is not None:
                await self._on_completed(execution)
            await self._cleanup_confirmed_handle_containment(
                execution.id,
                managed.handle,
            )
        finally:
            self._managed.pop(session_id, None)

    def _get_or_start_termination(
        self,
        session_id: str,
        managed: _ManagedTerminal,
    ) -> asyncio.Task[None]:
        task = managed.termination_task
        if task is None or (task.done() and not managed.physical_stop_confirmed.is_set()):
            task = asyncio.create_task(
                self._terminate_and_confirm(managed),
                name=f"riftx-terminal-terminate-{session_id}",
            )
            # A caller cancellation leaves this shielded task running. Observe
            # any later exception so it is retained for retry without producing
            # an unhandled-task warning.
            task.add_done_callback(_observe_task_exception)
            managed.termination_task = task
        return task

    async def _terminate_and_confirm(self, managed: _ManagedTerminal) -> None:
        await managed.handle.terminate(
            self._termination_grace_seconds,
            cleanup_containment=False,
        )
        managed.physical_stop_confirmed.set()

    async def _finalize_execution(
        self,
        execution_id: str,
        *,
        exit_code: int,
        cancel_confirmed: bool,
        physical_stop_confirmed: bool,
    ) -> tuple[Execution | None, bool]:
        lock = self._finalization_locks.setdefault(execution_id, asyncio.Lock())
        async with lock:
            return await self._finalize_execution_locked(
                execution_id,
                exit_code=exit_code,
                cancel_confirmed=cancel_confirmed,
                physical_stop_confirmed=physical_stop_confirmed,
            )

    async def _finalize_execution_locked(
        self,
        execution_id: str,
        *,
        exit_code: int,
        cancel_confirmed: bool,
        physical_stop_confirmed: bool,
    ) -> tuple[Execution | None, bool]:
        execution = await self._executions.get(execution_id)
        if execution is None:
            return None, False

        target = ExecutionStatus.CANCELLED if cancel_confirmed else ExecutionStatus.EXITED
        confirmation_time = execution.physical_stop_confirmed_at or utc_now()
        for _ in range(8):
            if physical_stop_confirmed and execution.physical_stop_confirmed_at is not None:
                return execution, False
            expected = execution.status
            candidate = execution.model_copy(deep=True)
            if candidate.can_transition_to(target):
                candidate.transition_to(target, exit_code=exit_code)
            elif physical_stop_confirmed and candidate.status not in _PHYSICAL_STOP_PROOF_STATUSES:
                # Physical absence remains valid across a concurrent FAILED or
                # LOST write. Converge such outcomes to the only safe terminal
                # state they can reach instead of attaching proof to an invalid
                # domain status.
                if candidate.can_transition_to(ExecutionStatus.CANCELLED):
                    candidate.transition_to(
                        ExecutionStatus.CANCELLED,
                        exit_code=exit_code,
                    )
                else:
                    raise ProcessTerminationError(
                        f"Terminal execution {execution_id!r} stopped, but status "
                        f"{candidate.status.value!r} cannot carry durable stop proof"
                    )
            elif not physical_stop_confirmed:
                return execution, False
            if physical_stop_confirmed:
                candidate.physical_stop_confirmed_at = confirmation_time
            execution, saved = await self._executions.save_if_status(
                candidate,
                expected={expected},
            )
            if saved:
                return execution, True
        raise ProcessTerminationError(
            f"Terminal execution {execution_id!r} stopped, but its durable state "
            "kept changing before physical-stop proof could be persisted"
        )

    def _backend(self) -> NativeTerminalBackend:
        if self._native_backend is not None:
            return self._native_backend
        if self._platform_name == "posix":
            from .unix_pty import UnixPTYBackend

            self._native_backend = UnixPTYBackend(
                self._containment_manager,
                autodetect_containment=self._autodetect_containment,
                require_containment=self._require_containment,
            )
            self._containment_manager = self._native_backend.containment_manager
            return self._native_backend
        if self._platform_name == "nt":
            from .conpty import ConPTYBackend

            self._native_backend = ConPTYBackend()
            return self._native_backend
        raise RuntimeError(f"native terminals are unsupported on {self._platform_name!r}")

    def _ensure_required_containment_available(
        self,
        backend: NativeTerminalBackend,
    ) -> None:
        if not self._require_containment:
            return
        manager = getattr(backend, "containment_manager", None)
        if self._platform_name != "posix" or manager is None:
            raise ProcessStartError(
                "kernel process containment is required for native terminals, "
                "but this platform/backend has no delegated cgroup v2 boundary"
            )

    def _require_managed(self, session_id: str) -> _ManagedTerminal:
        managed = self._managed.get(session_id)
        if managed is None:
            raise ApplicationConflictError(
                "terminal_not_attached",
                "The native terminal is not attached to this Runner process",
                details={"session_id": session_id},
            )
        return managed

    @staticmethod
    def _require_writer(terminal: TerminalSession, actor: TerminalOwner) -> None:
        if terminal.status is not TerminalStatus.OPEN:
            raise ApplicationConflictError(
                "terminal_not_open",
                f"Terminal {terminal.id!r} is {terminal.status.value}",
            )
        if terminal.owner is not actor:
            raise ApplicationConflictError(
                "terminal_not_owned",
                f"Terminal input belongs to {terminal.owner.value!r}, not {actor.value!r}",
                details={"session_id": terminal.id, "owner": terminal.owner.value},
            )


def _resolve_terminal_executable(
    executable: str,
    environment: dict[str, str],
) -> str | None:
    path = Path(executable)
    if path.is_absolute():
        return str(path.resolve(strict=False))
    return shutil.which(executable, path=environment.get("PATH"))


def _is_explicit_pre_spawn_failure(execution: Execution) -> bool:
    """Return whether durable state proves the native process never existed."""

    return (
        execution.status is ExecutionStatus.FAILED
        and execution.started_at is None
        and execution.process_created_at is None
        and execution.pid is None
        and execution.process_group_id is None
        and execution.containment_id is None
    )


def _is_explicit_pre_spawn_absence(execution: Execution) -> bool:
    """Return whether CANCELLED state itself proves no native handle existed."""

    return (
        execution.status is ExecutionStatus.CANCELLED
        and execution.started_at is None
        and execution.process_created_at is None
        and execution.pid is None
        and execution.process_group_id is None
        and execution.containment_id is None
    )


def _has_no_terminal_dispatch_identity(execution: Execution) -> bool:
    return all(
        value is None
        for value in (
            execution.started_at,
            execution.process_created_at,
            execution.pid,
            execution.process_group_id,
            execution.containment_id,
        )
    )


def _is_terminal_projection_conflict(error: BaseException) -> bool:
    return isinstance(error, RepositoryConflictError) or (
        isinstance(error, RuntimeError) and "terminal session already exists" in str(error)
    )


async def _wait_for_shielded_task(task: asyncio.Task[object]) -> bool:
    """Wait for a side-effect task to settle despite caller cancellation."""

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            break
    return interrupted


def _observe_task_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


def _handle_process_group_id(handle: NativeTerminalHandle) -> int:
    process_group_id = getattr(handle, "process_group_id", None)
    return handle.pid if process_group_id is None else int(process_group_id)


def _handle_containment_identifier(handle: NativeTerminalHandle) -> str | None:
    identifier = getattr(handle, "containment_identifier", None)
    return None if identifier is None else str(identifier)


async def _activate_terminal_handle(handle: NativeTerminalHandle) -> None:
    activate = getattr(handle, "activate", None)
    if activate is not None:
        await activate()


def _read_output_slice(path: Path, cursor: int, max_bytes: int) -> OutputSlice:
    if cursor < 0:
        raise ValueError("output cursor must not be negative")
    if not path.exists():
        return OutputSlice(data=b"", cursor=cursor, next_cursor=cursor, eof=True)
    size = path.stat().st_size
    if cursor > size:
        raise ValueError(f"terminal cursor {cursor} is beyond transcript size {size}")
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


def _output_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0
