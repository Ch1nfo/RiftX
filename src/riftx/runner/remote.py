"""Control-plane supervisors for routing execution to remote Runner nodes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import ExecutionRepository
from riftx.application.services.nodes import NodeApplicationService
from riftx.application.services.runner_control import RunnerControlService
from riftx.domain import Execution, ExecutionStatus, NodeStatus, RunnerCommandKind
from riftx.domain.base import new_id

from .models import ExecutionLaunchRequest, ExecutionOutput, OutputSlice
from .paths import RunnerPaths
from .protocols import ExecutionRunner

_TERMINAL_EXECUTION_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
    ExecutionStatus.LOST,
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
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._repository = repository
        self._paths = paths
        self._control = control
        self._nodes = nodes
        self._poll_interval_seconds = poll_interval_seconds

    async def start(self, request: ExecutionLaunchRequest) -> Execution:
        execution_id = new_id()
        self._paths.ensure_run_layout(request.run_id)
        output_paths = self._paths.execution(request.run_id, execution_id)
        execution = Execution(
            id=execution_id,
            execution_key=request.execution_key,
            run_id=request.run_id,
            session_id=request.session_id,
            tool_call_id=request.tool_call_id,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
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
        execution, created = await self._repository.create_if_absent(execution)
        if not created:
            return execution

        execution.transition_to(ExecutionStatus.STARTING)
        await self._repository.save(execution)
        try:
            await self._control.enqueue(
                request.node_id,
                kind=RunnerCommandKind.EXECUTE,
                idempotency_key=f"execute:{request.execution_key}",
                payload={
                    "execution_id": execution.id,
                    "request": request.model_copy(update={"execution_id": execution.id}).model_dump(
                        mode="json"
                    ),
                },
            )
        except Exception:
            current = await self.get(execution.id)
            if current.status is ExecutionStatus.STARTING:
                current.transition_to(ExecutionStatus.LOST)
                await self._repository.save(current)
            raise
        return execution

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
                execution.transition_to(ExecutionStatus.LOST)
                return await self._repository.save(execution)
            await asyncio.sleep(self._poll_interval_seconds)

    async def cancel(self, execution_id: str) -> Execution:
        execution = await self.get(execution_id)
        if execution.status in _TERMINAL_EXECUTION_STATUSES:
            return execution
        await self._control.enqueue(
            execution.node_id,
            kind=RunnerCommandKind.CANCEL,
            idempotency_key=f"cancel:{execution.id}",
            payload={
                "execution_id": execution.id,
                "execution_key": execution.execution_key,
            },
        )
        return execution

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
            node = await self._nodes.get(execution.node_id)
            if node.status is NodeStatus.LOST:
                execution.transition_to(ExecutionStatus.LOST)
                await self._repository.save(execution)
            recovered.append(execution)
        return recovered

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
    ) -> None:
        self._local_node_id = local_node_id
        self._repository = repository
        self._local = local
        self._remote = remote

    async def start(self, request: ExecutionLaunchRequest) -> Execution:
        return await self._runner_for_node(request.node_id).start(request)

    async def get(self, execution_id: str) -> Execution:
        execution = await self._require(execution_id)
        return await self._runner_for_node(execution.node_id).get(execution_id)

    async def wait(self, execution_id: str) -> Execution:
        execution = await self._require(execution_id)
        return await self._runner_for_node(execution.node_id).wait(execution_id)

    async def cancel(self, execution_id: str) -> Execution:
        execution = await self._require(execution_id)
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
