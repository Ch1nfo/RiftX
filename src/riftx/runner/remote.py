"""Control-plane supervisors for routing execution to remote Runner nodes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.ports import ExecutionRepository
from riftx.application.services.nodes import NodeApplicationService
from riftx.application.services.runner_control import RunnerControlService
from riftx.domain import (
    RUNNER_STOP_ACK_EXECUTION_SCHEMA,
    Execution,
    ExecutionStatus,
    ExecutorType,
    NodeStatus,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandStatus,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
)
from riftx.domain.base import new_id, utc_now

from .models import ExecutionLaunchRequest, ExecutionOutput, OutputSlice
from .paths import RunnerPaths
from .protocols import EffectGuard, ExecutionCloser, ExecutionRunner

_TERMINAL_EXECUTION_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
    ExecutionStatus.LOST,
}
_PHYSICAL_STOP_PROOF_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
}


class RemoteExecutionSupervisor:
    """Persists central execution state and dispatches it over the Runner channel."""

    def __init__(
        self,
        repository: ExecutionRepository,
        paths: RunnerPaths,
        control: RunnerControlService,
        nodes: NodeApplicationService,
        *,
        poll_interval_seconds: float = 0.25,
        cancel_ack_timeout_seconds: float = 5.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if cancel_ack_timeout_seconds <= 0:
            raise ValueError("cancel_ack_timeout_seconds must be positive")
        self._repository = repository
        self._paths = paths
        self._control = control
        self._nodes = nodes
        self._poll_interval_seconds = poll_interval_seconds
        self._cancel_ack_timeout_seconds = cancel_ack_timeout_seconds

    async def start(
        self,
        request: ExecutionLaunchRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> Execution:
        owner = await self._control.current_principal(request.node_id)
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
            owner=owner,
            executor_type=request.executor_type,
            argv=request.argv,
            command_text=request.command_text,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            stdout_path=str(output_paths.stdout),
            stderr_path=str(output_paths.stderr),
            status=(
                ExecutionStatus.QUEUED
                if request.session_id is not None
                else ExecutionStatus.CREATED
            ),
        )
        # Persist STARTING before the final Run fence and before enqueue.  A
        # concurrent stop can therefore never miss an admitted remote effect.
        execution.transition_to(ExecutionStatus.STARTING)
        execution, created = await self._repository.create_if_absent(execution)
        if not created:
            _require_remote_owner(execution)
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
        try:
            await self._control.enqueue(
                request.node_id,
                kind=RunnerCommandKind.EXECUTE,
                idempotency_key=f"execute:{request.execution_key}",
                target=owner,
                run_id=request.run_id,
                origin=RunnerCommandOrigin.APPLICATION_SERVICE,
                operation_family=RunnerOperationFamily.EXECUTION,
                resource_kind=RunnerResourceKind.EXECUTION,
                resource_id=execution.id,
                execution_id=execution.id,
                output_contract=RunnerOutputContract(
                    max_output_bytes=100_000_000,
                    allowed_streams=("stderr", "stdout"),
                    result_schema="riftx.runner-result/execution-start/v1",
                ),
                payload={
                    "execution_id": execution.id,
                    "request": request.model_copy(
                        update={
                            "execution_id": execution.id,
                            "runner_principal": owner,
                        }
                    ).model_dump(mode="json"),
                },
            )
            # Enqueue atomically binds the central Execution to the exact
            # launch command/effect envelope.  Never return or later save the
            # stale pre-enqueue object, which would omit those callback facts.
            execution = await self.get(execution.id)
        except Exception:
            await self._mark_start_lost(execution.id)
            raise
        try:
            if effect_guard is not None:
                await effect_guard()
        except BaseException:
            # Dispatch is delivery-ambiguous.  Persist a CANCEL tombstone and
            # leave the execution non-terminal until the Runner acknowledges
            # physical termination.
            try:
                await self.cancel(execution.id)
            except Exception:
                await self._mark_start_lost(execution.id)
            raise
        return execution

    async def _cancel_unstarted(self, execution: Execution) -> Execution:
        current = await self.get(execution.id)
        if current.status is not ExecutionStatus.STARTING:
            return current
        current.transition_to(ExecutionStatus.CANCELLED)
        current, _ = await self._repository.save_if_status(
            current,
            expected={ExecutionStatus.STARTING},
        )
        return current

    async def _mark_start_lost(self, execution_id: str) -> Execution:
        current = await self.get(execution_id)
        if current.status is not ExecutionStatus.STARTING:
            return current
        current.transition_to(ExecutionStatus.LOST)
        current, _ = await self._repository.save_if_status(
            current,
            expected={ExecutionStatus.STARTING},
        )
        return current

    async def get(self, execution_id: str) -> Execution:
        execution = await self._repository.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution

    async def wait(self, execution_id: str) -> Execution:
        while True:
            execution = await self.get(execution_id)
            if execution.status in _TERMINAL_EXECUTION_STATUSES:
                return execution
            node = await self._nodes.get(execution.node_id)
            if node.status is NodeStatus.LOST:
                return await self._mark_lost_if_active(execution)
            await asyncio.sleep(self._poll_interval_seconds)

    async def cancel(self, execution_id: str) -> Execution:
        execution = await self.get(execution_id)
        owner = _require_remote_owner(execution)
        # A terminal status reported by the Control Plane is not proof that a
        # remote process has stopped. In particular, LOST/FAILED can be written
        # after a disconnect while the Runner process is still alive. Persist a
        # CANCEL tombstone for every status except an already acknowledged
        # cancellation so a reconnecting Runner cannot start or keep it alive.
        cancel_ack_required = _remote_cancel_ack_required(execution)
        if not cancel_ack_required:
            return execution
        command, _ = await self._control.enqueue(
            execution.node_id,
            kind=RunnerCommandKind.CANCEL,
            # A failed safety command must be re-enqueueable. Each request is
            # independently idempotent at the Runner through its cancellation
            # journal, so do not permanently reuse a failed command row.
            idempotency_key=f"cancel:{execution.id}:{new_id()}",
            target=owner,
            run_id=execution.run_id,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=RunnerOperationFamily.SAFETY_STOP,
            resource_kind=RunnerResourceKind.EXECUTION,
            resource_id=execution.id,
            execution_id=execution.id,
            output_contract=RunnerOutputContract(
                result_schema="riftx.runner-result/execution-stop/v1",
                stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
            ),
            payload={
                "execution_id": execution.id,
                "execution_key": execution.execution_key,
            },
        )
        completed = await self._control.wait_command(
            command.id,
            timeout_seconds=self._cancel_ack_timeout_seconds,
            poll_interval_seconds=min(self._poll_interval_seconds, 0.1),
        )
        if completed.status is not RunnerCommandStatus.COMPLETED:
            raise RuntimeError(
                f"Remote execution cancellation failed: {completed.error or 'unknown Runner error'}"
            )
        if completed.result.get("execution_id") != execution.id:
            raise RuntimeError("Remote cancellation acknowledgement execution ID mismatch")
        if completed.result.get("local_execution_id") != execution.id:
            raise RuntimeError("Remote cancellation acknowledgement local execution ID mismatch")
        if completed.result.get("execution_key") != execution.execution_key:
            raise RuntimeError("Remote cancellation acknowledgement execution key mismatch")
        if completed.result.get("owner") != owner.model_dump(mode="json"):
            raise RuntimeError("Remote cancellation acknowledgement owner mismatch")
        if completed.result.get("status") != ExecutionStatus.CANCELLED.value:
            raise RuntimeError("Remote cancellation acknowledgement status mismatch")
        if completed.result.get("physical_stop_confirmed") is not True:
            raise RuntimeError("Remote cancellation did not confirm physical process stop")
        return await self._persist_acknowledged_stop(execution_id, owner)

    async def _persist_acknowledged_stop(
        self,
        execution_id: str,
        owner: RunnerPrincipal,
    ) -> Execution:
        execution = await self.get(execution_id)
        for _ in range(8):
            if _require_remote_owner(execution) != owner:
                raise RuntimeError("Remote execution owner changed after cancellation ACK")
            if (
                execution.status in _PHYSICAL_STOP_PROOF_STATUSES
                and execution.physical_stop_confirmed_at is not None
            ):
                return execution
            expected_status = execution.status
            candidate = execution.model_copy(deep=True)
            if candidate.status not in _PHYSICAL_STOP_PROOF_STATUSES:
                if not candidate.can_transition_to(ExecutionStatus.CANCELLED):
                    raise RuntimeError(
                        "Remote cancellation ACK cannot be reconciled with execution "
                        f"status {candidate.status.value!r}"
                    )
                candidate.transition_to(
                    ExecutionStatus.CANCELLED,
                    exit_code=candidate.exit_code,
                )
            # A late cancellation can race a natural terminal outcome. The ACK
            # proves physical absence, but COMPLETED/EXITED/HARD_TIMEOUT are
            # already truthful outcomes and cannot be rewritten to CANCELLED.
            candidate.physical_stop_confirmed_at = utc_now()
            execution, saved = await self._repository.save_if_status(
                candidate,
                expected={expected_status},
            )
            if saved:
                return execution
        raise RuntimeError(
            f"Remote cancellation proof for execution {execution_id!r} could not be persisted"
        )

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
            if execution.status not in {
                ExecutionStatus.STARTING,
                ExecutionStatus.RUNNING,
            }:
                continue
            node = await self._nodes.get(execution.node_id)
            if node.status is NodeStatus.LOST:
                execution = await self._mark_lost_if_active(execution)
            recovered.append(execution)
        return recovered

    async def _mark_lost_if_active(self, execution: Execution) -> Execution:
        if execution.status not in {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
        }:
            return execution
        expected = execution.status
        execution.transition_to(ExecutionStatus.LOST)
        execution, _ = await self._repository.save_if_status(
            execution,
            expected={expected},
        )
        return execution

    async def close(self, *, cancel_running: bool = False) -> None:
        if cancel_running:
            active = await self._repository.list_active()
            await asyncio.gather(*(self.cancel(item.id) for item in active))


class NodeExecutionRouter:
    """Routes one execution contract to the local or remote implementation."""

    def __init__(
        self,
        *,
        local_node_id: str,
        repository: ExecutionRepository,
        local: ExecutionRunner,
        remote: ExecutionRunner,
        local_terminal: ExecutionCloser,
    ) -> None:
        self._local_node_id = local_node_id
        self._repository = repository
        self._local = local
        self._remote = remote
        self._local_terminal = local_terminal

    async def start(
        self,
        request: ExecutionLaunchRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> Execution:
        return await self._runner_for_node(request.node_id).start(
            request,
            effect_guard=effect_guard,
        )

    async def get(self, execution_id: str) -> Execution:
        execution = await self._require(execution_id)
        return await self._runner_for_node(execution.node_id).get(execution_id)

    async def wait(self, execution_id: str) -> Execution:
        execution = await self._require(execution_id)
        return await self._runner_for_node(execution.node_id).wait(execution_id)

    async def cancel(self, execution_id: str) -> Execution:
        execution = await self._require(execution_id)
        if execution.node_id == self._local_node_id and execution.executor_type is ExecutorType.PTY:
            return await self._local_terminal.close_execution(execution_id)
        return await self._runner_for_node(execution.node_id).cancel(execution_id)

    async def read_output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ExecutionOutput:
        execution = await self._require(execution_id)
        return await self._runner_for_node(execution.node_id).read_output(
            execution_id,
            stdout_cursor=stdout_cursor,
            stderr_cursor=stderr_cursor,
            max_bytes=max_bytes,
        )

    def _runner_for_node(self, node_id: str) -> ExecutionRunner:
        return self._local if node_id == self._local_node_id else self._remote

    async def _require(self, execution_id: str) -> Execution:
        execution = await self._repository.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution


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


def _remote_cancel_ack_required(execution: Execution) -> bool:
    if (
        execution.status in _PHYSICAL_STOP_PROOF_STATUSES
        and execution.physical_stop_confirmed_at is not None
    ):
        return False
    if execution.status is not ExecutionStatus.CANCELLED:
        return True
    # A pre-dispatch guard can safely persist CANCELLED without a Runner ACK.
    # Once any process/start identity exists, even an old CANCELLED row must be
    # re-audited by the owning Runner.
    return any(
        value is not None
        for value in (
            execution.pid,
            execution.process_group_id,
            execution.containment_id,
            execution.process_created_at,
            execution.started_at,
        )
    )


def _require_remote_owner(execution: Execution) -> RunnerPrincipal:
    if execution.owner is None:
        raise ApplicationConflictError(
            "remote_execution_owner_missing",
            f"Remote execution {execution.id!r} has no bound Runner owner",
            details={
                "execution_id": execution.id,
                "execution_key": execution.execution_key,
                "node_id": execution.node_id,
            },
        )
    return execution.owner
