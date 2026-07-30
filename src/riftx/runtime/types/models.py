"""Infrastructure-independent durable Agent Runtime models."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, model_validator

from riftx.domain import ApprovalLevel, ApprovalStatus
from riftx.domain.base import DomainModel, new_id, utc_now

from .enums import (
    AgentStepType,
    ApprovalDecision,
    CycleStatus,
    SessionStatus,
    StepStatus,
    ToolCallStatus,
    UserInputStatus,
    YieldReason,
)


class AgentSession(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    parent_session_id: str | None = None
    agent_type: str = Field(default="primary", min_length=1)
    model_profile: str = Field(min_length=1)
    status: SessionStatus = SessionStatus.CREATED
    latest_checkpoint_id: str | None = None
    provider_state_id: str | None = None
    turn_count: int = Field(default=0, ge=0)
    model_call_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    closed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def reject_self_parent(self) -> AgentSession:
        if self.parent_session_id == self.id:
            raise ValueError("agent session cannot be its own parent")
        return self


class AgentCycle(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    status: CycleStatus = CycleStatus.CREATED
    yield_reason: YieldReason | None = None
    waiting_object_id: str | None = None
    checkpoint_id: str | None = None
    model_call_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    @property
    def model_calls(self) -> int:
        return self.model_call_count

    @property
    def tool_calls(self) -> int:
        return self.tool_call_count


class AgentStep(DomainModel):
    id: str = Field(default_factory=new_id)
    cycle_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    step_type: AgentStepType
    status: StepStatus = StepStatus.CREATED
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


class ToolCallIntent(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    tool_id: str | None = None
    skill_id: str | None = None
    arguments: dict[str, object] = Field(default_factory=dict)
    command_preview: str = ""
    reason: str = ""
    target_summary: str | None = None
    approval_level: ApprovalLevel = ApprovalLevel.SENSITIVE
    status: ToolCallStatus = ToolCallStatus.PROPOSED
    engine_call_id: str | None = None
    execution_spec: dict[str, object] | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_tool_or_skill(self) -> ToolCallIntent:
        if self.tool_id is None and self.skill_id is None:
            raise ValueError("tool call intent requires tool_id or skill_id")
        return self

    @property
    def agent_step_id(self) -> str:
        return self.step_id


class RuntimeApprovalRequest(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    tool_call_intent_id: str = Field(min_length=1)
    context_compilation_id: str | None = None
    working_memory_version: int | None = Field(default=None, ge=1)
    provider_state_id: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: ApprovalDecision | None = None
    feedback: str | None = None
    decided_by: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    decided_at: AwareDatetime | None = None

    def decide(
        self,
        decision: ApprovalDecision,
        *,
        decided_by: str,
        feedback: str | None = None,
    ) -> None:
        if self.status is not ApprovalStatus.PENDING:
            raise ValueError("runtime approval request is already decided")
        if decision is ApprovalDecision.REJECT_WITH_FEEDBACK and not feedback:
            raise ValueError("reject_with_feedback requires feedback")
        self.decision = decision
        self.status = (
            ApprovalStatus.APPROVED
            if decision in {
                ApprovalDecision.APPROVE_ONCE,
                ApprovalDecision.APPROVE_TOOL_FOR_RUN,
            }
            else ApprovalStatus.REJECTED
        )
        self.feedback = feedback
        self.decided_by = decided_by
        self.decided_at = utc_now()


class UserInputRequest(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    context_compilation_id: str | None = None
    working_memory_version: int | None = Field(default=None, ge=1)
    provider_state_id: str | None = None
    status: UserInputStatus = UserInputStatus.WAITING
    response_message_id: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    answered_at: AwareDatetime | None = None

    def answer(self, message_id: str) -> None:
        if self.status is not UserInputStatus.WAITING:
            raise ValueError("user input request is already resolved")
        if not message_id:
            raise ValueError("response message ID must not be empty")
        self.status = UserInputStatus.ANSWERED
        self.response_message_id = message_id
        self.answered_at = utc_now()


class ProviderState(DomainModel):
    id: str = Field(default_factory=new_id)
    session_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    engine_type: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    state: dict[str, object] = Field(default_factory=dict)
    previous_response_id: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)


class RunLease(DomainModel):
    run_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    acquired_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime
    heartbeat_at: AwareDatetime = Field(default_factory=utc_now)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_timestamps(self) -> RunLease:
        if self.heartbeat_at < self.acquired_at:
            raise ValueError("lease heartbeat_at cannot precede acquired_at")
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("lease expires_at must be later than heartbeat_at")
        return self
