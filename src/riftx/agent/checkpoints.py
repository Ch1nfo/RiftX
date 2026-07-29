"""Durable storage for resumable Agents SDK RunState payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError
from riftx.domain import AgentCheckpoint
from riftx.domain.base import utc_now
from riftx.persistence.orm import AgentCheckpointRecord, RunRecord

SessionFactory = async_sessionmaker[AsyncSession]


class SQLAlchemyCheckpointStore:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        run_id: str,
        state_json: Mapping[str, Any],
        *,
        status: str = "pending",
    ) -> AgentCheckpoint:
        checkpoint = AgentCheckpoint(
            run_id=run_id,
            sdk_state=dict(state_json),
            status=_validate_status(status),
        )
        async with self._session_factory() as session, session.begin():
            run_exists = await session.scalar(select(RunRecord.id).where(RunRecord.id == run_id))
            if run_exists is None:
                raise EntityNotFoundError("Run", run_id)
            session.add(_checkpoint_to_record(checkpoint))
            await session.flush()
        return checkpoint

    async def get(self, checkpoint_id: str) -> AgentCheckpoint | None:
        async with self._session_factory() as session:
            record = await session.get(AgentCheckpointRecord, checkpoint_id)
        return _checkpoint_from_record(record) if record is not None else None

    async def resolve(self, checkpoint_id: str, status: str = "resolved") -> AgentCheckpoint:
        target = _validate_status(status)
        if target == "pending":
            raise ValueError("resolved checkpoint status cannot be pending")

        async with self._session_factory() as session, session.begin():
            record = await session.scalar(
                select(AgentCheckpointRecord)
                .where(AgentCheckpointRecord.id == checkpoint_id)
                .with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("AgentCheckpoint", checkpoint_id)
            record.status = target
            record.resolved_at = utc_now()
            await session.flush()
            return _checkpoint_from_record(record)


def _validate_status(status: str) -> str:
    normalized = status.strip()
    if not normalized:
        raise ValueError("checkpoint status must not be empty")
    if len(normalized) > 32:
        raise ValueError("checkpoint status must not exceed 32 characters")
    return normalized


def _checkpoint_to_record(checkpoint: AgentCheckpoint) -> AgentCheckpointRecord:
    return AgentCheckpointRecord(
        id=checkpoint.id,
        run_id=checkpoint.run_id,
        sdk_state=checkpoint.sdk_state,
        status=checkpoint.status,
        created_at=checkpoint.created_at,
        resolved_at=checkpoint.resolved_at,
    )


def _checkpoint_from_record(record: AgentCheckpointRecord) -> AgentCheckpoint:
    return AgentCheckpoint(
        id=record.id,
        run_id=record.run_id,
        sdk_state=record.sdk_state,
        status=record.status,
        created_at=record.created_at,
        resolved_at=record.resolved_at,
    )
