from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from riftx.domain import ApprovalLevel
from riftx.runtime.types import (
    AgentCycle,
    AgentDirectiveType,
    AgentSession,
    AgentStep,
    AgentStepType,
    ProviderState,
    RunLease,
    ToolCallIntent,
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
