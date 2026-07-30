"""Controlled promotion from Run Working Memory into Engagement facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.context import ConfirmedFact
from riftx.domain.base import utc_now
from riftx.persistence.fact_repositories import (
    SQLAlchemyEngagementFactRepository,
    SQLAlchemyFactRelationRepository,
)
from riftx.persistence.repositories import SQLAlchemyRunRepository
from riftx.persistence.working_memory_repositories import (
    SQLAlchemyWorkingMemoryRepository,
)

from .models import (
    AttackGraph,
    EngagementFact,
    FactPromotionCandidate,
    FactRelation,
)


class FactPromotionAction(StrEnum):
    CREATED = "created"
    MERGED = "merged"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class FactPromotionResult:
    candidate_id: str
    action: FactPromotionAction
    fact: EngagementFact | None
    reason: str


class FactPromotionService:
    def __init__(
        self,
        *,
        runs: SQLAlchemyRunRepository,
        working_memory: SQLAlchemyWorkingMemoryRepository,
        facts: SQLAlchemyEngagementFactRepository,
    ) -> None:
        self._runs = runs
        self._working_memory = working_memory
        self._facts = facts

    async def candidates(
        self,
        run_id: str,
        *,
        source_session_id: str | None = None,
    ) -> list[FactPromotionCandidate]:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        memory = await self._working_memory.get_for_run(run_id)
        if memory is None:
            return []
        return [
            _candidate(run.engagement_id, source_session_id, fact)
            for fact in memory.confirmed_facts
            if fact.status.value == "confirmed"
        ]

    async def promote(self, candidate: FactPromotionCandidate) -> FactPromotionResult:
        run = await self._runs.get(candidate.source_run_id)
        if run is None:
            raise EntityNotFoundError("Run", candidate.source_run_id)
        if run.engagement_id != candidate.engagement_id:
            raise RepositoryConflictError("Fact Candidate crosses Engagement boundary")
        if candidate.valid_until is not None and candidate.valid_until <= utc_now():
            return FactPromotionResult(
                candidate.id,
                FactPromotionAction.REJECTED,
                None,
                "candidate validity has expired",
            )
        if not candidate.user_confirmed and not candidate.rule_eligible:
            return FactPromotionResult(
                candidate.id,
                FactPromotionAction.REJECTED,
                None,
                "model inference requires explicit user confirmation",
            )
        existing = await self._facts.find_active(
            candidate.engagement_id,
            candidate.subject,
            candidate.predicate,
        )
        duplicate = next((item for item in existing if item.value == candidate.value), None)
        if duplicate is not None:
            _merge_candidate(duplicate, candidate)
            return FactPromotionResult(
                candidate.id,
                FactPromotionAction.MERGED,
                await self._facts.save(duplicate),
                "matching Engagement Fact evidence merged",
            )
        strongest = max(existing, key=lambda item: item.confidence, default=None)
        if (
            strongest is not None
            and candidate.confidence < strongest.confidence
            and not candidate.user_confirmed
        ):
            return FactPromotionResult(
                candidate.id,
                FactPromotionAction.REJECTED,
                None,
                "candidate confidence is below the active conflicting Fact",
            )
        replacement = _engagement_fact(
            candidate,
            supersedes_fact_id=strongest.id if strongest is not None else None,
        )
        if strongest is None:
            saved = await self._facts.create(replacement)
            action = FactPromotionAction.CREATED
        else:
            saved = await self._facts.supersede(strongest, replacement)
            action = FactPromotionAction.SUPERSEDED
        return FactPromotionResult(candidate.id, action, saved, "candidate promoted")


class AttackGraphService:
    def __init__(
        self,
        *,
        facts: SQLAlchemyEngagementFactRepository,
        relations: SQLAlchemyFactRelationRepository,
    ) -> None:
        self._facts = facts
        self._relations = relations

    async def add_relation(self, relation: FactRelation) -> FactRelation:
        source = await self._facts.get(relation.source_fact_id)
        target = await self._facts.get(relation.target_fact_id)
        if source is None or target is None:
            missing = relation.source_fact_id if source is None else relation.target_fact_id
            raise EntityNotFoundError("EngagementFact", missing)
        if (
            source.engagement_id != relation.engagement_id
            or target.engagement_id != relation.engagement_id
        ):
            raise RepositoryConflictError("Fact Relation crosses Engagement boundary")
        return await self._relations.create(relation)

    async def graph(self, engagement_id: str) -> AttackGraph:
        return AttackGraph(
            engagement_id=engagement_id,
            facts=await self._facts.list_for_engagement(engagement_id),
            relations=await self._relations.list_for_engagement(engagement_id),
        )


def _candidate(
    engagement_id: str,
    source_session_id: str | None,
    fact: ConfirmedFact,
) -> FactPromotionCandidate:
    return FactPromotionCandidate(
        engagement_id=engagement_id,
        source_run_id=fact.run_id,
        source_session_id=source_session_id,
        working_fact_id=fact.id,
        subject=fact.subject,
        predicate=fact.predicate,
        value=fact.value,
        natural_language=fact.natural_language,
        evidence_refs=fact.source_refs,
        evidence_sources=fact.source_types,
        source_execution_ids=_ids(fact.source_refs, "execution://"),
        artifact_ids=_ids(fact.source_refs, "artifact://"),
        confidence=fact.confidence,
        valid_from=fact.first_observed_at,
    )


def _engagement_fact(
    candidate: FactPromotionCandidate,
    *,
    supersedes_fact_id: str | None,
) -> EngagementFact:
    return EngagementFact(
        engagement_id=candidate.engagement_id,
        subject=candidate.subject,
        predicate=candidate.predicate,
        value=candidate.value,
        natural_language=candidate.natural_language,
        evidence_refs=candidate.evidence_refs,
        source_run_ids=[candidate.source_run_id],
        source_session_ids=(
            [candidate.source_session_id] if candidate.source_session_id is not None else []
        ),
        source_execution_ids=candidate.source_execution_ids,
        artifact_ids=candidate.artifact_ids,
        confidence=candidate.confidence,
        valid_from=candidate.valid_from,
        valid_until=candidate.valid_until,
        supersedes_fact_id=supersedes_fact_id,
    )


def _merge_candidate(fact: EngagementFact, candidate: FactPromotionCandidate) -> None:
    fact.evidence_refs = _unique([*fact.evidence_refs, *candidate.evidence_refs])
    fact.source_run_ids = _unique([*fact.source_run_ids, candidate.source_run_id])
    fact.source_session_ids = _unique(
        [
            *fact.source_session_ids,
            *(
                [candidate.source_session_id]
                if candidate.source_session_id is not None
                else []
            ),
        ]
    )
    fact.source_execution_ids = _unique(
        [*fact.source_execution_ids, *candidate.source_execution_ids]
    )
    fact.artifact_ids = _unique([*fact.artifact_ids, *candidate.artifact_ids])
    fact.confidence = max(fact.confidence, candidate.confidence)
    fact.valid_until = candidate.valid_until or fact.valid_until


def _ids(refs: list[str], prefix: str) -> list[str]:
    return _unique([ref.removeprefix(prefix) for ref in refs if ref.startswith(prefix)])


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
