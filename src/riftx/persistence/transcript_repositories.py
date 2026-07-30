"""SQLAlchemy repository for complete per-session transcripts."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain import AgentMessage, TranscriptMessageDraft
from riftx.runtime.types import SessionStatus

from .orm import AgentMessageRecord, AgentSessionRecord
from .transcript_mappers import agent_message_from_record, agent_message_to_record

SessionFactory = async_sessionmaker[AsyncSession]
_TERMINAL_STATUSES = {
    SessionStatus.COMPLETED.value,
    SessionStatus.FAILED.value,
    SessionStatus.CANCELLED.value,
}


class SQLAlchemyTranscriptRepository:
    """Append and query the immutable-order transcript for an Agent Session."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def append(
        self,
        session_id: str,
        draft: TranscriptMessageDraft,
        *,
        expected_last_sequence: int | None = None,
    ) -> AgentMessage:
        messages = await self.append_many(
            session_id,
            [draft],
            expected_last_sequence=expected_last_sequence,
        )
        return messages[0]

    async def append_many(
        self,
        session_id: str,
        drafts: Sequence[TranscriptMessageDraft],
        *,
        expected_last_sequence: int | None = None,
    ) -> list[AgentMessage]:
        if not drafts:
            return []
        try:
            async with self._session_factory() as database_session, database_session.begin():
                agent_session = await database_session.scalar(
                    select(AgentSessionRecord)
                    .where(AgentSessionRecord.id == session_id)
                    .with_for_update()
                )
                if agent_session is None:
                    raise EntityNotFoundError("AgentSession", session_id)
                self._require_writable(agent_session)
                current = int(
                    await database_session.scalar(
                        select(func.max(AgentMessageRecord.sequence)).where(
                            AgentMessageRecord.session_id == session_id
                        )
                    )
                    or 0
                )
                if expected_last_sequence is not None and current != expected_last_sequence:
                    raise RepositoryConflictError(
                        f"transcript {session_id!r} sequence conflict; "
                        f"expected {expected_last_sequence}, found {current}"
                    )
                await self._validate_parents(database_session, session_id, drafts)
                messages = [
                    AgentMessage(
                        run_id=agent_session.run_id,
                        session_id=session_id,
                        sequence=current + offset,
                        **draft.model_dump(),
                    )
                    for offset, draft in enumerate(drafts, start=1)
                ]
                database_session.add_all(agent_message_to_record(item) for item in messages)
                await database_session.flush()
                return messages
        except RepositoryConflictError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise RepositoryConflictError(
                f"transcript {session_id!r} concurrent append conflict"
            ) from exc

    async def get(self, message_id: str) -> AgentMessage | None:
        async with self._session_factory() as session:
            record = await session.get(AgentMessageRecord, message_id)
        return agent_message_from_record(record) if record is not None else None

    async def list_by_session(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[AgentMessage]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        statement = (
            select(AgentMessageRecord)
            .where(AgentMessageRecord.session_id == session_id)
            .order_by(AgentMessageRecord.sequence.desc() if limit else AgentMessageRecord.sequence)
        )
        if limit is not None:
            statement = statement.limit(limit)
        async with self._session_factory() as session:
            records = list((await session.scalars(statement)).all())
        if limit is not None:
            records.reverse()
        return [agent_message_from_record(record) for record in records]

    async def last_sequence(self, session_id: str) -> int:
        async with self._session_factory() as session:
            exists = await session.get(AgentSessionRecord, session_id)
            if exists is None:
                raise EntityNotFoundError("AgentSession", session_id)
            value = await session.scalar(
                select(func.max(AgentMessageRecord.sequence)).where(
                    AgentMessageRecord.session_id == session_id
                )
            )
        return int(value or 0)

    async def pop(self, session_id: str) -> AgentMessage | None:
        async with self._session_factory() as session, session.begin():
            agent_session = await session.scalar(
                select(AgentSessionRecord)
                .where(AgentSessionRecord.id == session_id)
                .with_for_update()
            )
            if agent_session is None:
                raise EntityNotFoundError("AgentSession", session_id)
            self._require_writable(agent_session)
            record = await session.scalar(
                select(AgentMessageRecord)
                .where(AgentMessageRecord.session_id == session_id)
                .order_by(AgentMessageRecord.sequence.desc())
                .limit(1)
                .with_for_update()
            )
            if record is None:
                return None
            message = agent_message_from_record(record)
            await session.delete(record)
            await session.flush()
            return message

    async def clear(self, session_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            agent_session = await session.scalar(
                select(AgentSessionRecord)
                .where(AgentSessionRecord.id == session_id)
                .with_for_update()
            )
            if agent_session is None:
                raise EntityNotFoundError("AgentSession", session_id)
            self._require_writable(agent_session)
            await session.execute(
                delete(AgentMessageRecord).where(AgentMessageRecord.session_id == session_id)
            )

    async def trim(self, session_id: str, max_items: int) -> tuple[int, int]:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        async with self._session_factory() as session, session.begin():
            agent_session = await session.scalar(
                select(AgentSessionRecord)
                .where(AgentSessionRecord.id == session_id)
                .with_for_update()
            )
            if agent_session is None:
                raise EntityNotFoundError("AgentSession", session_id)
            self._require_writable(agent_session)
            records = list(
                (
                    await session.scalars(
                        select(AgentMessageRecord)
                        .where(AgentMessageRecord.session_id == session_id)
                        .order_by(AgentMessageRecord.sequence.desc())
                    )
                ).all()
            )
            retained = min(len(records), max_items)
            stale_ids = [record.id for record in records[max_items:]]
            if stale_ids:
                await session.execute(
                    delete(AgentMessageRecord).where(AgentMessageRecord.id.in_(stale_ids))
                )
            return len(stale_ids), retained

    @staticmethod
    def _require_writable(session: AgentSessionRecord) -> None:
        if session.status in _TERMINAL_STATUSES:
            raise RepositoryConflictError(
                f"agent session {session.id!r} is closed with status {session.status!r}"
            )

    @staticmethod
    async def _validate_parents(
        session: AsyncSession,
        session_id: str,
        drafts: Sequence[TranscriptMessageDraft],
    ) -> None:
        parent_ids = {draft.parent_message_id for draft in drafts if draft.parent_message_id}
        if not parent_ids:
            return
        records = list(
            (
                await session.scalars(
                    select(AgentMessageRecord).where(AgentMessageRecord.id.in_(parent_ids))
                )
            ).all()
        )
        found = {record.id for record in records if record.session_id == session_id}
        missing = parent_ids - found
        if missing:
            raise RepositoryConflictError(
                f"parent transcript messages do not belong to session {session_id!r}: "
                f"{sorted(missing)!r}"
            )
