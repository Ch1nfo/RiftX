"""SQLAlchemy persistence for Engagement facts and Attack Graph edges."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain.base import utc_now
from riftx.facts.models import EngagementFact, EngagementFactStatus, FactRelation

from .orm import EngagementFactRecord, FactRelationRecord

SessionFactory = async_sessionmaker[AsyncSession]


class SQLAlchemyEngagementFactRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, fact: EngagementFact) -> EngagementFact:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(_fact_record(fact))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create Fact {fact.id!r}") from exc
        return fact

    async def get(self, fact_id: str) -> EngagementFact | None:
        async with self._session_factory() as session:
            row = await session.get(EngagementFactRecord, fact_id)
        return _fact(row) if row is not None else None

    async def list_for_engagement(
        self,
        engagement_id: str,
        *,
        active_only: bool = True,
    ) -> list[EngagementFact]:
        statement = select(EngagementFactRecord).where(
            EngagementFactRecord.engagement_id == engagement_id
        )
        if active_only:
            statement = statement.where(
                EngagementFactRecord.status == EngagementFactStatus.ACTIVE.value
            )
        statement = statement.order_by(EngagementFactRecord.created_at, EngagementFactRecord.id)
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [_fact(row) for row in rows]

    async def find_active(
        self,
        engagement_id: str,
        subject: str,
        predicate: str,
    ) -> list[EngagementFact]:
        statement = select(EngagementFactRecord).where(
            EngagementFactRecord.engagement_id == engagement_id,
            EngagementFactRecord.subject == subject,
            EngagementFactRecord.predicate == predicate,
            EngagementFactRecord.status == EngagementFactStatus.ACTIVE.value,
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [_fact(row) for row in rows]

    async def save(self, fact: EngagementFact) -> EngagementFact:
        fact.updated_at = utc_now()
        async with self._session_factory() as session, session.begin():
            row = await session.get(EngagementFactRecord, fact.id)
            if row is None:
                raise EntityNotFoundError("EngagementFact", fact.id)
            replacement = _fact_record(fact)
            for name in (
                "natural_language",
                "evidence_refs_json",
                "source_run_ids_json",
                "source_session_ids_json",
                "source_execution_ids_json",
                "artifact_ids_json",
                "confidence",
                "valid_from",
                "valid_until",
                "supersedes_fact_id",
                "status",
                "updated_at",
            ):
                setattr(row, name, getattr(replacement, name))
        return fact

    async def supersede(
        self,
        previous: EngagementFact,
        replacement: EngagementFact,
    ) -> EngagementFact:
        if replacement.supersedes_fact_id != previous.id:
            raise ValueError("replacement must reference the superseded Fact")
        try:
            async with self._session_factory() as session, session.begin():
                row = await session.get(EngagementFactRecord, previous.id)
                if row is None:
                    raise EntityNotFoundError("EngagementFact", previous.id)
                if row.status != EngagementFactStatus.ACTIVE.value:
                    raise RepositoryConflictError(
                        f"Engagement Fact {previous.id!r} is not active"
                    )
                row.status = EngagementFactStatus.SUPERSEDED.value
                row.updated_at = utc_now()
                session.add(_fact_record(replacement))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not supersede Engagement Fact {previous.id!r}"
            ) from exc
        return replacement


class SQLAlchemyFactRelationRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, relation: FactRelation) -> FactRelation:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(_relation_record(relation))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not create Fact Relation {relation.id!r}"
            ) from exc
        return relation

    async def list_for_engagement(self, engagement_id: str) -> list[FactRelation]:
        statement = (
            select(FactRelationRecord)
            .where(FactRelationRecord.engagement_id == engagement_id)
            .order_by(FactRelationRecord.created_at, FactRelationRecord.id)
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [_relation(row) for row in rows]


def _fact_record(fact: EngagementFact) -> EngagementFactRecord:
    return EngagementFactRecord(
        id=fact.id,
        engagement_id=fact.engagement_id,
        subject=fact.subject,
        predicate=fact.predicate,
        value_json=fact.value,
        natural_language=fact.natural_language,
        evidence_refs_json=fact.evidence_refs,
        source_run_ids_json=fact.source_run_ids,
        source_session_ids_json=fact.source_session_ids,
        source_execution_ids_json=fact.source_execution_ids,
        artifact_ids_json=fact.artifact_ids,
        confidence=fact.confidence,
        valid_from=fact.valid_from,
        valid_until=fact.valid_until,
        supersedes_fact_id=fact.supersedes_fact_id,
        status=fact.status.value,
        created_at=fact.created_at,
        updated_at=fact.updated_at,
    )


def _fact(row: EngagementFactRecord) -> EngagementFact:
    return EngagementFact(
        id=row.id,
        engagement_id=row.engagement_id,
        subject=row.subject,
        predicate=row.predicate,
        value=row.value_json,
        natural_language=row.natural_language,
        evidence_refs=row.evidence_refs_json,
        source_run_ids=row.source_run_ids_json,
        source_session_ids=row.source_session_ids_json,
        source_execution_ids=row.source_execution_ids_json,
        artifact_ids=row.artifact_ids_json,
        confidence=row.confidence,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        supersedes_fact_id=row.supersedes_fact_id,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _relation_record(relation: FactRelation) -> FactRelationRecord:
    return FactRelationRecord(
        id=relation.id,
        engagement_id=relation.engagement_id,
        source_fact_id=relation.source_fact_id,
        target_fact_id=relation.target_fact_id,
        relation_type=relation.relation_type.value,
        evidence_refs_json=relation.evidence_refs,
        source_run_id=relation.source_run_id,
        source_session_id=relation.source_session_id,
        source_execution_ids_json=relation.source_execution_ids,
        artifact_ids_json=relation.artifact_ids,
        confidence=relation.confidence,
        valid_until=relation.valid_until,
        created_at=relation.created_at,
    )


def _relation(row: FactRelationRecord) -> FactRelation:
    return FactRelation(
        id=row.id,
        engagement_id=row.engagement_id,
        source_fact_id=row.source_fact_id,
        target_fact_id=row.target_fact_id,
        relation_type=row.relation_type,
        evidence_refs=row.evidence_refs_json,
        source_run_id=row.source_run_id,
        source_session_id=row.source_session_id,
        source_execution_ids=row.source_execution_ids_json,
        artifact_ids=row.artifact_ids_json,
        confidence=row.confidence,
        valid_until=row.valid_until,
        created_at=row.created_at,
    )
