"""Application service that assembles authoritative Observer snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
from typing import Protocol

from riftx.application.ports import RunEventRepository, ToolCallIntentRepository
from riftx.context import WorkingMemoryRepository
from riftx.reasoning import ReasoningGraphRepository
from riftx.runtime.lifecycle import CycleLimits
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    RuntimeApprovalRequest,
    UserInputRequest,
)
from riftx.tasks import TaskGraphRepository

from .models import SupervisorReport, SupervisorSnapshot
from .supervisor import ObserverSupervisor


class PendingApprovalReader(Protocol):
    async def pending_for_run(self, run_id: str) -> list[RuntimeApprovalRequest]: ...


class PendingUserInputReader(Protocol):
    async def pending_for_session(
        self,
        run_id: str,
        session_id: str,
    ) -> UserInputRequest | None: ...


class ActiveTakeoverReader(Protocol):
    async def active_for_run(self, run_id: str, *, limit: int = 100) -> Sequence[str]: ...


class ObserverSupervisorApplicationService:
    def __init__(
        self,
        *,
        working_memory: WorkingMemoryRepository,
        task_graphs: TaskGraphRepository,
        reasoning_graphs: ReasoningGraphRepository,
        tool_intents: ToolCallIntentRepository,
        approvals: PendingApprovalReader,
        user_input: PendingUserInputReader,
        events: RunEventRepository,
        takeovers: ActiveTakeoverReader,
        supervisor: ObserverSupervisor | None = None,
    ) -> None:
        self._working_memory = working_memory
        self._task_graphs = task_graphs
        self._reasoning_graphs = reasoning_graphs
        self._tool_intents = tool_intents
        self._approvals = approvals
        self._user_input = user_input
        self._events = events
        self._takeovers = takeovers
        self._supervisor = supervisor or ObserverSupervisor()

    async def inspect(
        self,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        limits: CycleLimits,
        elapsed_seconds: float,
        available_tool_ids: Collection[str],
        available_skill_ids: Collection[str] = (),
    ) -> SupervisorReport:
        cognitive_reads = asyncio.gather(
            self._working_memory.get_for_run(session.run_id),
            self._task_graphs.get(session.run_id),
            self._reasoning_graphs.get(session.run_id),
            self._tool_intents.recent_for_session(session.id, limit=100),
        )
        runtime_reads = asyncio.gather(
            self._approvals.pending_for_run(session.run_id),
            self._user_input.pending_for_session(session.run_id, session.id),
            self._events.list_recent(session.run_id, limit=100),
            self._takeovers.active_for_run(session.run_id, limit=100),
        )
        (
            (working_memory, task_graph, reasoning_graph, tool_intents),
            (approvals, pending_user_input, events, takeover_refs),
        ) = await asyncio.gather(cognitive_reads, runtime_reads)
        snapshot = SupervisorSnapshot(
            run_id=session.run_id,
            session=session,
            cycle=cycle,
            limits=limits,
            elapsed_seconds=elapsed_seconds,
            recent_events=tuple(events),
            recent_tool_intents=tuple(tool_intents),
            pending_approvals=tuple(
                approval for approval in approvals if approval.session_id == session.id
            ),
            pending_user_input=pending_user_input,
            working_memory=working_memory,
            task_graph=task_graph,
            reasoning_graph=reasoning_graph,
            available_tool_ids=tuple(sorted(set(available_tool_ids))),
            available_skill_ids=tuple(sorted(set(available_skill_ids))),
            active_takeover_refs=tuple(sorted(set(takeover_refs))),
        )
        return self._supervisor.inspect(snapshot)
