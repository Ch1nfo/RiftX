"""Deterministic Observer Supervisor contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from riftx.context import AttemptRecord, WorkingMemory
from riftx.domain import ApprovalStatus, RunEvent
from riftx.domain.base import DomainModel, utc_now
from riftx.reasoning import ReasoningGraph
from riftx.runtime.lifecycle import CycleLimits
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    RuntimeApprovalRequest,
    ToolCallIntent,
    UserInputRequest,
    YieldReason,
)
from riftx.tasks import TaskGraph


class SupervisorCheck(StrEnum):
    SCOPE = "scope"
    APPROVAL = "approval"
    DUPLICATE_ATTEMPT = "duplicate_attempt"
    EVIDENCE = "evidence"
    CAPABILITY = "capability"
    BUDGET = "budget"
    LOOP = "loop"
    HUMAN_CONTROL = "human_control"


class SupervisorSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class SupervisorDisposition(StrEnum):
    CONTINUE = "continue"
    YIELD = "yield"
    BLOCK = "block"


class SupervisorSignal(DomainModel):
    code: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    check: SupervisorCheck
    severity: SupervisorSeverity
    summary: str = Field(min_length=1, max_length=2_000)
    refs: tuple[str, ...] = Field(default=(), max_length=100)
    yield_reason: YieldReason | None = None


class SupervisorSnapshot(DomainModel):
    run_id: str = Field(min_length=1)
    session: AgentSession
    cycle: AgentCycle
    limits: CycleLimits
    elapsed_seconds: float = Field(default=0, ge=0)
    recent_events: tuple[RunEvent, ...] = Field(default=(), max_length=1_000)
    recent_tool_intents: tuple[ToolCallIntent, ...] = Field(default=(), max_length=1_000)
    pending_approvals: tuple[RuntimeApprovalRequest, ...] = Field(
        default=(), max_length=100
    )
    pending_user_input: UserInputRequest | None = None
    working_memory: WorkingMemory | None = None
    task_graph: TaskGraph | None = None
    reasoning_graph: ReasoningGraph | None = None
    available_tool_ids: tuple[str, ...] = Field(default=(), max_length=1_000)
    available_skill_ids: tuple[str, ...] = Field(default=(), max_length=1_000)
    active_takeover_refs: tuple[str, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def validate_scope(self) -> SupervisorSnapshot:
        if self.session.run_id != self.run_id:
            raise ValueError("Supervisor Session must belong to its Run")
        if self.cycle.run_id != self.run_id or self.cycle.session_id != self.session.id:
            raise ValueError("Supervisor Cycle must belong to its Run and Session")
        if any(event.run_id != self.run_id for event in self.recent_events):
            raise ValueError("Supervisor Events must belong to its Run")
        if any(
            intent.run_id != self.run_id or intent.session_id != self.session.id
            for intent in self.recent_tool_intents
        ):
            raise ValueError("Supervisor Tool Intents must belong to its Run and Session")
        if any(
            approval.run_id != self.run_id or approval.session_id != self.session.id
            for approval in self.pending_approvals
        ):
            raise ValueError("Supervisor Approvals must belong to its Run and Session")
        if any(
            approval.status is not ApprovalStatus.PENDING
            for approval in self.pending_approvals
        ):
            raise ValueError("Supervisor pending Approvals must remain pending")
        if self.pending_user_input is not None and (
            self.pending_user_input.run_id != self.run_id
            or self.pending_user_input.session_id != self.session.id
        ):
            raise ValueError("Supervisor User Input must belong to its Run and Session")
        for aggregate in (self.working_memory, self.task_graph, self.reasoning_graph):
            if aggregate is not None and aggregate.run_id != self.run_id:
                raise ValueError("Supervisor cognitive state must belong to its Run")
        for label, values in (
            ("available Tool IDs", self.available_tool_ids),
            ("available Skill IDs", self.available_skill_ids),
            ("takeover refs", self.active_takeover_refs),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"Supervisor {label} must be unique")
        return self


class SupervisorReport(DomainModel):
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    disposition: SupervisorDisposition
    yield_reason: YieldReason | None = None
    signals: tuple[SupervisorSignal, ...] = Field(default=(), max_length=1_000)
    generated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_disposition(self) -> SupervisorReport:
        if self.disposition is SupervisorDisposition.CONTINUE and (
            self.yield_reason is not None
            or any(signal.severity is SupervisorSeverity.BLOCKING for signal in self.signals)
        ):
            raise ValueError("Continue Supervisor Report cannot carry a stop decision")
        if self.disposition is SupervisorDisposition.YIELD and self.yield_reason is None:
            raise ValueError("Yield Supervisor Report requires a Yield Reason")
        if self.disposition is SupervisorDisposition.BLOCK and self.yield_reason is not None:
            raise ValueError("Blocked Supervisor Report must not carry a Yield Reason")
        return self


def attempt_key(attempt: AttemptRecord) -> tuple[str, str, str, str]:
    """Return the stable identity used by the Working Memory Reducer."""

    import json

    return (
        attempt.action_signature,
        attempt.target,
        attempt.tool_id,
        json.dumps(
            attempt.normalized_arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
