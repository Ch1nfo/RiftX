"""SQLAlchemy repositories for durable Agent Runtime state."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from math import ceil

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import (
    EntityNotFoundError,
    PentestBudgetExceededError,
    RepositoryConflictError,
)
from riftx.application.ports import ExecutionAdmissionIdentity, ToolCallIntentExecutionClaim
from riftx.domain import ApprovalStatus, ExecutorType, RunKind, RunStatus
from riftx.domain.base import utc_now
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    ApprovalDecision,
    CycleStatus,
    ProviderState,
    RunLease,
    RuntimeApprovalRequest,
    SessionStatus,
    ToolCallIntent,
    ToolCallStatus,
    UserInputRequest,
    UserInputStatus,
)

from .mappers import execution_from_record, run_from_record
from .mutation_clock import Clock, next_mutation_at
from .orm import (
    AgentCycleRecord,
    AgentRuntimeStepRecord,
    AgentSessionRecord,
    ContextCompilationRecord,
    ExecutionRecord,
    ProviderStateRecord,
    RunLeaseRecord,
    RunRecord,
    RuntimeApprovalRequestRecord,
    ToolCallIntentRecord,
    UserInputRequestRecord,
)
from .repositories import _serialized_run_write
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

    async def has_nonterminal_model_profile(self, profile_name: str) -> bool:
        terminal_statuses = {
            SessionStatus.COMPLETED.value,
            SessionStatus.FAILED.value,
            SessionStatus.CANCELLED.value,
        }
        statement = (
            select(AgentSessionRecord.id)
            .where(
                AgentSessionRecord.model_profile == profile_name,
                ~AgentSessionRecord.status.in_(terminal_statuses),
            )
            .limit(1)
        )
        async with self._session_factory() as session:
            return await session.scalar(statement) is not None


class SQLAlchemyAgentCycleRepository:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

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

    async def save_yield(
        self,
        agent_session: AgentSession,
        cycle: AgentCycle,
    ) -> AgentCycle:
        """Persist one yielded Cycle and its Session usage merge atomically."""

        if cycle.status is not CycleStatus.YIELDED:
            raise ValueError("save_yield requires a yielded AgentCycle")
        if (
            cycle.session_id != agent_session.id
            or cycle.run_id != agent_session.run_id
        ):
            raise RepositoryConflictError(
                "AgentCycle and AgentSession must belong to the same Runtime owner"
            )
        async with _serialized_run_write(self._session_factory) as session:
            session_record = await session.scalar(
                select(AgentSessionRecord)
                .where(AgentSessionRecord.id == agent_session.id)
                .with_for_update()
            )
            if session_record is None:
                raise EntityNotFoundError("AgentSession", agent_session.id)
            cycle_record = await session.scalar(
                select(AgentCycleRecord)
                .where(AgentCycleRecord.id == cycle.id)
                .with_for_update()
            )
            if cycle_record is None:
                raise EntityNotFoundError("AgentCycle", cycle.id)
            if (
                session_record.run_id != cycle_record.run_id
                or cycle_record.session_id != session_record.id
            ):
                raise RepositoryConflictError(
                    "Persisted AgentCycle and AgentSession ownership does not match"
                )
            apply_agent_session_to_record(agent_session, session_record)
            apply_agent_cycle_to_record(cycle, cycle_record)
            await session.flush()
        return cycle

    async def claim_pentest_model_call(
        self,
        *,
        run_id: str,
        cycle_id: str,
        compilation_id: str,
    ) -> AgentCycle:
        """Reserve one Pentest model call after checking prior durable usage."""

        async with _serialized_run_write(self._session_factory) as session:
            run_record = await session.scalar(
                select(RunRecord).where(RunRecord.id == run_id).with_for_update()
            )
            if run_record is None:
                raise EntityNotFoundError("Run", run_id)
            run = run_from_record(run_record)
            if run.kind is not RunKind.PENTEST or run.pentest_admission is None:
                raise RepositoryConflictError(
                    "Pentest model call claim requires a Pentest admission"
                )
            if run.status is not RunStatus.RUNNING:
                raise RepositoryConflictError(
                    "Pentest model call claim requires a running Run"
                )
            cycle_record = await session.scalar(
                select(AgentCycleRecord)
                .where(AgentCycleRecord.id == cycle_id)
                .with_for_update()
            )
            if cycle_record is None:
                raise EntityNotFoundError("AgentCycle", cycle_id)
            if cycle_record.run_id != run_id:
                raise RepositoryConflictError(
                    "AgentCycle is not owned by the Pentest Run"
                )
            compilation_record = await session.scalar(
                select(ContextCompilationRecord)
                .where(ContextCompilationRecord.id == compilation_id)
                .with_for_update()
            )
            if compilation_record is None:
                raise EntityNotFoundError("ContextCompilation", compilation_id)
            if (
                compilation_record.run_id != run_id
                or compilation_record.session_id != cycle_record.session_id
            ):
                raise RepositoryConflictError(
                    "ContextCompilation is not owned by the Pentest Cycle"
                )

            admission = run.pentest_admission
            elapsed = max(0.0, (self._clock() - run.created_at).total_seconds())
            if elapsed >= admission.budget.max_duration_seconds:
                raise PentestBudgetExceededError(
                    "max_duration_seconds",
                    limit=admission.budget.max_duration_seconds,
                    used=ceil(elapsed),
                )

            session_model_calls = int(
                await session.scalar(
                    select(func.coalesce(func.sum(AgentSessionRecord.model_call_count), 0))
                    .where(AgentSessionRecord.run_id == run_id)
                )
                or 0
            )
            unmerged_model_calls = int(
                await session.scalar(
                    select(func.coalesce(func.sum(AgentCycleRecord.model_call_count), 0))
                    .where(
                        AgentCycleRecord.run_id == run_id,
                        AgentCycleRecord.status != CycleStatus.YIELDED.value,
                    )
                )
                or 0
            )
            used_model_calls = session_model_calls + unmerged_model_calls
            if used_model_calls >= admission.budget.max_model_calls:
                raise PentestBudgetExceededError(
                    "max_model_calls",
                    limit=admission.budget.max_model_calls,
                    used=used_model_calls,
                )

            token_row = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        ContextCompilationRecord.actual_input_tokens.is_not(
                                            None
                                        )
                                        & ContextCompilationRecord.actual_output_tokens.is_not(
                                            None
                                        ),
                                        1,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        ContextCompilationRecord.actual_input_tokens.is_not(
                                            None
                                        )
                                        & ContextCompilationRecord.actual_output_tokens.is_not(
                                            None
                                        ),
                                        ContextCompilationRecord.actual_input_tokens,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        ContextCompilationRecord.actual_input_tokens.is_not(
                                            None
                                        )
                                        & ContextCompilationRecord.actual_output_tokens.is_not(
                                            None
                                        ),
                                        ContextCompilationRecord.actual_output_tokens,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                    ).where(
                        ContextCompilationRecord.run_id == run_id,
                        ContextCompilationRecord.id != compilation_id,
                    )
                )
            ).one()
            complete_compilations = int(token_row[0])
            used_tokens = int(token_row[1]) + int(token_row[2])
            if complete_compilations < used_model_calls:
                raise PentestBudgetExceededError(
                    "max_tokens",
                    limit=admission.budget.max_tokens,
                    used=used_tokens,
                    reason="token_usage_incomplete",
                )
            if used_tokens >= admission.budget.max_tokens:
                raise PentestBudgetExceededError(
                    "max_tokens",
                    limit=admission.budget.max_tokens,
                    used=used_tokens,
                )

            cycle_record.model_call_count += 1
            await session.flush()
            claimed = agent_cycle_from_record(cycle_record)
        return claimed

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
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def create(self, intent: ToolCallIntent) -> ToolCallIntent:
        try:
            async with _serialized_run_write(self._session_factory) as session:
                updated_at = next_mutation_at(
                    self._clock,
                    lifecycle_timestamps=(intent.created_at,),
                )
                session.add(tool_call_intent_to_record(intent, updated_at=updated_at))
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

    async def pending_for_session(self, session_id: str) -> list[ToolCallIntent]:
        statement = (
            select(ToolCallIntentRecord)
            .where(
                ToolCallIntentRecord.session_id == session_id,
                ToolCallIntentRecord.status.in_(
                    {
                        ToolCallStatus.WAITING_APPROVAL.value,
                        ToolCallStatus.READY.value,
                        ToolCallStatus.EXECUTING.value,
                    }
                ),
            )
            .order_by(
                ToolCallIntentRecord.created_at,
                ToolCallIntentRecord.id,
            )
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [tool_call_intent_from_record(record) for record in records]

    async def recent_for_session(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[ToolCallIntent]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        statement = (
            select(ToolCallIntentRecord)
            .where(ToolCallIntentRecord.session_id == session_id)
            .order_by(
                ToolCallIntentRecord.created_at.desc(),
                ToolCallIntentRecord.id.desc(),
            )
            .limit(limit)
        )
        async with self._session_factory() as session:
            records = list((await session.scalars(statement)).all())
        records.reverse()
        return [tool_call_intent_from_record(record) for record in records]

    async def active_for_run(
        self,
        run_id: str,
        *,
        tool_ids: Collection[str] | None = None,
    ) -> list[ToolCallIntent]:
        """List effect-capable intents so a Run stop can drain them explicitly."""

        if tool_ids is not None and not tool_ids:
            return []
        statement = select(ToolCallIntentRecord).where(
            ToolCallIntentRecord.run_id == run_id,
            ToolCallIntentRecord.status.in_(
                {
                    ToolCallStatus.READY.value,
                    ToolCallStatus.EXECUTING.value,
                }
            ),
        )
        if tool_ids is not None:
            statement = statement.where(ToolCallIntentRecord.tool_id.in_(set(tool_ids)))
        statement = statement.order_by(
            ToolCallIntentRecord.created_at,
            ToolCallIntentRecord.id,
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [tool_call_intent_from_record(record) for record in records]

    async def compare_and_set_status(
        self,
        intent_id: str,
        *,
        expected: Collection[ToolCallStatus],
        target: ToolCallStatus,
    ) -> tuple[ToolCallIntent, bool]:
        """Atomically transition status without overwriting a concurrent terminal state."""

        expected_values = {status.value for status in expected}
        if not expected_values:
            raise ValueError("expected Tool Call intent statuses cannot be empty")
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(ToolCallIntentRecord)
                .where(ToolCallIntentRecord.id == intent_id)
                .with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("ToolCallIntent", intent_id)
            if record.status not in expected_values:
                return tool_call_intent_from_record(record), False
            if record.status == target.value:
                return tool_call_intent_from_record(record), True
            record.status = target.value
            record.updated_at = next_mutation_at(
                self._clock,
                stored=record.updated_at,
                lifecycle_timestamps=(record.created_at,),
            )
            await session.flush()
            intent = tool_call_intent_from_record(record)
        return intent, True

    async def claim_execution(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
        target_interaction_tool_ids: Collection[str] | None = None,
    ) -> ToolCallIntentExecutionClaim:
        """Claim one exact execution identity before any Runner effect."""

        _validate_execution_claim_identity(execution_key, attempt_group)
        target_tool_ids = (
            frozenset(target_interaction_tool_ids)
            if target_interaction_tool_ids is not None
            else None
        )
        if target_tool_ids is not None and not target_tool_ids:
            raise ValueError("target interaction Tool ids cannot be empty")
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(ToolCallIntentRecord)
                .where(ToolCallIntentRecord.id == intent_id)
                .with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("ToolCallIntent", intent_id)
            exact_claim = (
                record.claimed_execution_key == execution_key
                and record.claimed_attempt_group == attempt_group
            )
            if record.status == ToolCallStatus.EXECUTING.value:
                return ToolCallIntentExecutionClaim(
                    intent=tool_call_intent_from_record(record),
                    acquired=exact_claim,
                    newly_acquired=False,
                    execution_key=execution_key,
                    attempt_group=attempt_group,
                )

            previous_status = ToolCallStatus(record.status)
            previous_execution_key = record.claimed_execution_key
            previous_attempt_group = record.claimed_attempt_group
            if previous_status is ToolCallStatus.READY:
                claimable = (
                    previous_execution_key is None and previous_attempt_group is None
                ) or exact_claim
            elif previous_status in _RETRYABLE_TERMINAL_INTENT_STATUSES:
                claimable = (
                    attempt_group != "initial"
                    and execution_key != previous_execution_key
                    and attempt_group != previous_attempt_group
                )
            else:
                claimable = False
            if not claimable:
                return ToolCallIntentExecutionClaim(
                    intent=tool_call_intent_from_record(record),
                    acquired=False,
                    newly_acquired=False,
                    execution_key=execution_key,
                    attempt_group=attempt_group,
                )

            run_record = await session.scalar(
                select(RunRecord)
                .where(RunRecord.id == record.run_id)
                .with_for_update()
            )
            if run_record is None:
                raise EntityNotFoundError("Run", record.run_id)
            run = run_from_record(run_record)
            cycle_record: AgentCycleRecord | None = None
            if run.kind is RunKind.PENTEST:
                admission = run.pentest_admission
                if admission is None:
                    raise RepositoryConflictError(
                        "Pentest Tool execution is missing its admission"
                    )
                if run.status is not RunStatus.RUNNING:
                    raise RepositoryConflictError(
                        "Pentest Tool execution claim requires a running Run"
                    )
                elapsed = max(0.0, (self._clock() - run.created_at).total_seconds())
                if elapsed >= admission.budget.max_duration_seconds:
                    raise PentestBudgetExceededError(
                        "max_duration_seconds",
                        limit=admission.budget.max_duration_seconds,
                        used=ceil(elapsed),
                    )
                session_tool_calls = int(
                    await session.scalar(
                        select(
                            func.coalesce(func.sum(AgentSessionRecord.tool_call_count), 0)
                        ).where(AgentSessionRecord.run_id == record.run_id)
                    )
                    or 0
                )
                unmerged_tool_calls = int(
                    await session.scalar(
                        select(
                            func.coalesce(func.sum(AgentCycleRecord.tool_call_count), 0)
                        ).where(
                            AgentCycleRecord.run_id == record.run_id,
                            AgentCycleRecord.status != CycleStatus.YIELDED.value,
                        )
                    )
                    or 0
                )
                used_tool_calls = session_tool_calls + unmerged_tool_calls
                if used_tool_calls >= admission.budget.max_tool_calls:
                    raise PentestBudgetExceededError(
                        "max_tool_calls",
                        limit=admission.budget.max_tool_calls,
                        used=used_tool_calls,
                    )
                cycle_record = await session.scalar(
                    select(AgentCycleRecord)
                    .where(AgentCycleRecord.id == record.cycle_id)
                    .with_for_update()
                )
                if (
                    cycle_record is None
                    or cycle_record.run_id != record.run_id
                    or cycle_record.session_id != record.session_id
                ):
                    raise RepositoryConflictError(
                        "Tool Call intent is not owned by its Pentest Cycle"
                    )

            if target_tool_ids is not None:
                if record.tool_id not in target_tool_ids:
                    raise RepositoryConflictError(
                        "Tool Call intent is not owned by the target interaction boundary"
                    )
                if run.kind is RunKind.PENTEST:
                    admission = run.pentest_admission
                    if admission is None:
                        raise RepositoryConflictError(
                            "Pentest target interaction is missing its admission"
                        )
                    has_target = ToolCallIntentRecord.tool_id.in_(target_tool_ids) | (
                        ToolCallIntentRecord.target_summary.is_not(None)
                        & (
                            func.length(func.trim(ToolCallIntentRecord.target_summary))
                            > 0
                        )
                    )
                    consumed = has_target & (
                        ToolCallIntentRecord.claimed_execution_key.is_not(None)
                        | ToolCallIntentRecord.status.in_(
                            {
                                ToolCallStatus.EXECUTING.value,
                                ToolCallStatus.COMPLETED.value,
                                ToolCallStatus.FAILED.value,
                            }
                        )
                    )
                    used_total = int(
                        await session.scalar(
                            select(func.count(ToolCallIntentRecord.id)).where(
                                ToolCallIntentRecord.run_id == record.run_id,
                                consumed,
                            )
                        )
                        or 0
                    )
                    if used_total >= admission.budget.max_target_interactions:
                        raise PentestBudgetExceededError(
                            "max_target_interactions",
                            limit=admission.budget.max_target_interactions,
                            used=used_total,
                        )
                    used_active = int(
                        await session.scalar(
                            select(func.count(ToolCallIntentRecord.id)).where(
                                ToolCallIntentRecord.run_id == record.run_id,
                                has_target,
                                ToolCallIntentRecord.status
                                == ToolCallStatus.EXECUTING.value,
                            )
                        )
                        or 0
                    )
                    if (
                        used_active
                        >= admission.budget.max_concurrent_target_interactions
                    ):
                        raise PentestBudgetExceededError(
                            "max_concurrent_target_interactions",
                            limit=(
                                admission.budget.max_concurrent_target_interactions
                            ),
                            used=used_active,
                            reason="capacity",
                        )

            record.status = ToolCallStatus.EXECUTING.value
            record.claimed_execution_key = execution_key
            record.claimed_attempt_group = attempt_group
            record.updated_at = next_mutation_at(
                self._clock,
                stored=record.updated_at,
                lifecycle_timestamps=(record.created_at,),
            )
            if cycle_record is not None:
                if cycle_record.status == CycleStatus.YIELDED.value:
                    session_record = await session.scalar(
                        select(AgentSessionRecord)
                        .where(AgentSessionRecord.id == cycle_record.session_id)
                        .with_for_update()
                    )
                    if session_record is None:
                        raise EntityNotFoundError(
                            "AgentSession", cycle_record.session_id
                        )
                    session_record.tool_call_count += 1
                else:
                    cycle_record.tool_call_count += 1
            await session.flush()
            return ToolCallIntentExecutionClaim(
                intent=tool_call_intent_from_record(record),
                acquired=True,
                newly_acquired=True,
                execution_key=execution_key,
                attempt_group=attempt_group,
                previous_status=previous_status,
                previous_execution_key=previous_execution_key,
                previous_attempt_group=previous_attempt_group,
            )

    async def execution_claim_is_current(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
    ) -> bool:
        _validate_execution_claim_identity(execution_key, attempt_group)
        statement = select(ToolCallIntentRecord.id).where(
            ToolCallIntentRecord.id == intent_id,
            ToolCallIntentRecord.status == ToolCallStatus.EXECUTING.value,
            ToolCallIntentRecord.claimed_execution_key == execution_key,
            ToolCallIntentRecord.claimed_attempt_group == attempt_group,
        )
        async with self._session_factory() as session:
            return await session.scalar(statement) is not None

    async def project_execution_status(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
        expected: Collection[ToolCallStatus],
        target: ToolCallStatus,
    ) -> tuple[ToolCallIntent, bool]:
        """Project one execution only while its exact claim is still current."""

        _validate_execution_claim_identity(execution_key, attempt_group)
        expected_values = {status.value for status in expected}
        if not expected_values:
            raise ValueError("expected Tool Call intent statuses cannot be empty")
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(ToolCallIntentRecord)
                .where(ToolCallIntentRecord.id == intent_id)
                .with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("ToolCallIntent", intent_id)
            exact_claim = (
                record.claimed_execution_key == execution_key
                and record.claimed_attempt_group == attempt_group
            )
            if not exact_claim:
                return tool_call_intent_from_record(record), False
            if record.status == target.value:
                return tool_call_intent_from_record(record), True
            if record.status not in expected_values:
                return tool_call_intent_from_record(record), False
            record.status = target.value
            record.updated_at = next_mutation_at(
                self._clock,
                stored=record.updated_at,
                lifecycle_timestamps=(record.created_at,),
            )
            await session.flush()
            intent = tool_call_intent_from_record(record)
        return intent, True

    async def adopt_execution_claim(
        self,
        intent_id: str,
        *,
        execution_id: str,
        execution_key: str,
        attempt_group: str,
    ) -> tuple[ToolCallIntent, bool]:
        """Adopt a claim-null legacy execution only when it is uniquely provable."""

        _validate_execution_claim_identity(execution_key, attempt_group)
        async with _serialized_run_write(self._session_factory) as session:
            intent_record = await session.scalar(
                select(ToolCallIntentRecord)
                .where(ToolCallIntentRecord.id == intent_id)
                .with_for_update()
            )
            if intent_record is None:
                raise EntityNotFoundError("ToolCallIntent", intent_id)
            exact_claim = (
                intent_record.claimed_execution_key == execution_key
                and intent_record.claimed_attempt_group == attempt_group
            )
            if exact_claim:
                return tool_call_intent_from_record(intent_record), True
            if (
                intent_record.claimed_execution_key is not None
                or intent_record.claimed_attempt_group is not None
            ):
                return tool_call_intent_from_record(intent_record), False

            executions = list(
                await session.scalars(
                    select(ExecutionRecord)
                    .where(
                        ExecutionRecord.run_id == intent_record.run_id,
                        ExecutionRecord.session_id == intent_record.session_id,
                        ExecutionRecord.tool_call_id == intent_id,
                    )
                    .with_for_update()
                )
            )
            if len(executions) != 1:
                return tool_call_intent_from_record(intent_record), False
            execution = executions[0]
            legacy_initial_pty = (
                execution.attempt_group is None
                and attempt_group == "initial"
                and execution.executor_type == ExecutorType.PTY.value
            )
            if (
                execution.id != execution_id
                or execution.execution_key != execution_key
                or (execution.attempt_group != attempt_group and not legacy_initial_pty)
            ):
                return tool_call_intent_from_record(intent_record), False

            if legacy_initial_pty:
                execution.attempt_group = attempt_group
                execution.updated_at = next_mutation_at(
                    self._clock,
                    stored=execution.updated_at,
                    lifecycle_timestamps=(execution.created_at,),
                )

            intent_record.claimed_execution_key = execution_key
            intent_record.claimed_attempt_group = attempt_group
            intent_record.updated_at = next_mutation_at(
                self._clock,
                stored=intent_record.updated_at,
                lifecycle_timestamps=(intent_record.created_at,),
            )
            await session.flush()
            intent = tool_call_intent_from_record(intent_record)
        return intent, True

    async def rollback_execution_claim(
        self,
        claim: ToolCallIntentExecutionClaim,
        *,
        admission: ExecutionAdmissionIdentity,
    ) -> tuple[ToolCallIntent, bool]:
        """Restore the pre-claim state only while the exact claim still owns EXECUTING."""

        if not claim.acquired or not claim.newly_acquired or claim.previous_status is None:
            raise ValueError("only a newly acquired execution claim can be rolled back")
        _validate_execution_claim_identity(claim.execution_key, claim.attempt_group)
        if (
            admission.execution_key != claim.execution_key
            or admission.run_id != claim.intent.run_id
            or admission.session_id != claim.intent.session_id
            or admission.tool_call_id != claim.intent.id
            or admission.attempt_group != claim.attempt_group
        ):
            raise ValueError("execution admission identity does not match the claimed Tool Call")
        if (claim.previous_execution_key is None) != (claim.previous_attempt_group is None):
            raise ValueError("previous execution claim identity must be complete or absent")
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(ToolCallIntentRecord)
                .where(ToolCallIntentRecord.id == claim.intent.id)
                .with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("ToolCallIntent", claim.intent.id)
            admission_candidates = [
                ExecutionRecord.execution_key == admission.execution_key,
            ]
            if admission.execution_id is not None:
                admission_candidates.append(ExecutionRecord.id == admission.execution_id)
            candidate_records = (
                await session.scalars(
                    select(ExecutionRecord).where(or_(*admission_candidates)).with_for_update()
                )
            ).all()
            if any(
                admission.matches(execution_from_record(candidate))
                for candidate in candidate_records
            ):
                return tool_call_intent_from_record(record), False
            if (
                record.status != ToolCallStatus.EXECUTING.value
                or record.claimed_execution_key != claim.execution_key
                or record.claimed_attempt_group != claim.attempt_group
            ):
                return tool_call_intent_from_record(record), False
            record.status = claim.previous_status.value
            record.claimed_execution_key = claim.previous_execution_key
            record.claimed_attempt_group = claim.previous_attempt_group
            record.updated_at = next_mutation_at(
                self._clock,
                stored=record.updated_at,
                lifecycle_timestamps=(record.created_at,),
            )
            await session.flush()
            intent = tool_call_intent_from_record(record)
        return intent, True

    async def save(self, intent: ToolCallIntent) -> ToolCallIntent:
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(ToolCallIntentRecord)
                .where(ToolCallIntentRecord.id == intent.id)
                .with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("ToolCallIntent", intent.id)
            before = _tool_call_intent_metadata_state(record)
            apply_tool_call_intent_to_record(intent, record)
            if _tool_call_intent_metadata_state(record) == before:
                return tool_call_intent_from_record(record)
            record.updated_at = next_mutation_at(
                self._clock,
                stored=record.updated_at,
                lifecycle_timestamps=(record.created_at,),
            )
            await session.flush()
            authoritative = tool_call_intent_from_record(record)
        return authoritative


_TOOL_CALL_INTENT_MUTABLE_RECORD_FIELDS = (
    "engine_call_id",
    "command_preview",
    "reason",
    "target_summary",
    "execution_spec_json",
)

_RETRYABLE_TERMINAL_INTENT_STATUSES = {
    ToolCallStatus.COMPLETED,
    ToolCallStatus.FAILED,
    ToolCallStatus.CANCELLED,
}


def _validate_execution_claim_identity(execution_key: str, attempt_group: str) -> None:
    if not execution_key or len(execution_key) > 255:
        raise ValueError("execution claim key must contain 1-255 characters")
    if not attempt_group or len(attempt_group) > 64:
        raise ValueError("execution claim attempt group must contain 1-64 characters")


def _tool_call_intent_metadata_state(record: ToolCallIntentRecord) -> tuple[object, ...]:
    return tuple(
        getattr(record, field_name) for field_name in _TOOL_CALL_INTENT_MUTABLE_RECORD_FIELDS
    )


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

    async def set_provider_state_id(
        self,
        approval_id: str,
        provider_state_id: str | None,
    ) -> RuntimeApprovalRequest:
        try:
            async with _serialized_run_write(self._session_factory) as session:
                record = await session.scalar(
                    select(RuntimeApprovalRequestRecord)
                    .where(RuntimeApprovalRequestRecord.id == approval_id)
                    .with_for_update()
                )
                if record is None:
                    raise EntityNotFoundError("RuntimeApprovalRequest", approval_id)
                if record.provider_state_id != provider_state_id:
                    record.provider_state_id = provider_state_id
                    await session.flush()
                request = runtime_approval_from_record(record)
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not set provider state for runtime approval {approval_id!r}"
            ) from exc
        return request

    async def decide_if_pending(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        decided_by: str,
        feedback: str | None = None,
        decided_at: datetime,
    ) -> tuple[RuntimeApprovalRequest, bool]:
        if decision is ApprovalDecision.REJECT_WITH_FEEDBACK and not feedback:
            raise ValueError("reject_with_feedback requires feedback")
        if decided_at.tzinfo is None or decided_at.utcoffset() is None:
            raise ValueError("runtime approval decided_at must be timezone-aware")
        normalized_decided_at = decided_at.astimezone(UTC)
        target = (
            ApprovalStatus.APPROVED
            if decision
            in {
                ApprovalDecision.APPROVE_ONCE,
                ApprovalDecision.APPROVE_TOOL_FOR_RUN,
            }
            else ApprovalStatus.REJECTED
        )
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(RuntimeApprovalRequestRecord)
                .where(RuntimeApprovalRequestRecord.id == approval_id)
                .with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("RuntimeApprovalRequest", approval_id)
            if record.status != ApprovalStatus.PENDING.value:
                return runtime_approval_from_record(record), False
            record.status = target.value
            record.decision = decision.value
            record.feedback = feedback
            record.decided_by = decided_by
            record.decided_at = normalized_decided_at
            await session.flush()
            request = runtime_approval_from_record(record)
        return request, True

    async def pending_for_run(self, run_id: str) -> list[RuntimeApprovalRequest]:
        statement = (
            select(RuntimeApprovalRequestRecord)
            .where(
                RuntimeApprovalRequestRecord.run_id == run_id,
                RuntimeApprovalRequestRecord.status == ApprovalStatus.PENDING.value,
            )
            .order_by(
                RuntimeApprovalRequestRecord.created_at,
                RuntimeApprovalRequestRecord.id,
            )
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [runtime_approval_from_record(record) for record in records]


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
