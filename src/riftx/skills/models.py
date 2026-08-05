"""Progressive file-backed Skill metadata and selected content models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from riftx.capabilities import CapabilitySource
from riftx.domain import ApprovalLevel


class SkillFrontMatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    version: str | int = 1
    source: CapabilitySource = CapabilitySource.OPERATOR
    required_capabilities: list[str] = Field(default_factory=list)
    preferred_tools: list[str] = Field(default_factory=list)
    approval_level: ApprovalLevel = ApprovalLevel.NEVER

    @field_validator("name", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("required_capabilities", "preferred_tools")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in values:
            value = item.strip()
            if value and value not in seen:
                normalized.append(value)
                seen.add(value)
        return normalized


class SkillSummary(BaseModel):
    """The only Skill payload included in initial model context."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(pattern="^[0-9a-f]{64}$")
    source: CapabilitySource
    required_capabilities: list[str] = Field(default_factory=list)


class SkillDocument(SkillSummary):
    """Full SKILL.md content loaded after explicit selection."""

    preferred_tools: list[str] = Field(default_factory=list)
    approval_level: ApprovalLevel
    content: str
    sections: dict[str, str]
    input_schema: dict[str, object] | None = None
    output_schema: dict[str, object] | None = None


class SkillReference(BaseModel):
    """REFERENCES.md content loaded independently from Skill procedure text."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version: str = Field(min_length=1)
    digest: str = Field(pattern="^[0-9a-f]{64}$")
    source: CapabilitySource
    content: str


class SkillSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: SkillSummary
    score: float = Field(ge=0, le=1)
    matched_terms: list[str] = Field(default_factory=list)
