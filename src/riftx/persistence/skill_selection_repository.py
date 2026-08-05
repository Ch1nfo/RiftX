"""Durable Session-scoped Progressive Skill selection storage."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from riftx.application.errors import (
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
)
from riftx.domain.base import utc_now
from riftx.skills import SkillDocument, SkillReference, SkillSelectionState

from .orm import AgentSessionRecord
from .skill_selection_records import AgentSkillScopeRecord, AgentSkillSelectionRecord
from .transactions import SessionFactory, serialized_write


class SQLAlchemySkillSelectionStore:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get_selection(
        self,
        session_id: str,
        skill_id: str,
    ) -> SkillSelectionState | None:
        try:
            async with self._session_factory() as session:
                record = await session.get(
                    AgentSkillSelectionRecord,
                    (session_id, skill_id),
                )
            return _from_record(record) if record is not None else None
        except (TypeError, ValueError):
            raise RepositoryIntegrityError("AgentSkillSelection", skill_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Skill selection persistence is unavailable") from None

    async def list_selections(self, session_id: str) -> list[SkillSelectionState]:
        statement = (
            select(AgentSkillSelectionRecord)
            .where(AgentSkillSelectionRecord.session_id == session_id)
            .order_by(
                AgentSkillSelectionRecord.selected_at,
                AgentSkillSelectionRecord.skill_id,
            )
        )
        try:
            async with self._session_factory() as session:
                records = (await session.scalars(statement)).all()
            return [_from_record(record) for record in records]
        except (TypeError, ValueError):
            raise RepositoryIntegrityError("AgentSkillSelection", session_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Skill selection persistence is unavailable") from None

    async def save_selection(self, selection: SkillSelectionState) -> None:
        try:
            async with serialized_write(self._session_factory) as session:
                await _require_session_scope(
                    session,
                    run_id=selection.run_id,
                    session_id=selection.session_id,
                    agent_id=selection.agent_id,
                )
                record = await session.get(
                    AgentSkillSelectionRecord,
                    (selection.session_id, selection.skill_id),
                    with_for_update=True,
                )
                if record is None:
                    session.add(_to_record(selection))
                else:
                    persisted = _from_record(record)
                    if _immutable_selection(persisted) != _immutable_selection(selection):
                        raise RepositoryConflictError(
                            "Running Agent Session cannot replace a pinned Skill package"
                        )
                    record.active = selection.active
                    record.references_loaded = selection.references_loaded
                    record.updated_at = selection.updated_at
                    record.unloaded_at = selection.unloaded_at
                await session.flush()
        except (RepositoryConflictError, EntityNotFoundError):
            raise
        except IntegrityError:
            raise RepositoryConflictError("Skill selection persistence conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Skill selection persistence is unavailable") from None

    async def get_allowlist(self, session_id: str) -> frozenset[str] | None:
        try:
            async with self._session_factory() as session:
                record = await session.get(AgentSkillScopeRecord, session_id)
            if record is None:
                return None
            values = record.allowed_skill_ids_json
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise RepositoryIntegrityError("AgentSkillScope", session_id)
            return frozenset(values)
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Skill selection persistence is unavailable") from None

    async def set_allowlist(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        skill_ids: list[str],
    ) -> None:
        try:
            async with serialized_write(self._session_factory) as session:
                await _require_session_scope(
                    session,
                    run_id=run_id,
                    session_id=session_id,
                    agent_id=agent_id,
                )
                record = await session.get(
                    AgentSkillScopeRecord,
                    session_id,
                    with_for_update=True,
                )
                normalized = list(dict.fromkeys(skill_ids))
                if record is None:
                    session.add(
                        AgentSkillScopeRecord(
                            session_id=session_id,
                            run_id=run_id,
                            agent_id=agent_id,
                            allowed_skill_ids_json=normalized,
                            updated_at=utc_now(),
                        )
                    )
                elif (
                    record.run_id != run_id
                    or record.agent_id != agent_id
                    or record.allowed_skill_ids_json != normalized
                ):
                    raise RepositoryConflictError(
                        "Agent Session Skill allowlist is immutable after delegation"
                    )
                await session.flush()
        except (RepositoryConflictError, EntityNotFoundError):
            raise
        except IntegrityError:
            raise RepositoryConflictError("Skill allowlist persistence conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Skill selection persistence is unavailable") from None


async def _require_session_scope(
    session: AsyncSession,
    *,
    run_id: str,
    session_id: str,
    agent_id: str,
) -> AgentSessionRecord:
    record = await session.get(AgentSessionRecord, session_id)
    if record is None:
        raise EntityNotFoundError("AgentSession", session_id)
    if record.run_id != run_id or record.agent_type != agent_id:
        raise RepositoryConflictError("Skill state does not match the Agent Session scope")
    return record


def _to_record(selection: SkillSelectionState) -> AgentSkillSelectionRecord:
    return AgentSkillSelectionRecord(
        session_id=selection.session_id,
        skill_id=selection.skill_id,
        run_id=selection.run_id,
        agent_id=selection.agent_id,
        version=selection.version,
        skill_digest=selection.digest,
        source=selection.source.value,
        reason=selection.reason,
        document_json=selection.document.model_dump(mode="json"),
        reference_json=(
            selection.reference.model_dump(mode="json")
            if selection.reference is not None
            else None
        ),
        active=selection.active,
        references_loaded=selection.references_loaded,
        selected_at=selection.selected_at,
        updated_at=selection.updated_at,
        unloaded_at=selection.unloaded_at,
    )


def _from_record(record: AgentSkillSelectionRecord) -> SkillSelectionState:
    document = SkillDocument.model_validate(record.document_json)
    reference = (
        SkillReference.model_validate(record.reference_json)
        if record.reference_json is not None
        else None
    )
    if (
        document.id != record.skill_id
        or document.version != record.version
        or document.digest != record.skill_digest
        or document.source.value != record.source
        or (reference is not None and reference.digest != record.skill_digest)
    ):
        raise RepositoryIntegrityError("AgentSkillSelection", record.skill_id)
    return SkillSelectionState(
        run_id=record.run_id,
        session_id=record.session_id,
        agent_id=record.agent_id,
        skill_id=record.skill_id,
        version=record.version,
        digest=record.skill_digest,
        source=record.source,
        reason=record.reason,
        document=document,
        reference=reference,
        active=record.active,
        references_loaded=record.references_loaded,
        selected_at=record.selected_at,
        updated_at=record.updated_at,
        unloaded_at=record.unloaded_at,
    )


def _immutable_selection(selection: SkillSelectionState) -> tuple[object, ...]:
    return (
        selection.run_id,
        selection.session_id,
        selection.agent_id,
        selection.skill_id,
        selection.version,
        selection.digest,
        selection.source,
        selection.reason,
        selection.document,
        selection.reference,
        selection.selected_at,
    )
