"""Persistence for provider-neutral Context Checkpoints."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import RepositoryConflictError
from riftx.context.checkpoints import ContextCheckpoint

from .orm import ContextCheckpointRecord

SessionFactory = async_sessionmaker[AsyncSession]


class SQLAlchemyContextCheckpointRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, checkpoint: ContextCheckpoint) -> ContextCheckpoint:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(_to_record(checkpoint))
                await session.flush()
        except IntegrityError as exc:
            existing = await self.get(checkpoint.id)
            if existing == checkpoint:
                return existing
            raise RepositoryConflictError(
                f"could not create Context Checkpoint {checkpoint.id!r}"
            ) from exc
        return checkpoint

    async def get(self, checkpoint_id: str) -> ContextCheckpoint | None:
        async with self._session_factory() as session:
            record = await session.get(ContextCheckpointRecord, checkpoint_id)
        return _from_record(record) if record is not None else None

    async def latest_for_session(self, session_id: str) -> ContextCheckpoint | None:
        statement = (
            select(ContextCheckpointRecord)
            .where(ContextCheckpointRecord.session_id == session_id)
            .order_by(
                ContextCheckpointRecord.created_at.desc(),
                ContextCheckpointRecord.id.desc(),
            )
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return _from_record(record) if record is not None else None

    async def list_for_run(self, run_id: str) -> list[ContextCheckpoint]:
        statement = (
            select(ContextCheckpointRecord)
            .where(ContextCheckpointRecord.run_id == run_id)
            .order_by(ContextCheckpointRecord.created_at, ContextCheckpointRecord.id)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [_from_record(record) for record in records]


def _to_record(checkpoint: ContextCheckpoint) -> ContextCheckpointRecord:
    return ContextCheckpointRecord(
        id=checkpoint.id,
        run_id=checkpoint.run_id,
        session_id=checkpoint.session_id,
        checkpoint_type=checkpoint.checkpoint_type.value,
        compaction_stage=checkpoint.compaction_stage.value,
        model_profile=checkpoint.model_profile,
        working_memory_version=checkpoint.working_memory_version,
        provider_state_id=checkpoint.provider_state_id,
        context_compilation_id=checkpoint.context_compilation_id,
        snapshot_json=checkpoint.model_dump(mode="json"),
        created_at=checkpoint.created_at,
    )


def _from_record(record: ContextCheckpointRecord) -> ContextCheckpoint:
    return ContextCheckpoint.model_validate(record.snapshot_json)
