"""Engagement-scoped durable facts and attack relationships."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from riftx.context import EvidenceSource
from riftx.domain.base import DomainModel, new_id, utc_now


class EngagementFactStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class FactRelationType(StrEnum):
    DISCOVERED_ON = "discovered_on"
    EXPLOITS = "exploits"
    ENABLES = "enables"
    DEPENDS_ON = "depends_on"
    LEADS_TO = "leads_to"


class FactPromotionCandidate(DomainModel):
    id: str = Field(default_factory=new_id)
    engagement_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    source_session_id: str | None = None
    working_fact_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: JsonValue
    natural_language: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    evidence_sources: dict[str, EvidenceSource]
    source_execution_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    user_confirmed: bool = False

    @model_validator(mode="after")
    def validate_evidence(self) -> FactPromotionCandidate:
        object.__setattr__(
            self,
            "evidence_refs",
            list(dict.fromkeys(self.evidence_refs)),
        )
        if set(self.evidence_sources) != set(self.evidence_refs):
            raise ValueError("Fact Promotion evidence sources must cover every reference")
        if self.valid_until is not None and self.valid_from is not None:
            if self.valid_until <= self.valid_from:
                raise ValueError("Fact validity end must be after its start")
        return self

    @property
    def rule_eligible(self) -> bool:
        return any(
            source in {EvidenceSource.DETERMINISTIC_PARSER, EvidenceSource.USER_DECISION}
            for source in self.evidence_sources.values()
        )


class EngagementFact(DomainModel):
    id: str = Field(default_factory=new_id)
    engagement_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: JsonValue
    natural_language: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    source_run_ids: list[str] = Field(min_length=1)
    source_session_ids: list[str] = Field(default_factory=list)
    source_execution_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    supersedes_fact_id: str | None = None
    status: EngagementFactStatus = EngagementFactStatus.ACTIVE
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class FactRelation(DomainModel):
    id: str = Field(default_factory=new_id)
    engagement_id: str = Field(min_length=1)
    source_fact_id: str = Field(min_length=1)
    target_fact_id: str = Field(min_length=1)
    relation_type: FactRelationType
    evidence_refs: list[str] = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    source_session_id: str | None = None
    source_execution_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    valid_until: AwareDatetime | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def reject_self_relation(self) -> FactRelation:
        if self.source_fact_id == self.target_fact_id:
            raise ValueError("Fact Relation cannot point to itself")
        object.__setattr__(
            self,
            "evidence_refs",
            list(dict.fromkeys(self.evidence_refs)),
        )
        return self


class AttackGraph(DomainModel):
    engagement_id: str = Field(min_length=1)
    facts: list[EngagementFact] = Field(default_factory=list)
    relations: list[FactRelation] = Field(default_factory=list)

    def successors(self, fact_id: str) -> list[EngagementFact]:
        target_ids = {
            relation.target_fact_id
            for relation in self.relations
            if relation.source_fact_id == fact_id
        }
        return [fact for fact in self.facts if fact.id in target_ids]
