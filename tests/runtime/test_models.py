from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from riftx.domain import ApprovalLevel, ApprovalStatus
from riftx.runtime.types import (
    AgentCycle,
    AgentDirectiveType,
    AgentSession,
    AgentStep,
    AgentStepType,
    ApprovalDecision,
    ProviderState,
    RunLease,
    RuntimeApprovalRequest,
    ToolCallIntent,
    UserInputRequest,
    UserInputStatus,
    YieldReason,
)


def test_runtime_models_round_trip_through_json() -> None:
    now = datetime(2026, 7, 30, tzinfo=UTC)
    models = [
        AgentSession(
            id="session-1",
            run_id="run-1",
            agent_type="primary",
            model_profile="default",
            created_at=now,
        ),
        AgentCycle(
            id="cycle-1",
            run_id="run-1",
            session_id="session-1",
            sequence=1,
            yield_reason=YieldReason.TOOL_RUNNING,
        ),
        AgentStep(
            id="step-1",
            cycle_id="cycle-1",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
            input_refs=["artifact://input"],
            output_refs=["artifact://output"],
        ),
        ToolCallIntent(
            id="intent-1",
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            step_id="step-1",
            tool_id="nmap",
            arguments={"targets": ["192.0.2.1"]},
            command_preview="nmap 192.0.2.1",
            reason="Authorized discovery",
            approval_level=ApprovalLevel.SENSITIVE,
            execution_spec={"argv": ["nmap", "192.0.2.1"]},
            created_at=now,
        ),
        RuntimeApprovalRequest(
            id="approval-1",
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            tool_call_intent_id="intent-1",
            working_memory_version=2,
            created_at=now,
        ),
        UserInputRequest(
            id="input-1",
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            prompt="Which target should be tested next?",
            created_at=now,
        ),
        ProviderState(
            id="provider-1",
            session_id="session-1",
            provider="openai",
            model="gpt-5.6",
            engine_type="openai-agents",
            engine_version="0.19",
            state={"conversation_id": "conversation-1"},
            previous_response_id="response-1",
            created_at=now,
        ),
        RunLease(
            run_id="run-1",
            owner_id="worker-1",
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(minutes=1),
        ),
    ]

    for model in models:
        restored = type(model).model_validate_json(model.model_dump_json())
        assert restored == model


def test_tool_call_intent_requires_tool_or_skill() -> None:
    with pytest.raises(ValidationError, match="requires tool_id or skill_id"):
        ToolCallIntent(
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            step_id="step-1",
        )


def test_runtime_approval_decisions_and_user_input_transitions() -> None:
    approval = RuntimeApprovalRequest(
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        tool_call_intent_id="intent-1",
    )
    approval.decide(
        ApprovalDecision.REJECT_WITH_FEEDBACK,
        decided_by="operator",
        feedback="Use the authorized staging host instead.",
    )
    assert approval.status is ApprovalStatus.REJECTED
    assert approval.decision is ApprovalDecision.REJECT_WITH_FEEDBACK

    request = UserInputRequest(
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        prompt="Continue?",
    )
    request.answer("message-1")
    assert request.status is UserInputStatus.ANSWERED
    assert request.response_message_id == "message-1"


def test_reject_with_feedback_requires_feedback() -> None:
    approval = RuntimeApprovalRequest(
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        tool_call_intent_id="intent-1",
    )
    with pytest.raises(ValueError, match="requires feedback"):
        approval.decide(
            ApprovalDecision.REJECT_WITH_FEEDBACK,
            decided_by="operator",
        )


def test_run_lease_rejects_invalid_expiry() -> None:
    now = datetime(2026, 7, 30, tzinfo=UTC)
    with pytest.raises(ValidationError, match="expires_at must be later"):
        RunLease(
            run_id="run-1",
            owner_id="worker-1",
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now,
        )


def test_directive_contract_is_provider_neutral() -> None:
    assert {directive.value for directive in AgentDirectiveType} == {
        "respond",
        "call_tool",
        "run_shell",
        "open_terminal",
        "delegate",
        "update_plan",
        "ask_user",
        "create_finding",
        "complete_run",
    }
