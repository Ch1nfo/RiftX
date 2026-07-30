"""SQLAlchemy repositories for durable Agent Runtime state."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    ProviderState,
    RunLease,
    RuntimeApprovalRequest,
    ToolCallIntent,
    UserInputRequest,
    UserInputStatus,
)

from .orm import (
    AgentCycleRecord,
    AgentRuntimeStepRecord,
    AgentSessionRecord,
    ProviderStateRecord,
    RunLeaseRecord,
    RuntimeApprovalRequestRecord,
    ToolCallIntentRecord,
    UserInputRequestRecord,
)
from .runtime_mappers import (
    agent_cycle_from_record,
    agent_cycle_to_record,
    agent_session_from_record,
    agent_session_to_record,
    agent_step_from_record,
    agent_step_to_record,
    apply_agent_cycle_to_record,
    apply_agent_session_to_record,
    apply_agent_step_to_record,
    apply_runtime_approval_to_record,
    apply_tool_call_intent_to_record,
    apply_user_input_request_to_record,
    provider_state_from_record,
    provider_state_to_record,
    run_lease_from_record,
    run_lease_to_record,
    runtime_approval_from_record,
    runtime_approval_to_record,
    tool_call_intent_from_record,
    tool_call_intent_to_record,
    user_input_request_from_record,
    user_input_request_to_record,
)

SessionFactory = async_sessionmaker[AsyncSession]


class SQLAlchemyAgentSessionRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, agent_session: AgentSession) -> AgentSession:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(agent_session_to_record(agent_session))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not create agent session {agent_session.id!r}"
            ) from exc
        return agent_session

    async def get(self, session_id: str) -> AgentSession | None:
        async with self._session_factory() as session:
            record = await session.get(AgentSessionRecord, session_id)
        return agent_session_from_record(record) if record is not None else None

    async def save(self, agent_session: AgentSession) -> AgentSession:
        async with self._session_factory() as session, session.begin():
            record = await session.get(AgentSessionRecord, agent_session.id)
            if record is None:
                raise EntityNotFoundError("AgentSession", agent_session.id)
            apply_agent_session_to_record(agent_session, record)
            await session.flush()
        return agent_session

    async def list_by_run(self, run_id: str) -> Sequence[AgentSession]:
        statement = (
            select(AgentSessionRecord)
            .where(AgentSessionRecord.run_id == run_id)
            .order_by(AgentSessionRecord.created_at, AgentSessionRecord.id)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [agent_session_from_record(record) for record in records]


class SQLAlchemyAgentCycleRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, cycle: AgentCycle) -> AgentCycle:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(agent_cycle_to_record(cycle))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create agent cycle {cycle.id!r}") from exc
        return cycle

    async def get(self, cycle_id: str) -> AgentCycle | None:
        async with self._session_factory() as session:
            record = await session.get(AgentCycleRecord, cycle_id)
        return agent_cycle_from_record(record) if record is not None else None

    async def save(self, cycle: AgentCycle) -> AgentCycle:
        async with self._session_factory() as session, session.begin():
            record = await session.get(AgentCycleRecord, cycle.id)
            if record is None:
                raise EntityNotFoundError("AgentCycle", cycle.id)
            apply_agent_cycle_to_record(cycle, record)
            await session.flush()
        return cycle

    async def list_by_session(self, session_id: str) -> Sequence[AgentCycle]:
        statement = (
            select(AgentCycleRecord)
            .where(AgentCycleRecord.session_id == session_id)
            .order_by(AgentCycleRecord.sequence)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [agent_cycle_from_record(record) for record in records]


class SQLAlchemyAgentStepRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, step: AgentStep) -> AgentStep:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(agent_step_to_record(step))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create agent step {step.id!r}") from exc
        return step

    async def get(self, step_id: str) -> AgentStep | None:
        async with self._session_factory() as session:
            record = await session.get(AgentRuntimeStepRecord, step_id)
        return agent_step_from_record(record) if record is not None else None

    async def save(self, step: AgentStep) -> AgentStep:
        async with self._session_factory() as session, session.begin():
            record = await session.get(AgentRuntimeStepRecord, step.id)
            if record is None:
                raise EntityNotFoundError("AgentStep", step.id)
            apply_agent_step_to_record(step, record)
            await session.flush()
        return step

    async def list_by_cycle(self, cycle_id: str) -> Sequence[AgentStep]:
        statement = (
            select(AgentRuntimeStepRecord)
            .where(AgentRuntimeStepRecord.cycle_id == cycle_id)
            .order_by(AgentRuntimeStepRecord.sequence)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [agent_step_from_record(record) for record in records]


class SQLAlchemyProviderStateRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, state: ProviderState) -> ProviderState:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(provider_state_to_record(state))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create provider state {state.id!r}") from exc
        return state

    async def get(self, state_id: str) -> ProviderState | None:
        async with self._session_factory() as session:
            record = await session.get(ProviderStateRecord, state_id)
        return provider_state_from_record(record) if record is not None else None

    async def latest_for_session(self, session_id: str) -> ProviderState | None:
        statement = (
            select(ProviderStateRecord)
            .where(ProviderStateRecord.session_id == session_id)
            .order_by(ProviderStateRecord.created_at.desc(), ProviderStateRecord.id.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return provider_state_from_record(record) if record is not None else None


class SQLAlchemyToolCallIntentRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, intent: ToolCallIntent) -> ToolCallIntent:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(tool_call_intent_to_record(intent))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not create tool call intent {intent.id!r}"
            ) from exc
        return intent

    async def get(self, intent_id: str) -> ToolCallIntent | None:
        async with self._session_factory() as session:
            record = await session.get(ToolCallIntentRecord, intent_id)
        return tool_call_intent_from_record(record) if record is not None else None

    async def save(self, intent: ToolCallIntent) -> ToolCallIntent:
        async with self._session_factory() as session, session.begin():
            record = await session.get(ToolCallIntentRecord, intent.id)
            if record is None:
                raise EntityNotFoundError("ToolCallIntent", intent.id)
            apply_tool_call_intent_to_record(intent, record)
            await session.flush()
        return intent


class SQLAlchemyRuntimeApprovalRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, request: RuntimeApprovalRequest) -> RuntimeApprovalRequest:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(runtime_approval_to_record(request))
                await session.flush()
            return request
        except IntegrityError as exc:
            existing = await self.get_for_intent(request.tool_call_intent_id)
            if existing is not None:
                return existing
            raise RepositoryConflictError(
                f"could not create runtime approval {request.id!r}"
            ) from exc

    async def get(self, approval_id: str) -> RuntimeApprovalRequest | None:
        async with self._session_factory() as session:
            record = await session.get(RuntimeApprovalRequestRecord, approval_id)
        return runtime_approval_from_record(record) if record is not None else None

    async def get_for_intent(self, intent_id: str) -> RuntimeApprovalRequest | None:
        statement = select(RuntimeApprovalRequestRecord).where(
            RuntimeApprovalRequestRecord.tool_call_intent_id == intent_id
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return runtime_approval_from_record(record) if record is not None else None

    async def save(self, request: RuntimeApprovalRequest) -> RuntimeApprovalRequest:
        async with self._session_factory() as session, session.begin():
            record = await session.get(RuntimeApprovalRequestRecord, request.id)
            if record is None:
                raise EntityNotFoundError("RuntimeApprovalRequest", request.id)
            apply_runtime_approval_to_record(request, record)
            await session.flush()
        return request


class SQLAlchemyUserInputRequestRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, request: UserInputRequest) -> UserInputRequest:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(user_input_request_to_record(request))
                await session.flush()
            return request
        except IntegrityError as exc:
            existing = await self.get_for_cycle(request.cycle_id)
            if existing is not None:
                return existing
            raise RepositoryConflictError(
                f"could not create user input request {request.id!r}"
            ) from exc

    async def get(self, request_id: str) -> UserInputRequest | None:
        async with self._session_factory() as session:
            record = await session.get(UserInputRequestRecord, request_id)
        return user_input_request_from_record(record) if record is not None else None

    async def get_for_cycle(self, cycle_id: str) -> UserInputRequest | None:
        statement = select(UserInputRequestRecord).where(
            UserInputRequestRecord.cycle_id == cycle_id
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return user_input_request_from_record(record) if record is not None else None

    async def pending_for_session(
        self,
        run_id: str,
        session_id: str,
    ) -> UserInputRequest | None:
        statement = (
            select(UserInputRequestRecord)
            .where(
                UserInputRequestRecord.run_id == run_id,
                UserInputRequestRecord.session_id == session_id,
                UserInputRequestRecord.status == UserInputStatus.WAITING.value,
            )
            .order_by(
                UserInputRequestRecord.created_at.desc(),
                UserInputRequestRecord.id.desc(),
            )
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return user_input_request_from_record(record) if record is not None else None

    async def save(self, request: UserInputRequest) -> UserInputRequest:
        async with self._session_factory() as session, session.begin():
            record = await session.get(UserInputRequestRecord, request.id)
            if record is None:
                raise EntityNotFoundError("UserInputRequest", request.id)
            apply_user_input_request_to_record(request, record)
            await session.flush()
        return request

class SQLAlchemyRunLeaseRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def acquire(self, lease: RunLease) -> RunLease:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(run_lease_to_record(lease))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"run {lease.run_id!r} already has an active lease"
            ) from exc
        return lease

    async def get(self, run_id: str) -> RunLease | None:
        async with self._session_factory() as session:
            record = await session.get(RunLeaseRecord, run_id)
        return run_lease_from_record(record) if record is not None else None

    async def save(self, lease: RunLease, *, expected_version: int) -> RunLease:
        next_version = expected_version + 1
        statement = (
            update(RunLeaseRecord)
            .where(
                RunLeaseRecord.run_id == lease.run_id,
                RunLeaseRecord.owner_id == lease.owner_id,
                RunLeaseRecord.version == expected_version,
            )
            .values(
                acquired_at=lease.acquired_at,
                expires_at=lease.expires_at,
                heartbeat_at=lease.heartbeat_at,
                version=next_version,
            )
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(statement)
            if result.rowcount != 1:
                raise RepositoryConflictError(
                    f"run lease {lease.run_id!r} version conflict; expected {expected_version}"
                )
        return lease.model_copy(update={"version": next_version})

    async def release(self, run_id: str, *, owner_id: str, expected_version: int) -> None:
        statement = delete(RunLeaseRecord).where(
            RunLeaseRecord.run_id == run_id,
            RunLeaseRecord.owner_id == owner_id,
            RunLeaseRecord.version == expected_version,
        )
        async with self._session_factory() as session, session.begin():
            result = await session.execute(statement)
            if result.rowcount != 1:
                raise RepositoryConflictError(
                    f"run lease {run_id!r} release conflict; expected version {expected_version}"
                )
