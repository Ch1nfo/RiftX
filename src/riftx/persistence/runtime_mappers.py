"""Mappings for the durable Agent Runtime persistence records."""

from datetime import datetime

from riftx.domain import ApprovalLevel, ApprovalStatus
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ApprovalDecision,
    CycleStatus,
    ProviderState,
    RunLease,
    RuntimeApprovalRequest,
    SessionStatus,
    StepStatus,
    ToolCallIntent,
    ToolCallStatus,
    UserInputRequest,
    UserInputStatus,
    YieldReason,
)

from .orm import (
    AgentCycleRecord,
    AgentRuntimeStepRecord,
    AgentSessionRecord,
    ProviderStateRecord,
    RunLeaseRecord,
    RuntimeApprovalRequestRecord,
    ToolCallIntentRecord,
    UserInputRequestRecord,
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


def tool_call_intent_to_record(
    intent: ToolCallIntent,
    *,
    updated_at: datetime,
) -> ToolCallIntentRecord:
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
        claimed_execution_key=None,
        claimed_attempt_group=None,
        engine_call_id=intent.engine_call_id,
        execution_spec_json=intent.execution_spec,
        created_at=intent.created_at,
        updated_at=updated_at,
    )


def apply_tool_call_intent_to_record(intent: ToolCallIntent, record: ToolCallIntentRecord) -> None:
    record.engine_call_id = intent.engine_call_id
    record.command_preview = intent.command_preview
    record.reason = intent.reason
    record.target_summary = intent.target_summary
    record.execution_spec_json = intent.execution_spec


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
        execution_spec=record.execution_spec_json,
        created_at=record.created_at,
    )


def runtime_approval_to_record(
    request: RuntimeApprovalRequest,
) -> RuntimeApprovalRequestRecord:
    return RuntimeApprovalRequestRecord(
        id=request.id,
        run_id=request.run_id,
        session_id=request.session_id,
        cycle_id=request.cycle_id,
        tool_call_intent_id=request.tool_call_intent_id,
        context_compilation_id=request.context_compilation_id,
        working_memory_version=request.working_memory_version,
        provider_state_id=request.provider_state_id,
        status=request.status.value,
        decision=request.decision.value if request.decision is not None else None,
        feedback=request.feedback,
        decided_by=request.decided_by,
        created_at=request.created_at,
        decided_at=request.decided_at,
    )


def runtime_approval_from_record(
    record: RuntimeApprovalRequestRecord,
) -> RuntimeApprovalRequest:
    return RuntimeApprovalRequest(
        id=record.id,
        run_id=record.run_id,
        session_id=record.session_id,
        cycle_id=record.cycle_id,
        tool_call_intent_id=record.tool_call_intent_id,
        context_compilation_id=record.context_compilation_id,
        working_memory_version=record.working_memory_version,
        provider_state_id=record.provider_state_id,
        status=ApprovalStatus(record.status),
        decision=ApprovalDecision(record.decision) if record.decision is not None else None,
        feedback=record.feedback,
        decided_by=record.decided_by,
        created_at=record.created_at,
        decided_at=record.decided_at,
    )


def user_input_request_to_record(request: UserInputRequest) -> UserInputRequestRecord:
    return UserInputRequestRecord(
        id=request.id,
        run_id=request.run_id,
        session_id=request.session_id,
        cycle_id=request.cycle_id,
        prompt=request.prompt,
        context_compilation_id=request.context_compilation_id,
        working_memory_version=request.working_memory_version,
        provider_state_id=request.provider_state_id,
        status=request.status.value,
        response_message_id=request.response_message_id,
        created_at=request.created_at,
        answered_at=request.answered_at,
    )


def apply_user_input_request_to_record(
    request: UserInputRequest,
    record: UserInputRequestRecord,
) -> None:
    record.provider_state_id = request.provider_state_id
    record.status = request.status.value
    record.response_message_id = request.response_message_id
    record.answered_at = request.answered_at


def user_input_request_from_record(record: UserInputRequestRecord) -> UserInputRequest:
    return UserInputRequest(
        id=record.id,
        run_id=record.run_id,
        session_id=record.session_id,
        cycle_id=record.cycle_id,
        prompt=record.prompt,
        context_compilation_id=record.context_compilation_id,
        working_memory_version=record.working_memory_version,
        provider_state_id=record.provider_state_id,
        status=UserInputStatus(record.status),
        response_message_id=record.response_message_id,
        created_at=record.created_at,
        answered_at=record.answered_at,
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
