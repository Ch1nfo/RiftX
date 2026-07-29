"""SQLAlchemy implementations of RiftX repository ports."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain import Engagement, Execution, ExecutionStatus, Run, RunEvent, RunStatus
from riftx.domain.base import utc_now

from .mappers import (
    apply_execution_to_record,
    apply_run_to_record,
    engagement_from_record,
    engagement_to_record,
    event_from_record,
    event_to_record,
    execution_from_record,
    execution_to_record,
    run_from_record,
    run_to_record,
)
from .orm import EngagementRecord, ExecutionRecord, RunEventRecord, RunRecord

SessionFactory = async_sessionmaker[AsyncSession]


class SQLAlchemyEngagementRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, engagement: Engagement) -> Engagement:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(engagement_to_record(engagement))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create engagement {engagement.id!r}") from exc
        return engagement

    async def get(self, engagement_id: str) -> Engagement | None:
        async with self._session_factory() as session:
            record = await session.get(EngagementRecord, engagement_id)
            return engagement_from_record(record) if record else None


class SQLAlchemyRunRepository:
    """Persist Run aggregates and their lifecycle events atomically."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, run: Run) -> Run:
        event = RunEvent(
            run_id=run.id,
            sequence=1,
            event_type="run.created",
            payload={"status": run.status.value},
            created_at=run.created_at,
        )
        try:
            async with self._session_factory() as session, session.begin():
                session.add(run_to_record(run))
                await session.flush()
                session.add(event_to_record(event))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create run {run.id!r}") from exc
        return run

    async def get(self, run_id: str) -> Run | None:
        async with self._session_factory() as session:
            record = await session.get(RunRecord, run_id)
            return run_from_record(record) if record else None

    async def list(
        self,
        *,
        status: RunStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Run]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")

        statement = select(RunRecord).order_by(RunRecord.created_at.desc())
        if status is not None:
            statement = statement.where(RunRecord.status == status.value)
        statement = statement.limit(limit).offset(offset)

        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [run_from_record(record) for record in records]

    async def update_status(self, run_id: str, target: RunStatus) -> Run:
        try:
            async with self._session_factory() as session, session.begin():
                statement = select(RunRecord).where(RunRecord.id == run_id).with_for_update()
                record = await session.scalar(statement)
                if record is None:
                    raise EntityNotFoundError("Run", run_id)

                run = run_from_record(record)
                previous = run.status
                changed_at = utc_now()
                run.transition_to(target, at=changed_at)
                apply_run_to_record(run, record)

                sequence = await _next_event_sequence(session, run_id)
                session.add(
                    event_to_record(
                        RunEvent(
                            run_id=run_id,
                            sequence=sequence,
                            event_type="run.status_changed",
                            payload={"from": previous.value, "to": target.value},
                            created_at=changed_at,
                        )
                    )
                )
                await session.flush()
                return run
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not update status for run {run_id!r}") from exc


class SQLAlchemyRunEventRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> RunEvent:
        try:
            async with self._session_factory() as session, session.begin():
                run_exists = await session.scalar(
                    select(RunRecord.id).where(RunRecord.id == run_id).with_for_update()
                )
                if run_exists is None:
                    raise EntityNotFoundError("Run", run_id)

                event = RunEvent(
                    run_id=run_id,
                    sequence=await _next_event_sequence(session, run_id),
                    event_type=event_type,
                    payload=payload or {},
                )
                session.add(event_to_record(event))
                await session.flush()
                return event
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not append event for run {run_id!r}") from exc

    async def list_after(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Sequence[RunEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")

        statement = (
            select(RunEventRecord)
            .where(
                RunEventRecord.run_id == run_id,
                RunEventRecord.sequence > after_sequence,
            )
            .order_by(RunEventRecord.sequence)
            .limit(limit)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [event_from_record(record) for record in records]


class SQLAlchemyExecutionRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create_if_absent(self, execution: Execution) -> tuple[Execution, bool]:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(execution_to_record(execution))
                await session.flush()
            return execution, True
        except IntegrityError as exc:
            existing = await self.get_by_key(execution.execution_key)
            if existing is not None:
                return existing, False
            raise RepositoryConflictError(f"could not create execution {execution.id!r}") from exc

    async def get(self, execution_id: str) -> Execution | None:
        async with self._session_factory() as session:
            record = await session.get(ExecutionRecord, execution_id)
            return execution_from_record(record) if record else None

    async def get_by_key(self, execution_key: str) -> Execution | None:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(ExecutionRecord).where(ExecutionRecord.execution_key == execution_key)
            )
            return execution_from_record(record) if record else None

    async def save(self, execution: Execution) -> Execution:
        async with self._session_factory() as session, session.begin():
            record = await session.scalar(
                select(ExecutionRecord).where(ExecutionRecord.id == execution.id).with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("Execution", execution.id)
            apply_execution_to_record(execution, record)
            await session.flush()
        return execution

    async def list_active(self) -> Sequence[Execution]:
        statement = (
            select(ExecutionRecord)
            .where(
                ExecutionRecord.status.in_(
                    [ExecutionStatus.STARTING.value, ExecutionStatus.RUNNING.value]
                )
            )
            .order_by(ExecutionRecord.started_at, ExecutionRecord.id)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [execution_from_record(record) for record in records]


async def _next_event_sequence(session: AsyncSession, run_id: str) -> int:
    current = await session.scalar(
        select(func.max(RunEventRecord.sequence)).where(RunEventRecord.run_id == run_id)
    )
    return (current or 0) + 1
