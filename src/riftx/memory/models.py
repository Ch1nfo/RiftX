"""Provider-neutral long-term Memory records and deterministic scope matching."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, field_validator, model_validator

from riftx.domain.base import DomainModel, new_id, utc_now


class MemoryType(StrEnum):
    INSTRUCTION = "instruction"
    USER_PREFERENCE = "user_preference"
    PROCEDURAL = "procedural"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"


class MemoryScopeType(StrEnum):
    USER = "user"
    NODE = "node"
    WORKSPACE = "workspace"
    RUN = "run"
    ENGAGEMENT = "engagement"
    ASSET = "asset"
    TOOL = "tool"
    SKILL = "skill"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class MemoryAuthor(StrEnum):
    USER = "user"
    SYSTEM = "system"


class MemoryScope(DomainModel):
    scope_type: MemoryScopeType
    scope_id: str = Field(min_length=1)

    @field_validator("scope_id")
    @classmethod
    def normalize_scope_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("memory scope_id must not be empty")
        return normalized


class MemoryRecord(DomainModel):
    id: str = Field(default_factory=new_id)
    memory_type: MemoryType
    scope_type: MemoryScopeType
    scope_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    retrieval_keywords: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    source_refs: list[str]
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    supersedes: str | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    pinned: bool = False
    created_by: MemoryAuthor = MemoryAuthor.USER
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator(
        "scope_id",
        "title",
        "content",
        "summary",
        mode="before",
    )
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("source_refs", "retrieval_keywords")
    @classmethod
    def normalize_unique_values(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if item and item not in seen:
                normalized.append(item)
                seen.add(item)
        return normalized

    @model_validator(mode="after")
    def validate_lifecycle(self) -> MemoryRecord:
        if not self.source_refs:
            raise ValueError("memory requires at least one source reference")
        if self.valid_from and self.valid_until and self.valid_from >= self.valid_until:
            raise ValueError("memory valid_from must be earlier than valid_until")
        if self.supersedes == self.id:
            raise ValueError("memory cannot supersede itself")
        return self

    def is_current(self, *, at: AwareDatetime | None = None) -> bool:
        moment = at or utc_now()
        return (
            self.status is MemoryStatus.ACTIVE
            and (self.valid_from is None or self.valid_from <= moment)
            and (self.valid_until is None or moment < self.valid_until)
        )

    @property
    def scope(self) -> MemoryScope:
        return MemoryScope(scope_type=self.scope_type, scope_id=self.scope_id)
