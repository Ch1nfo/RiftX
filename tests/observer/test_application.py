from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from riftx.domain import ApprovalStatus, RunEvent
from riftx.observer import ObserverSupervisorApplicationService, SupervisorDisposition
from riftx.runtime.lifecycle import CycleLimits
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    RuntimeApprovalRequest,
    ToolCallIntent,
    ToolCallStatus,
    YieldReason,
)


async def test_application_collects_authoritative_state_and_filters_approvals() -> None:
    session = AgentSession(id="session-1", run_id="run-1", model_profile="test")
    cycle = AgentCycle(
        id="cycle-1",
        run_id="run-1",
        session_id=session.id,
        sequence=1,
    )
    intent = ToolCallIntent(
        id="intent-1",
        run_id="run-1",
        session_id=session.id,
        cycle_id=cycle.id,
        step_id="step-1",
        tool_id="target_http_request",
    ).model_copy(update={"status": ToolCallStatus.WAITING_APPROVAL})
    approval = RuntimeApprovalRequest(
        id="approval-1",
        run_id="run-1",
        session_id=session.id,
        cycle_id=cycle.id,
        tool_call_intent_id=intent.id,
        status=ApprovalStatus.PENDING,
    )
    other_approval = approval.model_copy(
        update={"id": "approval-other", "session_id": "session-other"}
    )
    event = RunEvent(run_id="run-1", sequence=1, event_type="runtime.started")
    working_memory = SimpleNamespace(get_for_run=AsyncMock(return_value=None))
    task_graphs = SimpleNamespace(get=AsyncMock(return_value=None))
    reasoning_graphs = SimpleNamespace(get=AsyncMock(return_value=None))
    tool_intents = SimpleNamespace(
        recent_for_session=AsyncMock(return_value=[intent])
    )
    approvals = SimpleNamespace(
        pending_for_run=AsyncMock(return_value=[approval, other_approval])
    )
    user_input = SimpleNamespace(pending_for_session=AsyncMock(return_value=None))
    events = SimpleNamespace(list_recent=AsyncMock(return_value=[event]))
    takeovers = SimpleNamespace(active_for_run=AsyncMock(return_value=[]))
    service = ObserverSupervisorApplicationService(
        working_memory=working_memory,
        task_graphs=task_graphs,
        reasoning_graphs=reasoning_graphs,
        tool_intents=tool_intents,
        approvals=approvals,
        user_input=user_input,
        events=events,
        takeovers=takeovers,
    )

    report = await service.inspect(
        session=session,
        cycle=cycle,
        limits=CycleLimits(),
        elapsed_seconds=1.5,
        available_tool_ids=["target_http_request", "target_http_request"],
    )

    assert report.disposition is SupervisorDisposition.YIELD
    assert report.yield_reason is YieldReason.APPROVAL_REQUIRED
    assert report.signals[0].refs == ("approval:approval-1",)
    tool_intents.recent_for_session.assert_awaited_once_with(session.id, limit=100)
    events.list_recent.assert_awaited_once_with(session.run_id, limit=100)
    takeovers.active_for_run.assert_awaited_once_with(session.run_id, limit=100)
