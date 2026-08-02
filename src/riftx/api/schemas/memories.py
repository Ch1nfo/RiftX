"""Long-term Memory API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from riftx.memory import (
    CreateMemory,
    MemoryAuthor,
    MemoryRecord,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
)


class CreateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_type: MemoryType
    scope_type: MemoryScopeType
    scope_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    retrieval_keywords: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    source_refs: list[str] = Field(min_length=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    supersedes: str | None = None
    pinned: bool = False

    def to_command(self) -> CreateMemory:
        return CreateMemory(**self.model_dump())


class UpdateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_type: MemoryType | None = None
    scope_type: MemoryScopeType | None = None
    scope_id: str | None = Field(default=None, min_length=1)
    title: str | None = Field(default=None, min_length=1)
    content: str | None = Field(default=None, min_length=1)
    summary: str | None = Field(default=None, min_length=1)
    retrieval_keywords: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    source_refs: list[str] | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    def changes(self) -> dict[str, object]:
        return {
            name: value
            for name, value in self.model_dump().items()
            if name in self.model_fields_set
        }


class PinMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool = True


class MemoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    memory_type: MemoryType
    scope_type: MemoryScopeType
    scope_id: str
    title: str
    content: str
    summary: str
    retrieval_keywords: list[str]
    confidence: float
    importance: float
    source_refs: list[str]
    valid_from: datetime | None
    valid_until: datetime | None
    supersedes: str | None
    status: MemoryStatus
    pinned: bool
    created_by: MemoryAuthor
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, memory: MemoryRecord) -> MemoryResponse:
        return cls.model_validate(memory)


class MemoryListResponse(BaseModel):
    items: list[MemoryResponse]
