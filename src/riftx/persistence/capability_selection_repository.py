"""Durable unified Session Capability selection storage."""

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
from riftx.capabilities import CapabilityKind, SessionCapabilitySelection
from riftx.domain.base import utc_now

from .capability_selection_records import (
    AgentCapabilityScopeRecord,
    AgentCapabilitySelectionRecord,
)
from .orm import AgentSessionRecord
from .transactions import SessionFactory, serialized_write


class SQLAlchemyCapabilitySelectionStore:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get_selection(
        self,
        session_id: str,
        kind: CapabilityKind,
        capability_id: str,
    ) -> SessionCapabilitySelection | None:
        try:
            async with self._session_factory() as session:
                record = await session.get(
                    AgentCapabilitySelectionRecord,
                    (session_id, kind.value, capability_id),
                )
            return _from_record(record) if record is not None else None
        except (TypeError, ValueError):
            raise RepositoryIntegrityError("AgentCapabilitySelection", capability_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Capability selection persistence is unavailable"
            ) from None

    async def list_selections(
        self,
        session_id: str,
        *,
        kind: CapabilityKind | None = None,
    ) -> list[SessionCapabilitySelection]:
        statement = select(AgentCapabilitySelectionRecord).where(
            AgentCapabilitySelectionRecord.session_id == session_id
        )
        if kind is not None:
            statement = statement.where(AgentCapabilitySelectionRecord.kind == kind.value)
        statement = statement.order_by(
            AgentCapabilitySelectionRecord.selected_at,
            AgentCapabilitySelectionRecord.kind,
            AgentCapabilitySelectionRecord.capability_id,
        )
        try:
            async with self._session_factory() as session:
                records = (await session.scalars(statement)).all()
            return [_from_record(record) for record in records]
        except (TypeError, ValueError):
            raise RepositoryIntegrityError("AgentCapabilitySelection", session_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Capability selection persistence is unavailable"
            ) from None

    async def save_selection(self, selection: SessionCapabilitySelection) -> None:
        try:
            async with serialized_write(self._session_factory) as session:
                await _require_session_scope(session, selection)
                record = await session.get(
                    AgentCapabilitySelectionRecord,
                    (selection.session_id, selection.kind.value, selection.capability_id),
                    with_for_update=True,
                )
                if record is None:
                    session.add(_to_record(selection))
                else:
                    persisted = _from_record(record)
                    if _pinned_fields(persisted) != _pinned_fields(selection):
                        raise RepositoryConflictError(
                            "Running Agent Session cannot replace a pinned Capability"
                        )
                    record.state_json = selection.state
                    record.active = selection.active
                    record.updated_at = selection.updated_at
                    record.unloaded_at = selection.unloaded_at
                await session.flush()
        except (RepositoryConflictError, EntityNotFoundError):
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability selection persistence conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Capability selection persistence is unavailable"
            ) from None

    async def replace_selection(
        self,
        selection: SessionCapabilitySelection,
        *,
        expected_digest: str,
    ) -> None:
        try:
            async with serialized_write(self._session_factory) as session:
                await _require_session_scope(session, selection)
                record = await session.get(
                    AgentCapabilitySelectionRecord,
                    (selection.session_id, selection.kind.value, selection.capability_id),
                    with_for_update=True,
                )
                if record is None or record.capability_digest != expected_digest:
                    raise RepositoryConflictError("Capability reload digest no longer matches")
                persisted = _from_record(record)
                if _scope_fields(persisted) != _scope_fields(selection):
                    raise RepositoryConflictError("Capability reload changed its Session scope")
                _replace_record(record, selection)
                await session.flush()
        except (RepositoryConflictError, EntityNotFoundError):
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability reload conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Capability selection persistence is unavailable"
            ) from None

    async def get_allowlist(
        self,
        session_id: str,
        kind: CapabilityKind,
    ) -> frozenset[str] | None:
        try:
            async with self._session_factory() as session:
                record = await session.get(
                    AgentCapabilityScopeRecord,
                    (session_id, kind.value),
                )
            if record is None:
                return None
            values = record.allowed_capability_ids_json
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise RepositoryIntegrityError("AgentCapabilityScope", session_id)
            return frozenset(values)
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Capability selection persistence is unavailable"
            ) from None

    async def set_allowlist(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        kind: CapabilityKind,
        capability_ids: list[str],
    ) -> None:
        normalized = list(dict.fromkeys(capability_ids))
        try:
            async with serialized_write(self._session_factory) as session:
                await _require_session_identity(
                    session,
                    run_id=run_id,
                    session_id=session_id,
                    agent_id=agent_id,
                )
                record = await session.get(
                    AgentCapabilityScopeRecord,
                    (session_id, kind.value),
                    with_for_update=True,
                )
                if record is None:
                    session.add(
                        AgentCapabilityScopeRecord(
                            session_id=session_id,
                            kind=kind.value,
                            run_id=run_id,
                            agent_id=agent_id,
                            allowed_capability_ids_json=normalized,
                            updated_at=utc_now(),
                        )
                    )
                elif (
                    record.run_id != run_id
                    or record.agent_id != agent_id
                    or record.allowed_capability_ids_json != normalized
                ):
                    raise RepositoryConflictError(
                        "Agent Session Capability allowlist is immutable after delegation"
                    )
                await session.flush()
        except (RepositoryConflictError, EntityNotFoundError):
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability allowlist conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Capability selection persistence is unavailable"
            ) from None


async def _require_session_scope(
    session: AsyncSession,
    selection: SessionCapabilitySelection,
) -> AgentSessionRecord:
    return await _require_session_identity(
        session,
        run_id=selection.run_id,
        session_id=selection.session_id,
        agent_id=selection.agent_id,
    )


async def _require_session_identity(
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
        raise RepositoryConflictError("Capability state does not match the Agent Session scope")
    return record


def _to_record(selection: SessionCapabilitySelection) -> AgentCapabilitySelectionRecord:
    return AgentCapabilitySelectionRecord(
        session_id=selection.session_id,
        kind=selection.kind.value,
        capability_id=selection.capability_id,
        run_id=selection.run_id,
        agent_id=selection.agent_id,
        version=selection.version,
        capability_digest=selection.digest,
        source=selection.source.value,
        reason=selection.reason,
        snapshot_json=selection.snapshot,
        state_json=selection.state,
        active=selection.active,
        selected_at=selection.selected_at,
        updated_at=selection.updated_at,
        unloaded_at=selection.unloaded_at,
    )


def _replace_record(
    record: AgentCapabilitySelectionRecord,
    selection: SessionCapabilitySelection,
) -> None:
    record.version = selection.version
    record.capability_digest = selection.digest
    record.source = selection.source.value
    record.reason = selection.reason
    record.snapshot_json = selection.snapshot
    record.state_json = selection.state
    record.active = selection.active
    record.selected_at = selection.selected_at
    record.updated_at = selection.updated_at
    record.unloaded_at = selection.unloaded_at


def _from_record(record: AgentCapabilitySelectionRecord) -> SessionCapabilitySelection:
    return SessionCapabilitySelection.model_validate(
        {
            "run_id": record.run_id,
            "session_id": record.session_id,
            "agent_id": record.agent_id,
            "kind": record.kind,
            "capability_id": record.capability_id,
            "version": record.version,
            "digest": record.capability_digest,
            "source": record.source,
            "reason": record.reason,
            "snapshot": record.snapshot_json,
            "state": record.state_json,
            "active": record.active,
            "selected_at": record.selected_at,
            "updated_at": record.updated_at,
            "unloaded_at": record.unloaded_at,
        }
    )


def _scope_fields(selection: SessionCapabilitySelection) -> tuple[object, ...]:
    return (
        selection.run_id,
        selection.session_id,
        selection.agent_id,
        selection.kind,
        selection.capability_id,
    )


def _pinned_fields(selection: SessionCapabilitySelection) -> tuple[object, ...]:
    return (
        *_scope_fields(selection),
        selection.version,
        selection.digest,
        selection.source,
        selection.reason,
        selection.snapshot,
        selection.selected_at,
    )
