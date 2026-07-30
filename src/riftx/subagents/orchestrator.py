"""Bounded parallel execution for independently scheduled Subagent tasks."""

from __future__ import annotations

import asyncio
from typing import Protocol

from .manager import SubagentHandle, SubagentManager
from .models import DelegationPacket, SubagentResult, SubagentStatus


class SubagentTaskRunner(Protocol):
    async def run(self, handle: SubagentHandle) -> SubagentResult: ...


class SubagentOrchestrator:
    def __init__(self, manager: SubagentManager, runner: SubagentTaskRunner) -> None:
        self._manager = manager
        self._runner = runner

    async def execute_many(
        self,
        *,
        parent_session_id: str,
        delegations: list[DelegationPacket],
    ) -> list[SubagentResult]:
        handles = [
            await self._manager.start(
                parent_session_id=parent_session_id,
                delegation=delegation,
            )
            for delegation in delegations
        ]
        return list(await asyncio.gather(*(self._execute(handle) for handle in handles)))

    async def _execute(self, handle: SubagentHandle) -> SubagentResult:
        try:
            result = await asyncio.wait_for(
                self._runner.run(handle),
                timeout=handle.delegation.timeout_seconds,
            )
        except TimeoutError:
            result = SubagentResult(
                task_id=handle.delegation.task_id,
                status=SubagentStatus.CANCELLED,
                summary="Subagent timed out before completing its delegated task.",
                unresolved_questions=[handle.delegation.task],
            )
        except Exception as exc:
            result = SubagentResult(
                task_id=handle.delegation.task_id,
                status=SubagentStatus.FAILED,
                summary=f"Subagent failed: {type(exc).__name__}",
                failed_approaches=[str(exc)],
                unresolved_questions=[handle.delegation.task],
            )
        return await self._manager.complete(handle.session.id, result)
