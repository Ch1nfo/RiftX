"""Structured findings produced during a run."""

from pydantic import AwareDatetime, Field

from .base import DomainModel, new_id, utc_now
from .enums import FindingSeverity, FindingStatus


class FindingEvidence(DomainModel):
    artifact_id: str | None = None
    execution_id: str | None = None
    description: str = ""
    location: str | None = None


class Finding(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    title: str = Field(min_length=1)
    severity: FindingSeverity
    status: FindingStatus = FindingStatus.DRAFT
    affected_assets: list[str] = Field(default_factory=list)
    description: str = ""
    evidence: list[FindingEvidence] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    impact: str = ""
    recommendation: str = ""
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
