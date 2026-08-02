"""SQLAlchemy persistence for Context compilation manifests and usage."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.context import ContextCompilation

from .context_mappers import context_compilation_from_record, context_compilation_to_record
from .orm import ContextCompilationRecord

SessionFactory = async_sessionmaker[AsyncSession]


class SQLAlchemyContextCompilationRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, compilation: ContextCompilation) -> ContextCompilation:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(context_compilation_to_record(compilation))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not create Context compilation {compilation.id!r}"
            ) from exc
        return compilation

    async def get(self, compilation_id: str) -> ContextCompilation | None:
        async with self._session_factory() as session:
            record = await session.get(ContextCompilationRecord, compilation_id)
        return context_compilation_from_record(record) if record is not None else None

    async def latest_for_session(self, session_id: str) -> ContextCompilation | None:
        return await self._latest(ContextCompilationRecord.session_id == session_id)

    async def latest_for_run(self, run_id: str) -> ContextCompilation | None:
        return await self._latest(ContextCompilationRecord.run_id == run_id)

    async def update_usage(
        self,
        compilation_id: str,
        *,
        actual_input_tokens: int | None,
        actual_output_tokens: int | None,
    ) -> ContextCompilation:
        async with self._session_factory() as session, session.begin():
            record = await session.get(ContextCompilationRecord, compilation_id)
            if record is None:
                raise EntityNotFoundError("ContextCompilation", compilation_id)
            if actual_input_tokens is not None:
                record.actual_input_tokens = actual_input_tokens
            if actual_output_tokens is not None:
                record.actual_output_tokens = actual_output_tokens
            await session.flush()
            compilation = context_compilation_from_record(record)
        return compilation

    async def _latest(self, predicate: object) -> ContextCompilation | None:
        statement = (
            select(ContextCompilationRecord)
            .where(predicate)
            .order_by(
                ContextCompilationRecord.created_at.desc(),
                ContextCompilationRecord.id.desc(),
            )
            .limit(1)
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return context_compilation_from_record(record) if record is not None else None
