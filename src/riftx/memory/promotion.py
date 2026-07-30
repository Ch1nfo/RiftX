"""Controlled candidate promotion into auditable long-term Memory."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from riftx.application.ports import RunEventRepository
from riftx.domain import DomainError
from riftx.domain.base import DomainModel, new_id, utc_now
from riftx.hooks import HookBus, HookDecision, HookPoint, HookRequest

from .models import MemoryAuthor, MemoryRecord, MemoryScope, MemoryStatus, MemoryType
from .service import MemoryRepository

_SENSITIVE = re.compile(
    r"(?ix)(?:"
    r"\b(?:set-cookie|cookie|authorization|api[_ -]?key|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|token)\b\s*[:=]\s*\S+"
    r"|\bbearer\s+\S+"
    r"|[?&](?:x-amz-signature|x-goog-signature|signature|sig|"
    r"access_token|token)="
    r")"
)


class MemoryCandidateOrigin(StrEnum):
    DETERMINISTIC_PARSER = "deterministic_parser"
    MULTI_SOURCE_CONFIRMATION = "multi_source_confirmation"
    USER_EXPLICIT = "user_explicit"
    CONFIRMED_FINDING = "confirmed_finding"
    STABLE_TOOL_NODE = "stable_tool_node"
    MODEL_INFERENCE = "model_inference"
    UNVERIFIED_VULNERABILITY = "unverified_vulnerability"
    WEB_CONTENT = "web_content"


class PromotionDecision(StrEnum):
    PROMOTE = "promote"
    CANDIDATE_ONLY = "candidate_only"
    REJECT = "reject"


class ConflictAction(StrEnum):
    CREATE = "create"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    IGNORE = "ignore"


class MemoryCandidate(DomainModel):
    id: str = Field(default_factory=new_id)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    suggested_type: MemoryType
    suggested_scope: MemoryScope
    source_refs: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    reason_to_remember: str = Field(min_length=1)
    origin: MemoryCandidateOrigin
    retrieval_keywords: list[str] = Field(default_factory=list)
    valid_until: AwareDatetime | None = None
    conflict_key: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_sources(self) -> MemoryCandidate:
        normalized = list(
            dict.fromkeys(ref.strip() for ref in self.source_refs if ref.strip())
        )
        if not normalized:
            raise ValueError("Memory Candidate requires source references")
        object.__setattr__(self, "source_refs", normalized)
        return self


@dataclass(frozen=True, slots=True)
class PromotionAssessment:
    decision: PromotionDecision
    reason: str


@dataclass(frozen=True, slots=True)
class ConflictResolution:
    action: ConflictAction
    target: MemoryRecord | None = None


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    candidate_id: str
    assessment: PromotionAssessment
    action: ConflictAction | None = None
    memory: MemoryRecord | None = None


class PromotionPolicy:
    def assess(self, candidate: MemoryCandidate) -> PromotionAssessment:
        inspected = "\n".join(
            (
                candidate.title,
                candidate.content,
                candidate.summary,
                *candidate.retrieval_keywords,
                *candidate.source_refs,
            )
        )
        if _SENSITIVE.search(inspected):
            return PromotionAssessment(PromotionDecision.REJECT, "sensitive or signed data")
        if candidate.valid_until is not None and candidate.valid_until <= utc_now():
            return PromotionAssessment(PromotionDecision.REJECT, "candidate already expired")
        if candidate.origin in {
            MemoryCandidateOrigin.MODEL_INFERENCE,
            MemoryCandidateOrigin.UNVERIFIED_VULNERABILITY,
            MemoryCandidateOrigin.WEB_CONTENT,
        }:
            return PromotionAssessment(
                PromotionDecision.CANDIDATE_ONLY,
                f"{candidate.origin.value} cannot auto-promote",
            )
        if (
            candidate.origin is MemoryCandidateOrigin.MULTI_SOURCE_CONFIRMATION
            and len(candidate.source_refs) < 2
        ):
            return PromotionAssessment(
                PromotionDecision.CANDIDATE_ONLY,
                "multi-source promotion requires two independent sources",
            )
        return PromotionAssessment(PromotionDecision.PROMOTE, "trusted promotion source")


class MemoryDeduplicator:
    def find_duplicate(
        self,
        candidate: MemoryCandidate,
        existing: list[MemoryRecord],
    ) -> MemoryRecord | None:
        normalized = _canonical(candidate.content)
        return next(
            (
                memory
                for memory in existing
                if memory.status is MemoryStatus.ACTIVE
                and memory.scope == candidate.suggested_scope
                and memory.memory_type is candidate.suggested_type
                and _canonical(memory.content) == normalized
            ),
            None,
        )


class MemoryConflictResolver:
    def resolve(
        self,
        candidate: MemoryCandidate,
        existing: list[MemoryRecord],
    ) -> ConflictResolution:
        if candidate.conflict_key is None:
            return ConflictResolution(ConflictAction.CREATE)
        target = next(
            (
                memory
                for memory in existing
                if memory.status is MemoryStatus.ACTIVE
                and memory.scope == candidate.suggested_scope
                and candidate.conflict_key in memory.retrieval_keywords
            ),
            None,
        )
        if target is None:
            return ConflictResolution(ConflictAction.CREATE)
        if candidate.confidence >= target.confidence:
            return ConflictResolution(ConflictAction.SUPERSEDE, target)
        return ConflictResolution(ConflictAction.IGNORE, target)


class MemoryWriter:
    def __init__(
        self,
        repository: MemoryRepository,
        *,
        policy: PromotionPolicy | None = None,
        deduplicator: MemoryDeduplicator | None = None,
        conflict_resolver: MemoryConflictResolver | None = None,
        hooks: HookBus | None = None,
        events: RunEventRepository | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy or PromotionPolicy()
        self._deduplicator = deduplicator or MemoryDeduplicator()
        self._conflicts = conflict_resolver or MemoryConflictResolver()
        self._hooks = hooks
        self._events = events

    async def write(
        self,
        candidate: MemoryCandidate,
        *,
        run_id: str | None = None,
    ) -> MemoryWriteResult:
        candidate = await self._candidate_hook(candidate, run_id=run_id)
        assessment = self._policy.assess(candidate)
        if assessment.decision is not PromotionDecision.PROMOTE:
            return MemoryWriteResult(candidate.id, assessment)
        existing = await self._repository.list_all()
        duplicate = self._deduplicator.find_duplicate(candidate, existing)
        if duplicate is not None:
            duplicate.source_refs = list(
                dict.fromkeys([*duplicate.source_refs, *candidate.source_refs])
            )
            duplicate.confidence = max(duplicate.confidence, candidate.confidence)
            duplicate.importance = max(duplicate.importance, candidate.importance)
            duplicate = await self._repository.save(duplicate)
            result = MemoryWriteResult(
                candidate.id,
                assessment,
                ConflictAction.MERGE,
                duplicate,
            )
            await self._written_hook(result, run_id=run_id)
            return result
        resolution = self._conflicts.resolve(candidate, existing)
        if resolution.action is ConflictAction.IGNORE:
            return MemoryWriteResult(candidate.id, assessment, resolution.action)
        memory = _to_memory(
            candidate,
            supersedes=(
                resolution.target.id
                if resolution.action is ConflictAction.SUPERSEDE
                and resolution.target is not None
                else None
            ),
        )
        if memory.supersedes is not None:
            memory = await self._repository.supersede(memory)
        else:
            memory = await self._repository.create(memory)
        result = MemoryWriteResult(candidate.id, assessment, resolution.action, memory)
        await self._written_hook(result, run_id=run_id)
        return result

    async def _candidate_hook(
        self,
        candidate: MemoryCandidate,
        *,
        run_id: str | None,
    ) -> MemoryCandidate:
        if self._hooks is None or run_id is None:
            return candidate
        outcome = await self._hooks.dispatch(
            HookRequest(
                point=HookPoint.MEMORY_CANDIDATE,
                run_id=run_id,
                payload=candidate.model_dump(mode="json"),
            )
        )
        await self._emit(run_id, outcome.emitted_events)
        _require_memory_hook_continue(outcome.decision, HookPoint.MEMORY_CANDIDATE)
        updated = MemoryCandidate.model_validate(outcome.payload)
        if updated.id != candidate.id:
            raise DomainError("Memory Candidate Hook cannot change candidate identity")
        return updated

    async def _written_hook(
        self,
        result: MemoryWriteResult,
        *,
        run_id: str | None,
    ) -> None:
        if self._hooks is None or run_id is None or result.memory is None:
            return
        outcome = await self._hooks.dispatch(
            HookRequest(
                point=HookPoint.MEMORY_WRITTEN,
                run_id=run_id,
                payload={
                    "candidate_id": result.candidate_id,
                    "action": result.action.value if result.action is not None else None,
                    "memory": result.memory.model_dump(mode="json"),
                },
            )
        )
        await self._emit(run_id, outcome.emitted_events)
        _require_memory_hook_continue(outcome.decision, HookPoint.MEMORY_WRITTEN)

    async def _emit(
        self,
        run_id: str,
        emitted_events: list[dict[str, object]],
    ) -> None:
        if self._events is None:
            return
        for emitted in emitted_events:
            event_type = emitted.get("event_type")
            if isinstance(event_type, str) and event_type:
                await self._events.append(
                    run_id,
                    event_type,
                    {key: value for key, value in emitted.items() if key != "event_type"},
                )


def _require_memory_hook_continue(decision: HookDecision, point: HookPoint) -> None:
    if decision in {HookDecision.BLOCK, HookDecision.REQUIRE_APPROVAL}:
        raise DomainError(f"Runtime Hook blocked {point.value}")


def _to_memory(candidate: MemoryCandidate, *, supersedes: str | None) -> MemoryRecord:
    return MemoryRecord(
        memory_type=candidate.suggested_type,
        scope_type=candidate.suggested_scope.scope_type,
        scope_id=candidate.suggested_scope.scope_id,
        title=candidate.title,
        content=candidate.content,
        summary=candidate.summary,
        retrieval_keywords=list(
            dict.fromkeys(
                [
                    *candidate.retrieval_keywords,
                    *([candidate.conflict_key] if candidate.conflict_key else []),
                ]
            )
        ),
        confidence=candidate.confidence,
        importance=candidate.importance,
        source_refs=candidate.source_refs,
        valid_until=candidate.valid_until,
        supersedes=supersedes,
        created_by=MemoryAuthor.SYSTEM,
    )


def _canonical(value: str) -> str:
    return " ".join(value.casefold().split())
