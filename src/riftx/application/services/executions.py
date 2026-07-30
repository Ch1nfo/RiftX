"""Execution inspection, output streaming, and cancellation."""

from __future__ import annotations

from collections.abc import Sequence

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import ExecutionRepository, RunEventRepository, RunRepository
from riftx.domain import Execution
from riftx.runner import ExecutionOutput, ExecutionRunner


class ExecutionApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        execution_repository: ExecutionRepository,
        event_repository: RunEventRepository,
        runner: ExecutionRunner,
    ) -> None:
        self._run_repository = run_repository
        self._execution_repository = execution_repository
        self._event_repository = event_repository
        self._runner = runner

    async def get(self, execution_id: str) -> Execution:
        execution = await self._execution_repository.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution

    async def list(
        self,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Execution]:
        if await self._run_repository.get(run_id) is None:
            raise EntityNotFoundError("Run", run_id)
        return await self._execution_repository.list(run_id, limit=limit, offset=offset)

    async def cancel(self, execution_id: str) -> Execution:
        execution = await self.get(execution_id)
        cancelled = await self._runner.cancel(execution.id)
        await self._event_repository.append(
            cancelled.run_id,
            "execution.cancel_requested",
            {"execution_id": cancelled.id},
        )
        return cancelled

    async def output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ExecutionOutput:
        await self.get(execution_id)
        return await self._runner.read_output(
            execution_id,
            stdout_cursor=stdout_cursor,
            stderr_cursor=stderr_cursor,
            max_bytes=max_bytes,
        )
