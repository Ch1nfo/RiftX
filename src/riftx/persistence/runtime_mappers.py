"""Mappings for the durable Agent Runtime persistence records."""

from riftx.domain import ApprovalLevel
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    CycleStatus,
    ProviderState,
    RunLease,
    SessionStatus,
    StepStatus,
    ToolCallIntent,
    ToolCallStatus,
    YieldReason,
)

from .orm import (
    AgentCycleRecord,
    AgentRuntimeStepRecord,
    AgentSessionRecord,
    ProviderStateRecord,
    RunLeaseRecord,
    ToolCallIntentRecord,
)


def agent_session_to_record(session: AgentSession) -> AgentSessionRecord:
    return AgentSessionRecord(
        id=session.id,
        run_id=session.run_id,
        parent_session_id=session.parent_session_id,
        agent_type=session.agent_type,
        model_profile=session.model_profile,
        status=session.status.value,
        latest_checkpoint_id=session.latest_checkpoint_id,
        provider_state_id=session.provider_state_id,
        turn_count=session.turn_count,
        model_call_count=session.model_call_count,
        tool_call_count=session.tool_call_count,
        created_at=session.created_at,
        closed_at=session.closed_at,
    )


def apply_agent_session_to_record(session: AgentSession, record: AgentSessionRecord) -> None:
    record.parent_session_id = session.parent_session_id
    record.agent_type = session.agent_type
    record.model_profile = session.model_profile
    record.status = session.status.value
    record.latest_checkpoint_id = session.latest_checkpoint_id
    record.provider_state_id = session.provider_state_id
    record.turn_count = session.turn_count
    record.model_call_count = session.model_call_count
    record.tool_call_count = session.tool_call_count
    record.closed_at = session.closed_at


def agent_session_from_record(record: AgentSessionRecord) -> AgentSession:
    return AgentSession(
        id=record.id,
        run_id=record.run_id,
        parent_session_id=record.parent_session_id,
        agent_type=record.agent_type,
        model_profile=record.model_profile,
        status=SessionStatus(record.status),
        latest_checkpoint_id=record.latest_checkpoint_id,
        provider_state_id=record.provider_state_id,
        turn_count=record.turn_count,
        model_call_count=record.model_call_count,
        tool_call_count=record.tool_call_count,
        created_at=record.created_at,
        closed_at=record.closed_at,
    )


def agent_cycle_to_record(cycle: AgentCycle) -> AgentCycleRecord:
    return AgentCycleRecord(
        id=cycle.id,
        run_id=cycle.run_id,
        session_id=cycle.session_id,
        sequence=cycle.sequence,
        status=cycle.status.value,
        yield_reason=cycle.yield_reason.value if cycle.yield_reason else None,
        waiting_object_id=cycle.waiting_object_id,
        checkpoint_id=cycle.checkpoint_id,
        model_call_count=cycle.model_call_count,
        tool_call_count=cycle.tool_call_count,
        started_at=cycle.started_at,
        finished_at=cycle.finished_at,
    )


def apply_agent_cycle_to_record(cycle: AgentCycle, record: AgentCycleRecord) -> None:
    record.status = cycle.status.value
    record.yield_reason = cycle.yield_reason.value if cycle.yield_reason else None
    record.waiting_object_id = cycle.waiting_object_id
    record.checkpoint_id = cycle.checkpoint_id
    record.model_call_count = cycle.model_call_count
    record.tool_call_count = cycle.tool_call_count
    record.started_at = cycle.started_at
    record.finished_at = cycle.finished_at


def agent_cycle_from_record(record: AgentCycleRecord) -> AgentCycle:
    return AgentCycle(
        id=record.id,
        run_id=record.run_id,
        session_id=record.session_id,
        sequence=record.sequence,
        status=CycleStatus(record.status),
        yield_reason=YieldReason(record.yield_reason) if record.yield_reason else None,
        waiting_object_id=record.waiting_object_id,
        checkpoint_id=record.checkpoint_id,
        model_call_count=record.model_call_count,
        tool_call_count=record.tool_call_count,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def agent_step_to_record(step: AgentStep) -> AgentRuntimeStepRecord:
    return AgentRuntimeStepRecord(
        id=step.id,
        cycle_id=step.cycle_id,
        sequence=step.sequence,
        step_type=step.step_type.value,
        status=step.status.value,
        input_refs_json=step.input_refs,
        output_refs_json=step.output_refs,
        started_at=step.started_at,
        finished_at=step.finished_at,
    )


def apply_agent_step_to_record(step: AgentStep, record: AgentRuntimeStepRecord) -> None:
    record.step_type = step.step_type.value
    record.status = step.status.value
    record.input_refs_json = step.input_refs
    record.output_refs_json = step.output_refs
    record.started_at = step.started_at
    record.finished_at = step.finished_at


def agent_step_from_record(record: AgentRuntimeStepRecord) -> AgentStep:
    return AgentStep(
        id=record.id,
        cycle_id=record.cycle_id,
        sequence=record.sequence,
        step_type=AgentStepType(record.step_type),
        status=StepStatus(record.status),
        input_refs=record.input_refs_json,
        output_refs=record.output_refs_json,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def provider_state_to_record(state: ProviderState) -> ProviderStateRecord:
    return ProviderStateRecord(
        id=state.id,
        session_id=state.session_id,
        provider=state.provider,
        model=state.model,
        engine_type=state.engine_type,
        engine_version=state.engine_version,
        state_json=state.state,
        previous_response_id=state.previous_response_id,
        created_at=state.created_at,
    )


def provider_state_from_record(record: ProviderStateRecord) -> ProviderState:
    return ProviderState(
        id=record.id,
        session_id=record.session_id,
        provider=record.provider,
        model=record.model,
        engine_type=record.engine_type,
        engine_version=record.engine_version,
        state=record.state_json,
        previous_response_id=record.previous_response_id,
        created_at=record.created_at,
    )


def tool_call_intent_to_record(intent: ToolCallIntent) -> ToolCallIntentRecord:
    return ToolCallIntentRecord(
        id=intent.id,
        run_id=intent.run_id,
        session_id=intent.session_id,
        cycle_id=intent.cycle_id,
        step_id=intent.step_id,
        tool_id=intent.tool_id,
        skill_id=intent.skill_id,
        arguments_json=intent.arguments,
        command_preview=intent.command_preview,
        reason=intent.reason,
        target_summary=intent.target_summary,
        approval_level=intent.approval_level.value,
        status=intent.status.value,
        engine_call_id=intent.engine_call_id,
        created_at=intent.created_at,
    )


def apply_tool_call_intent_to_record(intent: ToolCallIntent, record: ToolCallIntentRecord) -> None:
    record.status = intent.status.value
    record.engine_call_id = intent.engine_call_id
    record.command_preview = intent.command_preview
    record.reason = intent.reason
    record.target_summary = intent.target_summary


def tool_call_intent_from_record(record: ToolCallIntentRecord) -> ToolCallIntent:
    return ToolCallIntent(
        id=record.id,
        run_id=record.run_id,
        session_id=record.session_id,
        cycle_id=record.cycle_id,
        step_id=record.step_id,
        tool_id=record.tool_id,
        skill_id=record.skill_id,
        arguments=record.arguments_json,
        command_preview=record.command_preview,
        reason=record.reason,
        target_summary=record.target_summary,
        approval_level=ApprovalLevel(record.approval_level),
        status=ToolCallStatus(record.status),
        engine_call_id=record.engine_call_id,
        created_at=record.created_at,
    )


def run_lease_to_record(lease: RunLease) -> RunLeaseRecord:
    return RunLeaseRecord(
        run_id=lease.run_id,
        owner_id=lease.owner_id,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        heartbeat_at=lease.heartbeat_at,
        version=lease.version,
    )


def run_lease_from_record(record: RunLeaseRecord) -> RunLease:
    return RunLease(
        run_id=record.run_id,
        owner_id=record.owner_id,
        acquired_at=record.acquired_at,
        expires_at=record.expires_at,
        heartbeat_at=record.heartbeat_at,
        version=record.version,
    )
