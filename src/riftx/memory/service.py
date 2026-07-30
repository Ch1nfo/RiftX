"""Manual Memory management and deterministic first-stage keyword retrieval."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Protocol

from pydantic import AwareDatetime

from riftx.application.errors import EntityNotFoundError
from riftx.domain.base import utc_now

from .models import (
    MemoryRecord,
    MemoryRetrievalScope,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
)

_WORD = re.compile(r"[\w.-]+", re.UNICODE)
_EDITABLE_FIELDS = {
    "memory_type",
    "scope_type",
    "scope_id",
    "title",
    "content",
    "summary",
    "retrieval_keywords",
    "confidence",
    "importance",
    "source_refs",
    "valid_from",
    "valid_until",
}


class MemoryRepository(Protocol):
    async def create(self, memory: MemoryRecord) -> MemoryRecord: ...

    async def get(self, memory_id: str) -> MemoryRecord | None: ...

    async def save(self, memory: MemoryRecord) -> MemoryRecord: ...

    async def list_all(self) -> list[MemoryRecord]: ...

    async def supersede(self, memory: MemoryRecord) -> MemoryRecord: ...


@dataclass(frozen=True, slots=True)
class CreateMemory:
    memory_type: MemoryType
    scope_type: MemoryScopeType
    scope_id: str
    title: str
    content: str
    summary: str
    source_refs: list[str]
    retrieval_keywords: list[str] = field(default_factory=list)
    confidence: float = 1.0
    importance: float = 0.5
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    supersedes: str | None = None
    pinned: bool = False


class MemoryService:
    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository

    async def create(self, command: CreateMemory) -> MemoryRecord:
        memory = MemoryRecord.model_validate(asdict(command))
        if memory.supersedes is not None:
            return await self._repository.supersede(memory)
        return await self._repository.create(memory)

    async def get(self, memory_id: str) -> MemoryRecord:
        memory = await self._repository.get(memory_id)
        if memory is None:
            raise EntityNotFoundError("Memory", memory_id)
        return memory

    async def update(
        self,
        memory_id: str,
        changes: Mapping[str, object],
    ) -> MemoryRecord:
        memory = await self.get(memory_id)
        if memory.status is not MemoryStatus.ACTIVE:
            raise ValueError("only active Memory can be edited")
        unknown = set(changes) - _EDITABLE_FIELDS
        if unknown:
            raise ValueError(f"unsupported Memory fields: {sorted(unknown)!r}")
        candidate = MemoryRecord.model_validate(
            {**memory.model_dump(), **dict(changes), "updated_at": utc_now()}
        )
        return await self._repository.save(candidate)

    async def delete(self, memory_id: str) -> MemoryRecord:
        memory = await self.get(memory_id)
        if memory.status is MemoryStatus.DELETED:
            return memory
        memory.status = MemoryStatus.DELETED
        memory.pinned = False
        return await self._repository.save(memory)

    async def pin(self, memory_id: str, *, pinned: bool = True) -> MemoryRecord:
        memory = await self.get(memory_id)
        if memory.status is not MemoryStatus.ACTIVE:
            raise ValueError("only active Memory can be pinned")
        memory.pinned = pinned
        return await self._repository.save(memory)

    async def list(
        self,
        *,
        scope: MemoryRetrievalScope | None = None,
        include_inactive: bool = False,
        at: AwareDatetime | None = None,
    ) -> list[MemoryRecord]:
        memories = await self._repository.list_all()
        if scope is not None:
            memories = [item for item in memories if scope.allows(item)]
        if not include_inactive:
            memories = [item for item in memories if item.is_current(at=at)]
        return memories

    async def retrieve(
        self,
        query: str,
        *,
        scope: MemoryRetrievalScope,
        limit: int = 10,
        at: AwareDatetime | None = None,
    ) -> list[MemoryRecord]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        try:
            candidates = await self.list(scope=scope, at=at)
        except Exception:
            return []
        query_terms = _terms(query)
        ranked: list[tuple[tuple[float, ...], MemoryRecord]] = []
        for memory in candidates:
            overlap = len(query_terms & _memory_terms(memory))
            if overlap == 0 and not memory.pinned:
                continue
            score = (
                1.0 if memory.pinned else 0.0,
                float(overlap),
                memory.importance,
                memory.confidence,
                memory.updated_at.timestamp(),
            )
            ranked.append((score, memory))
        ranked.sort(key=lambda item: (item[0], item[1].id), reverse=True)
        return [memory for _, memory in ranked[:limit]]


def _terms(value: str) -> set[str]:
    return {match.group(0).casefold() for match in _WORD.finditer(value)}


def _memory_terms(memory: MemoryRecord) -> set[str]:
    values: Sequence[str] = (
        memory.title,
        memory.summary,
        memory.content,
        *memory.retrieval_keywords,
    )
    return {term for value in values for term in _terms(value)}
