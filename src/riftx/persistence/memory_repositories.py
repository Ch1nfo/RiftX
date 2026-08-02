"""SQLAlchemy persistence for auditable long-term Memory records."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain.base import utc_now
from riftx.memory import MemoryRecord, MemoryStatus

from .orm import MemoryRecordRow

SessionFactory = async_sessionmaker[AsyncSession]


class SQLAlchemyMemoryRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, memory: MemoryRecord) -> MemoryRecord:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(_to_record(memory))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create Memory {memory.id!r}") from exc
        return memory

    async def get(self, memory_id: str) -> MemoryRecord | None:
        async with self._session_factory() as session:
            record = await session.get(MemoryRecordRow, memory_id)
        return _from_record(record) if record is not None else None

    async def save(self, memory: MemoryRecord) -> MemoryRecord:
        memory.updated_at = utc_now()
        async with self._session_factory() as session, session.begin():
            record = await session.get(MemoryRecordRow, memory.id)
            if record is None:
                raise EntityNotFoundError("Memory", memory.id)
            _apply(memory, record)
            await session.flush()
        return memory

    async def list_all(self) -> list[MemoryRecord]:
        statement = select(MemoryRecordRow).order_by(
            MemoryRecordRow.pinned.desc(),
            MemoryRecordRow.importance.desc(),
            MemoryRecordRow.updated_at.desc(),
            MemoryRecordRow.id,
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [_from_record(record) for record in records]

    async def supersede(self, memory: MemoryRecord) -> MemoryRecord:
        if memory.supersedes is None:
            raise ValueError("superseding Memory requires supersedes")
        try:
            async with self._session_factory() as session, session.begin():
                old = await session.get(MemoryRecordRow, memory.supersedes)
                if old is None:
                    raise EntityNotFoundError("Memory", memory.supersedes)
                old_memory = _from_record(old)
                if old_memory.status is not MemoryStatus.ACTIVE:
                    raise RepositoryConflictError(
                        f"Memory {old_memory.id!r} is not active"
                    )
                if old_memory.scope != memory.scope:
                    raise RepositoryConflictError(
                        "superseding Memory must use the same scope"
                    )
                old.status = MemoryStatus.SUPERSEDED.value
                old.updated_at = utc_now()
                session.add(_to_record(memory))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not supersede Memory {memory.id!r}") from exc
        return memory


def _to_record(memory: MemoryRecord) -> MemoryRecordRow:
    return MemoryRecordRow(
        id=memory.id,
        memory_type=memory.memory_type.value,
        scope_type=memory.scope_type.value,
        scope_id=memory.scope_id,
        title=memory.title,
        content=memory.content,
        summary=memory.summary,
        retrieval_keywords_json=memory.retrieval_keywords,
        confidence=memory.confidence,
        importance=memory.importance,
        source_refs_json=memory.source_refs,
        valid_from=memory.valid_from,
        valid_until=memory.valid_until,
        supersedes=memory.supersedes,
        status=memory.status.value,
        pinned=memory.pinned,
        created_by=memory.created_by.value,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def _apply(memory: MemoryRecord, record: MemoryRecordRow) -> None:
    replacement = _to_record(memory)
    for field in (
        "memory_type",
        "scope_type",
        "scope_id",
        "title",
        "content",
        "summary",
        "retrieval_keywords_json",
        "confidence",
        "importance",
        "source_refs_json",
        "valid_from",
        "valid_until",
        "supersedes",
        "status",
        "pinned",
        "created_by",
        "updated_at",
    ):
        setattr(record, field, getattr(replacement, field))


def _from_record(record: MemoryRecordRow) -> MemoryRecord:
    return MemoryRecord(
        id=record.id,
        memory_type=record.memory_type,
        scope_type=record.scope_type,
        scope_id=record.scope_id,
        title=record.title,
        content=record.content,
        summary=record.summary,
        retrieval_keywords=record.retrieval_keywords_json,
        confidence=record.confidence,
        importance=record.importance,
        source_refs=record.source_refs_json,
        valid_from=record.valid_from,
        valid_until=record.valid_until,
        supersedes=record.supersedes,
        status=record.status,
        pinned=record.pinned,
        created_by=record.created_by,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
