"""Typed, durable Working Memory contracts for a single Run."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from riftx.domain.base import DomainModel, new_id, utc_now


class EvidenceSource(StrEnum):
    MODEL_INFERENCE = "model_inference"
    USER_DECISION = "user_decision"
    DETERMINISTIC_PARSER = "deterministic_parser"


class FactStatus(StrEnum):
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    INVESTIGATING = "investigating"
    SUPPORTED = "supported"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    STALE = "stale"


class PlanItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CurrentFocus(DomainModel):
    phase: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    plan_item_id: str | None = None
    notes: list[str] = Field(default_factory=list)


class PlanItem(DomainModel):
    id: str = Field(default_factory=new_id)
    task: str = Field(min_length=1)
    status: PlanItemStatus = PlanItemStatus.PENDING
    sequence: int = Field(ge=1)
    completion_summary: str | None = None


class RunPlan(DomainModel):
    items: list[PlanItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_items_and_sequences(self) -> RunPlan:
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Run Plan item IDs must be unique")
        sequences = [item.sequence for item in self.items]
        if len(sequences) != len(set(sequences)):
            raise ValueError("Run Plan item sequences must be unique")
        return self


class ConfirmedFact(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: JsonValue
    natural_language: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    status: FactStatus = FactStatus.CONFIRMED
    source_refs: list[str] = Field(min_length=1)
    source_types: dict[str, EvidenceSource] = Field(default_factory=dict)
    first_observed_at: AwareDatetime = Field(default_factory=utc_now)
    last_confirmed_at: AwareDatetime = Field(default_factory=utc_now)
    supersedes_fact_id: str | None = None

    @model_validator(mode="after")
    def align_sources(self) -> ConfirmedFact:
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("Confirmed Fact source refs must be unique")
        if set(self.source_types) != set(self.source_refs):
            raise ValueError("Confirmed Fact source types must cover every source ref")
        if self.supersedes_fact_id == self.id:
            raise ValueError("Confirmed Fact cannot supersede itself")
        return self


class Hypothesis(DomainModel):
    id: str = Field(default_factory=new_id)
    statement: str = Field(min_length=1)
    confidence: float = Field(default=0.25, ge=0.0, le=1.0)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    supporting_fact_ids: list[str] = Field(default_factory=list)
    contradicting_fact_ids: list[str] = Field(default_factory=list)
    next_validation_action: str | None = None

    @model_validator(mode="after")
    def reject_ambiguous_evidence(self) -> Hypothesis:
        overlap = set(self.supporting_fact_ids) & set(self.contradicting_fact_ids)
        if overlap:
            raise ValueError(f"Hypothesis evidence cannot both support and contradict: {overlap}")
        return self


class AttemptRecord(DomainModel):
    id: str = Field(default_factory=new_id)
    action_signature: str = Field(min_length=1)
    target: str = Field(min_length=1)
    tool_id: str = Field(min_length=1)
    normalized_arguments: dict[str, JsonValue] = Field(default_factory=dict)
    result_status: AttemptStatus
    result_summary: str = Field(min_length=1)
    retryable: bool = False
    retry_of_attempt_id: str | None = None
    retry_reason: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_retry_reason(self) -> AttemptRecord:
        if self.retry_of_attempt_id is not None and not self.retry_reason:
            raise ValueError("Retry attempts require a retry reason")
        return self


class UserDecision(DomainModel):
    id: str = Field(default_factory=new_id)
    question: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    reason: str | None = None
    source_ref: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)


class PendingQuestion(DomainModel):
    id: str = Field(default_factory=new_id)
    question: str = Field(min_length=1)
    owner: str | None = None
    blocking: bool = False
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ActiveExecutionRef(DomainModel):
    execution_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    summary: str | None = None


class ActiveTerminalRef(DomainModel):
    terminal_session_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    purpose: str | None = None


class NextAction(DomainModel):
    description: str = Field(min_length=1)
    tool_id: str | None = None
    skill_id: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str | None = None


class WorkingMemory(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    current_focus: CurrentFocus | None = None
    run_plan: RunPlan = Field(default_factory=RunPlan)
    confirmed_facts: list[ConfirmedFact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    attempts: list[AttemptRecord] = Field(default_factory=list)
    user_decisions: list[UserDecision] = Field(default_factory=list)
    pending_questions: list[PendingQuestion] = Field(default_factory=list)
    active_executions: list[ActiveExecutionRef] = Field(default_factory=list)
    active_terminals: list[ActiveTerminalRef] = Field(default_factory=list)
    pending_approvals: list[str] = Field(default_factory=list)
    next_action: NextAction | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_consistent_state(self) -> WorkingMemory:
        fact_ids = [fact.id for fact in self.confirmed_facts]
        hypothesis_ids = [hypothesis.id for hypothesis in self.hypotheses]
        attempt_ids = [attempt.id for attempt in self.attempts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("Working Memory fact IDs must be unique")
        if len(hypothesis_ids) != len(set(hypothesis_ids)):
            raise ValueError("Working Memory hypothesis IDs must be unique")
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("Working Memory attempt IDs must be unique")
        if any(fact.run_id != self.run_id for fact in self.confirmed_facts):
            raise ValueError("Working Memory facts must belong to the same Run")
        return self


class PlanItemUpdate(DomainModel):
    item_id: str = Field(min_length=1)
    task: str | None = None
    status: PlanItemStatus | None = None
    sequence: int | None = Field(default=None, ge=1)
    completion_summary: str | None = None
    reopen_reason: str | None = None


class PlanUpdateProposal(DomainModel):
    item_updates: list[PlanItemUpdate] = Field(default_factory=list)
    current_focus: CurrentFocus | None = None
    next_action: NextAction | None = None


class FactCandidate(DomainModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: JsonValue
    natural_language: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    source_refs: list[str] = Field(min_length=1)
    source_type: EvidenceSource = EvidenceSource.MODEL_INFERENCE
    observed_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_unique_sources(self) -> FactCandidate:
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("Fact Candidate source refs must be unique")
        return self


class HypothesisEvidenceEffect(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class HypothesisUpdate(DomainModel):
    hypothesis_id: str = Field(default_factory=new_id)
    statement: str | None = None
    evidence_effect: HypothesisEvidenceEffect
    fact_ids: list[str] = Field(min_length=1)
    initial_confidence: float = Field(default=0.25, ge=0.0, le=1.0)
    next_validation_action: str | None = None

    @model_validator(mode="after")
    def require_unique_fact_ids(self) -> HypothesisUpdate:
        if len(self.fact_ids) != len(set(self.fact_ids)):
            raise ValueError("Hypothesis Update fact IDs must be unique")
        return self


@runtime_checkable
class WorkingMemoryRepository(Protocol):
    async def create(self, memory: WorkingMemory) -> WorkingMemory: ...

    async def get(self, memory_id: str) -> WorkingMemory | None: ...

    async def get_for_run(self, run_id: str) -> WorkingMemory | None: ...

    async def save(self, memory: WorkingMemory, *, expected_version: int) -> WorkingMemory: ...
