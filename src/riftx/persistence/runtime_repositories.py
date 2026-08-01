"""SQLAlchemy repositories for durable Agent Runtime state."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.application.ports import ExecutionAdmissionIdentity, ToolCallIntentExecutionClaim
from riftx.domain import ApprovalStatus, ExecutorType
from riftx.domain.base import utc_now
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    ApprovalDecision,
    ProviderState,
    RunLease,
    RuntimeApprovalRequest,
    SessionStatus,
    ToolCallIntent,
    ToolCallStatus,
    UserInputRequest,
    UserInputStatus,
)

from .mappers import execution_from_record
from .mutation_clock import Clock, next_mutation_at
from .orm import (
    AgentCycleRecord,
    AgentRuntimeStepRecord,
    AgentSessionRecord,
    ExecutionRecord,
    ProviderStateRecord,
    RunLeaseRecord,
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
    ) -> ToolCallIntentExecutionClaim:
        """Claim one exact execution identity before any Runner effect."""

        _validate_execution_claim_identity(execution_key, attempt_group)
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

            record.status = ToolCallStatus.EXECUTING.value
            record.claimed_execution_key = execution_key
            record.claimed_attempt_group = attempt_group
            record.updated_at = next_mutation_at(
                self._clock,
                stored=record.updated_at,
                lifecycle_timestamps=(record.created_at,),
            )
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
