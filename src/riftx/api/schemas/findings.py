"""Finding create, edit, and read schemas."""

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import (
    Finding,
    FindingEvidence,
    FindingSeverity,
    FindingStatus,
)


class CreateFindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    severity: FindingSeverity
    status: FindingStatus = FindingStatus.DRAFT
    affected_assets: list[str] = Field(default_factory=list)
    description: str = ""
    evidence: list[FindingEvidence] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    impact: str = ""
    recommendation: str = ""


class UpdateFindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    severity: FindingSeverity | None = None
    status: FindingStatus | None = None
    affected_assets: list[str] | None = None
    description: str | None = None
    evidence: list[FindingEvidence] | None = None
    reproduction_steps: list[str] | None = None
    impact: str | None = None
    recommendation: str | None = None


class FindingListResponse(BaseModel):
    items: list[Finding]
    limit: int
    offset: int
