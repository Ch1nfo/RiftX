"""Provider-neutral Subagent delegation and result packet contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from riftx.context import FactCandidate, HypothesisUpdate
from riftx.domain import FindingSeverity
from riftx.domain.base import DomainModel, new_id
from riftx.memory import MemoryCandidate


class SubagentStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DelegationPacket(DomainModel):
    task_id: str = Field(default_factory=new_id)
    subagent_type: str = Field(min_length=1)
    task: str = Field(min_length=1)
    expected_output_schema: dict[str, object] = Field(default_factory=dict)
    run_contract_summary: str = Field(min_length=1)
    relevant_scope: list[str] = Field(min_length=1)
    selected_fact_ids: list[str] = Field(default_factory=list)
    selected_artifact_refs: list[str] = Field(default_factory=list)
    selected_memory_ids: list[str] = Field(default_factory=list)
    available_tool_ids: list[str] = Field(default_factory=list)
    workspace: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    max_turns: int = Field(default=10, ge=1, le=100)
    max_tool_calls: int = Field(default=12, ge=0, le=1000)
    token_budget: int = Field(default=16_000, ge=256)
    timeout_seconds: float = Field(default=900, gt=0)

    @field_validator(
        "relevant_scope",
        "selected_fact_ids",
        "selected_artifact_refs",
        "selected_memory_ids",
        "available_tool_ids",
        "constraints",
        "stop_conditions",
    )
    @classmethod
    def normalize_unique_values(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class FindingCandidate(DomainModel):
    title: str = Field(min_length=1)
    severity: FindingSeverity
    affected_assets: list[str] = Field(default_factory=list)
    description: str = ""
    evidence_refs: list[str] = Field(min_length=1)
    reproduction_steps: list[str] = Field(default_factory=list)
    impact: str = ""
    recommendation: str = ""
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("affected_assets", "evidence_refs", "reproduction_steps")
    @classmethod
    def normalize_unique_values(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class PrimaryMergePacket(DomainModel):
    """The complete allowlist of data that may enter the Primary context."""

    task_id: str = Field(min_length=1)
    status: SubagentStatus
    summary: str = Field(min_length=1)
    confirmed_fact_candidates: list[FactCandidate] = Field(default_factory=list)
    hypothesis_updates: list[HypothesisUpdate] = Field(default_factory=list)
    finding_candidates: list[FindingCandidate] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)


class SubagentResult(DomainModel):
    task_id: str = Field(min_length=1)
    status: SubagentStatus
    summary: str = Field(min_length=1)
    confirmed_fact_candidates: list[FactCandidate] = Field(default_factory=list)
    hypothesis_updates: list[HypothesisUpdate] = Field(default_factory=list)
    finding_candidates: list[FindingCandidate] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    failed_approaches: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)
    memory_candidates: list[MemoryCandidate] = Field(default_factory=list)

    @field_validator(
        "evidence_refs",
        "artifact_refs",
        "failed_approaches",
        "unresolved_questions",
        "recommended_next_actions",
    )
    @classmethod
    def normalize_unique_values(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def require_evidence_for_candidates(self) -> SubagentResult:
        candidate_refs = {
            ref
            for candidate in self.confirmed_fact_candidates
            for ref in candidate.source_refs
        }
        finding_refs = {
            ref for candidate in self.finding_candidates for ref in candidate.evidence_refs
        }
        known_refs = set(self.evidence_refs) | set(self.artifact_refs)
        missing = (candidate_refs | finding_refs) - known_refs
        if missing:
            raise ValueError(
                f"Subagent Result candidate evidence is not declared: {sorted(missing)!r}"
            )
        return self

    def primary_packet(self) -> PrimaryMergePacket:
        return PrimaryMergePacket(
            task_id=self.task_id,
            status=self.status,
            summary=self.summary,
            confirmed_fact_candidates=self.confirmed_fact_candidates,
            hypothesis_updates=self.hypothesis_updates,
            finding_candidates=self.finding_candidates,
            evidence_refs=list(dict.fromkeys([*self.evidence_refs, *self.artifact_refs])),
            recommended_next_actions=self.recommended_next_actions,
        )
