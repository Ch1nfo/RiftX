"""Execution inspection, output streaming, and cancellation."""

from __future__ import annotations

import secrets
from collections.abc import Sequence

from riftx.application.errors import EntityNotFoundError, resource_not_accessible
from riftx.application.ports import ExecutionRepository, RunEventRepository, RunRepository
from riftx.domain import Execution
from riftx.execution import ExecutionWaitResult
from riftx.execution.waiting import wait_for_execution
from riftx.runner import ExecutionOutput, ExecutionRunner

from .runs import require_interactive_run_operation


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
        if not secrets.compare_digest(execution_id, execution.id):
            raise resource_not_accessible()
        return execution

    async def resolve_run_id(self, execution_id: str) -> str:
        run_id = await self._execution_repository.get_run_id(execution_id)
        if run_id is None:
            raise resource_not_accessible()
        return run_id

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

    async def list_active(self) -> Sequence[Execution]:
        return await self._execution_repository.list_active()

    async def cancel(self, execution_id: str) -> Execution:
        execution = await self.get(execution_id)
        run = await self._run_repository.get(execution.run_id)
        if run is None:
            raise EntityNotFoundError("Run", execution.run_id)
        require_interactive_run_operation(run)
        cancelled = await self._runner.cancel(execution.id)
        await self._event_repository.append(
            cancelled.run_id,
            "execution.cancel_requested",
            {"execution_id": cancelled.id},
        )
        return cancelled

    async def wait(
        self,
        execution_id: str,
        *,
        timeout_seconds: float = 30.0,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
        next_poll_after_seconds: int = 10,
        expected_run_id: str | None = None,
    ) -> ExecutionWaitResult:
        execution = await self.get(execution_id)
        _require_execution_binding(
            execution,
            expected_execution_id=execution_id,
            expected_run_id=expected_run_id,
        )
        result = await wait_for_execution(
            self._runner,
            execution,
            timeout_seconds=timeout_seconds,
            stdout_cursor=stdout_cursor,
            stderr_cursor=stderr_cursor,
            max_bytes=max_bytes,
            next_poll_after_seconds=next_poll_after_seconds,
            validate_execution=(
                None
                if expected_run_id is None
                else lambda current: _require_execution_binding(
                    current,
                    expected_execution_id=execution_id,
                    expected_run_id=expected_run_id,
                )
            ),
        )
        return result

    async def output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
        expected_run_id: str | None = None,
    ) -> ExecutionOutput:
        execution = await self.get(execution_id)
        _require_execution_binding(
            execution,
            expected_execution_id=execution_id,
            expected_run_id=expected_run_id,
        )
        return await self._runner.read_output(
            execution_id,
            stdout_cursor=stdout_cursor,
            stderr_cursor=stderr_cursor,
            max_bytes=max_bytes,
        )


def _require_execution_binding(
    execution: Execution,
    *,
    expected_execution_id: str,
    expected_run_id: str | None,
) -> None:
    if not secrets.compare_digest(expected_execution_id, execution.id) or (
        expected_run_id is not None
        and not secrets.compare_digest(expected_run_id, execution.run_id)
    ):
        raise resource_not_accessible()
