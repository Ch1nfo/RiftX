from __future__ import annotations

from riftx.context import AttemptRecord, AttemptStatus, WorkingMemory
from riftx.domain import ApprovalStatus, RunEvent
from riftx.observer import (
    ObserverSupervisor,
    SupervisorDisposition,
    SupervisorSnapshot,
)
from riftx.runtime.lifecycle import CycleLimits
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    RuntimeApprovalRequest,
    ToolCallIntent,
    ToolCallStatus,
    UserInputRequest,
    YieldReason,
)


def session() -> AgentSession:
    return AgentSession(id="session-1", run_id="run-1", model_profile="test")


def cycle(*, model_calls: int = 0, tool_calls: int = 0) -> AgentCycle:
    return AgentCycle(
        id="cycle-1",
        run_id="run-1",
        session_id="session-1",
        sequence=1,
        model_call_count=model_calls,
        tool_call_count=tool_calls,
    )


def intent(
    intent_id: str,
    *,
    tool_id: str = "target_http_request",
    status: ToolCallStatus = ToolCallStatus.READY,
) -> ToolCallIntent:
    return ToolCallIntent(
        id=intent_id,
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        step_id=f"step-{intent_id}",
        tool_id=tool_id,
        arguments={"url": "https://example.com/admin"},
        engine_call_id=f"call-{intent_id}",
    ).model_copy(update={"status": status})


def snapshot(**updates: object) -> SupervisorSnapshot:
    payload: dict[str, object] = {
        "run_id": "run-1",
        "session": session(),
        "cycle": cycle(),
        "limits": CycleLimits(),
        "available_tool_ids": ("target_http_request",),
    }
    payload.update(updates)
    return SupervisorSnapshot.model_validate(payload)


def test_clean_snapshot_continues() -> None:
    report = ObserverSupervisor().inspect(snapshot())

    assert report.disposition is SupervisorDisposition.CONTINUE
    assert report.signals == ()


def test_scope_and_capability_mismatch_block() -> None:
    report = ObserverSupervisor().inspect(
        snapshot(
            recent_events=(
                RunEvent(
                    run_id="run-1",
                    sequence=1,
                    event_type="runtime.control_tool_failed",
                    payload={"error_code": "reasoning_evidence_session_mismatch"},
                ),
            ),
            recent_tool_intents=(intent("intent-1", tool_id="unavailable-tool"),),
        )
    )

    assert report.disposition is SupervisorDisposition.BLOCK
    assert {signal.code for signal in report.signals} == {
        "scope_boundary_rejected",
        "tool_capability_mismatch",
    }


def test_waiting_intent_requires_durable_approval_and_yields() -> None:
    waiting = intent("intent-1", status=ToolCallStatus.WAITING_APPROVAL)
    missing = ObserverSupervisor().inspect(
        snapshot(recent_tool_intents=(waiting,))
    )
    assert missing.disposition is SupervisorDisposition.BLOCK
    assert missing.signals[0].code == "approval_record_missing"

    pending = RuntimeApprovalRequest(
        id="approval-1",
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        tool_call_intent_id="intent-1",
        status=ApprovalStatus.PENDING,
    )
    report = ObserverSupervisor().inspect(
        snapshot(recent_tool_intents=(waiting,), pending_approvals=(pending,))
    )
    assert report.disposition is SupervisorDisposition.YIELD
    assert report.yield_reason is YieldReason.APPROVAL_REQUIRED


def test_invalid_retry_lineage_blocks() -> None:
    failed = AttemptRecord(
        id="attempt-1",
        action_signature="probe:v1",
        target="https://example.com",
        tool_id="target_http_request",
        normalized_arguments={"path": "/admin"},
        result_status=AttemptStatus.FAILED,
        result_summary="Failed",
        retryable=True,
    )
    duplicate = failed.model_copy(update={"id": "attempt-2"})
    report = ObserverSupervisor().inspect(
        snapshot(working_memory=WorkingMemory(run_id="run-1", attempts=[failed, duplicate]))
    )

    assert report.disposition is SupervisorDisposition.BLOCK
    assert report.signals[0].code == "duplicate_attempt_retry_invalid"


def test_budget_and_repeated_call_loop_yield() -> None:
    repeated = tuple(intent(f"intent-{index}") for index in range(3))
    report = ObserverSupervisor().inspect(
        snapshot(
            cycle=cycle(tool_calls=12),
            recent_tool_intents=repeated,
        )
    )

    assert report.disposition is SupervisorDisposition.YIELD
    assert report.yield_reason is YieldReason.CYCLE_LIMIT_REACHED
    assert {signal.code for signal in report.signals} == {
        "cycle_budget_reached",
        "repeated_tool_call_loop",
    }


def test_user_input_precedes_human_takeover_yield() -> None:
    report = ObserverSupervisor().inspect(
        snapshot(
            pending_user_input=UserInputRequest(
                id="input-1",
                run_id="run-1",
                session_id="session-1",
                cycle_id="cycle-1",
                prompt="Choose the authorized account",
            ),
            active_takeover_refs=("browser:browser-1",),
        )
    )

    assert report.disposition is SupervisorDisposition.YIELD
    assert report.yield_reason is YieldReason.USER_INPUT_REQUIRED
    assert {signal.code for signal in report.signals} == {
        "user_input_pending",
        "human_takeover_active",
    }
