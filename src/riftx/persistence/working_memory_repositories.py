"""SQLAlchemy persistence with optimistic locking for Working Memory."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import RepositoryConflictError
from riftx.context.working_memory import WorkingMemory

from .orm import WorkingMemoryRecord
from .working_memory_mappers import (
    working_memory_from_record,
    working_memory_state,
    working_memory_to_record,
)

SessionFactory = async_sessionmaker[AsyncSession]


class SQLAlchemyWorkingMemoryRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, memory: WorkingMemory) -> WorkingMemory:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(working_memory_to_record(memory))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not create Working Memory for Run {memory.run_id!r}"
            ) from exc
        return memory

    async def get(self, memory_id: str) -> WorkingMemory | None:
        async with self._session_factory() as session:
            record = await session.get(WorkingMemoryRecord, memory_id)
        return working_memory_from_record(record) if record is not None else None

    async def get_for_run(self, run_id: str) -> WorkingMemory | None:
        statement = select(WorkingMemoryRecord).where(WorkingMemoryRecord.run_id == run_id)
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return working_memory_from_record(record) if record is not None else None

    async def save(self, memory: WorkingMemory, *, expected_version: int) -> WorkingMemory:
        if memory.version != expected_version + 1:
            raise RepositoryConflictError(
                f"Working Memory {memory.id!r} must advance exactly one version from "
                f"{expected_version} to {expected_version + 1}"
            )
        statement = (
            update(WorkingMemoryRecord)
            .where(
                WorkingMemoryRecord.id == memory.id,
                WorkingMemoryRecord.run_id == memory.run_id,
                WorkingMemoryRecord.version == expected_version,
            )
            .values(
                version=memory.version,
                state_json=working_memory_state(memory),
                updated_at=memory.updated_at,
            )
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(statement)
            if result.rowcount != 1:
                raise RepositoryConflictError(
                    f"Working Memory {memory.id!r} version conflict; expected {expected_version}"
                )
        return memory
