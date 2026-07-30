"""Reconcile durable Execution state after Runner or Worker restarts."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import ExecutionRepository, NodeRepository, RunEventRepository
from riftx.domain import Execution, ExecutionStatus, ExecutorType, NodeStatus
from riftx.runner import ProcessInspector

_RECONCILABLE_STATUSES = {ExecutionStatus.STARTING, ExecutionStatus.RUNNING}
_TERMINAL_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
    ExecutionStatus.LOST,
}
_ONLINE_RUNNER_STATUSES = {NodeStatus.ONLINE, NodeStatus.DEGRADED}


class ExecutionReconciler:
    """Confirm active process identity without restarting the command."""

    def __init__(
        self,
        *,
        execution_repository: ExecutionRepository,
        local_node_id: str,
        process_inspector: ProcessInspector | None = None,
        node_repository: NodeRepository | None = None,
        event_repository: RunEventRepository | None = None,
        max_concurrency: int = 16,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._executions = execution_repository
        self._local_node_id = local_node_id
        self._processes = process_inspector or ProcessInspector()
        self._nodes = node_repository
        self._events = event_repository
        self._max_concurrency = max_concurrency

    async def reconcile_run(self, run_id: str) -> Sequence[Execution]:
        executions = await self._executions.list(run_id, limit=1000)
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def reconcile(item: Execution) -> Execution:
            async with semaphore:
                return await self._reconcile(item)

        return await asyncio.gather(*(reconcile(item) for item in executions))

    async def reconcile_execution(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return await self._reconcile(execution)

    async def _reconcile(self, execution: Execution) -> Execution:
        if execution.status in _TERMINAL_STATUSES:
            await self._record(execution, "terminal_unchanged")
            return execution
        if execution.executor_type is ExecutorType.PTY:
            await self._record(execution, "pty_recovery_deferred")
            return execution
        if execution.status not in _RECONCILABLE_STATUSES:
            await self._record(execution, "status_unchanged")
            return execution

        if execution.node_id != self._local_node_id:
            runner_online = await self._runner_is_online(execution.node_id)
            if runner_online:
                await self._record(execution, "remote_runner_online")
                return execution
            return await self._mark_lost(execution, "runner_unavailable")

        if await self._processes.matches(execution):
            await self._record(execution, "process_identity_matched")
            return execution
        return await self._mark_lost(execution, "process_identity_mismatch")

    async def _runner_is_online(self, node_id: str) -> bool:
        if self._nodes is None:
            return False
        node = await self._nodes.get(node_id)
        return node is not None and node.status in _ONLINE_RUNNER_STATUSES

    async def _mark_lost(self, execution: Execution, reason: str) -> Execution:
        execution.transition_to(ExecutionStatus.LOST)
        saved = await self._executions.save(execution)
        await self._record(saved, reason)
        return saved

    async def _record(self, execution: Execution, outcome: str) -> None:
        if self._events is None:
            return
        await self._events.append(
            execution.run_id,
            "execution.reconciled",
            {
                "execution_id": execution.id,
                "runner_id": execution.node_id,
                "status": execution.status.value,
                "outcome": outcome,
                "pid": execution.pid,
                "process_created_at": (
                    execution.process_created_at.isoformat()
                    if execution.process_created_at is not None
                    else None
                ),
                "command_summary": execution.command_text or " ".join(execution.argv),
            },
        )
