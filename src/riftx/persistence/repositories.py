"""SQLAlchemy implementations of RiftX repository ports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import (
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryDecisionConflictError,
)
from riftx.application.finalization import (
    FINALIZATION_INTENT_EVENT_TYPE,
    FINALIZATION_TARGETS,
    RunFinalizationIntent,
    cleanup_event_id,
    cleanup_event_payload,
    report_failure_event_id,
    resolve_finalization_intent,
)
from riftx.application.ports import ExecutionAdmissionIdentity
from riftx.domain import (
    Approval,
    ApprovalDecision,
    ApprovalGrant,
    ApprovalStatus,
    Artifact,
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Finding,
    FindingSeverity,
    FindingStatus,
    Node,
    NodeStatus,
    Report,
    ReportFormat,
    Run,
    RunEvent,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandStatus,
    RunnerCredential,
    RunnerPrincipal,
    RunStatus,
    TerminalSession,
    TerminalStatus,
    ToolCall,
)
from riftx.domain.base import utc_now

from .mappers import (
    apply_approval_to_record,
    apply_execution_to_record,
    apply_finding_to_record,
    apply_node_to_record,
    apply_run_to_record,
    apply_runner_credential_to_record,
    apply_terminal_to_record,
    approval_from_record,
    approval_grant_from_record,
    approval_grant_to_record,
    approval_to_record,
    artifact_from_record,
    artifact_to_record,
    engagement_from_record,
    engagement_to_record,
    event_from_record,
    event_to_record,
    execution_from_record,
    execution_to_record,
    finding_from_record,
    finding_to_record,
    node_from_record,
    node_to_record,
    report_from_record,
    report_to_record,
    run_from_record,
    run_to_record,
    runner_command_from_record,
    runner_command_to_record,
    runner_credential_from_record,
    runner_credential_to_record,
    terminal_from_record,
    terminal_to_record,
    tool_call_from_record,
    tool_call_to_record,
)
from .mutation_clock import Clock, next_mutation_at
from .orm import (
    ApprovalGrantRecord,
    ApprovalRecord,
    ArtifactRecord,
    EngagementRecord,
    ExecutionRecord,
    FindingRecord,
    NodeRecord,
    ReportRecord,
    RunEventRecord,
    RunnerCommandRecord,
    RunnerCredentialRecord,
    RunRecord,
    RuntimeApprovalRequestRecord,
    TerminalSessionRecord,
    ToolCallRecord,
)

SessionFactory = async_sessionmaker[AsyncSession]


@asynccontextmanager
async def _serialized_run_write(
    session_factory: SessionFactory,
) -> AsyncIterator[AsyncSession]:
    """Start a transaction that serializes a Run read-before-write on SQLite.

    ``SELECT ... FOR UPDATE`` provides the row-level lock used by the Run
    repositories on server databases, but SQLite ignores that clause.  Worse,
    pysqlite defers ``BEGIN`` until the first write, so a nominal SQLAlchemy
    transaction can read the Run and its events without holding any database
    lock.  A competing writer may then commit between that read and our first
    write, allowing both sides of a lifecycle fence to win.

    ``BEGIN IMMEDIATE`` obtains SQLite's RESERVED writer lock before the first
    read.  Competing writers wait and then re-read committed state, making the
    whole read/decision/write unit linearizable.  Other dialects retain the
    normal SQLAlchemy transaction and their row lock semantics.
    """

    async with session_factory() as session:
        if session.get_bind().dialect.name == "sqlite":
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()
            return

        async with session.begin():
            yield session


class SQLAlchemyArtifactRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, artifact: Artifact) -> Artifact:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(artifact_to_record(artifact))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create artifact {artifact.id!r}") from exc
        return artifact

    async def get(self, artifact_id: str) -> Artifact | None:
        async with self._session_factory() as session:
            record = await session.get(ArtifactRecord, artifact_id)
        return artifact_from_record(record) if record is not None else None

    async def list(
        self,
        run_id: str,
        *,
        execution_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Artifact]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")

        statement = select(ArtifactRecord).where(ArtifactRecord.run_id == run_id)
        if execution_id is not None:
            statement = statement.where(ArtifactRecord.execution_id == execution_id)
        statement = (
            statement.order_by(ArtifactRecord.created_at, ArtifactRecord.id)
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [artifact_from_record(record) for record in records]


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


class SQLAlchemyNodeRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, node: Node) -> Node:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(node_to_record(node))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create node {node.id!r}") from exc
        return node

    async def get(self, node_id: str) -> Node | None:
        async with self._session_factory() as session:
            record = await session.get(NodeRecord, node_id)
        return node_from_record(record) if record is not None else None

    async def save(self, node: Node) -> Node:
        async with self._session_factory() as session, session.begin():
            record = await session.get(NodeRecord, node.id)
            if record is None:
                raise EntityNotFoundError("Node", node.id)
            apply_node_to_record(node, record)
            await session.flush()
        return node

    async def list(
        self,
        *,
        status: NodeStatus | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[Node]:
        statement = select(NodeRecord).order_by(NodeRecord.name, NodeRecord.id)
        if status is not None:
            statement = statement.where(NodeRecord.status == status.value)
        statement = statement.limit(limit).offset(offset)
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [node_from_record(record) for record in records]


class SQLAlchemyRunnerCredentialRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def issue(
        self,
        node_id: str,
        *,
        token_hash: str,
        token_prefix: str,
        issued_at: datetime,
        instance_id: str | None = None,
    ) -> RunnerCredential:
        """Atomically advance a node epoch and persist its new owner credential."""

        try:
            async with _serialized_run_write(self._session_factory) as session:
                node = await session.scalar(
                    select(NodeRecord).where(NodeRecord.id == node_id).with_for_update()
                )
                if node is None:
                    raise EntityNotFoundError("Node", node_id)
                principal = RunnerPrincipal(
                    instance_id=instance_id or str(uuid4()),
                    epoch=node.current_runner_epoch + 1,
                )
                credential = RunnerCredential(
                    node_id=node_id,
                    principal=principal,
                    token_hash=token_hash,
                    token_prefix=token_prefix,
                    created_at=issued_at,
                    rotated_at=issued_at,
                )
                session.add(runner_credential_to_record(credential))
                node.current_runner_instance_id = principal.instance_id
                node.current_runner_epoch = principal.epoch
                node.updated_at = issued_at
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not issue Runner credential for node {node_id!r}"
            ) from exc
        return credential

    async def get(self, node_id: str) -> RunnerCredential | None:
        """Compatibility alias for the node's current owner credential."""

        return await self.get_current(node_id)

    async def get_current(self, node_id: str) -> RunnerCredential | None:
        statement = (
            select(RunnerCredentialRecord)
            .join(
                NodeRecord,
                and_(
                    NodeRecord.id == RunnerCredentialRecord.node_id,
                    NodeRecord.current_runner_instance_id
                    == RunnerCredentialRecord.runner_instance_id,
                    NodeRecord.current_runner_epoch == RunnerCredentialRecord.runner_epoch,
                ),
            )
            .where(NodeRecord.id == node_id)
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return runner_credential_from_record(record) if record is not None else None

    async def get_by_principal(
        self,
        node_id: str,
        principal: RunnerPrincipal,
    ) -> RunnerCredential | None:
        statement = select(RunnerCredentialRecord).where(
            RunnerCredentialRecord.node_id == node_id,
            RunnerCredentialRecord.runner_instance_id == principal.instance_id,
            RunnerCredentialRecord.runner_epoch == principal.epoch,
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return runner_credential_from_record(record) if record is not None else None

    async def get_by_token_hash(
        self,
        node_id: str,
        token_hash: str,
    ) -> RunnerCredential | None:
        statement = select(RunnerCredentialRecord).where(
            RunnerCredentialRecord.node_id == node_id,
            RunnerCredentialRecord.token_hash == token_hash,
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return runner_credential_from_record(record) if record is not None else None

    async def save(self, credential: RunnerCredential) -> RunnerCredential:
        async with self._session_factory() as session, session.begin():
            record = await session.get(
                RunnerCredentialRecord,
                credential.principal.instance_id,
                with_for_update=True,
            )
            if record is None:
                raise EntityNotFoundError(
                    "RunnerCredential",
                    credential.principal.instance_id,
                )
            apply_runner_credential_to_record(credential, record)
            await session.flush()
        return credential


class SQLAlchemyRunnerCommandRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def enqueue(self, command: RunnerCommand) -> tuple[RunnerCommand, bool]:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(runner_command_to_record(command))
                await session.flush()
            return command, True
        except IntegrityError as exc:
            statement = select(RunnerCommandRecord).where(
                RunnerCommandRecord.node_id == command.node_id,
                RunnerCommandRecord.idempotency_key == command.idempotency_key,
            )
            async with self._session_factory() as session:
                existing = await session.scalar(statement)
            if existing is not None:
                return runner_command_from_record(existing), False
            raise RepositoryConflictError(
                f"could not enqueue runner command {command.id!r}"
            ) from exc

    async def get(self, command_id: str) -> RunnerCommand | None:
        async with self._session_factory() as session:
            record = await session.get(RunnerCommandRecord, command_id)
        return runner_command_from_record(record) if record is not None else None

    async def lease_next(
        self,
        node_id: str,
        *,
        principal: RunnerPrincipal,
        lease_id: str,
        leased_until: datetime,
        now: datetime,
        safety_only: bool = False,
    ) -> RunnerCommand | None:
        if leased_until <= now:
            raise ValueError("leased_until must be later than now")
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(RunnerCommandRecord)
                .where(
                    RunnerCommandRecord.node_id == node_id,
                    RunnerCommandRecord.target_runner_instance_id == principal.instance_id,
                    RunnerCommandRecord.target_runner_epoch == principal.epoch,
                    RunnerCommandRecord.status == RunnerCommandStatus.LEASED.value,
                    RunnerCommandRecord.lease_expires_at <= now,
                )
                .values(
                    status=RunnerCommandStatus.PENDING.value,
                    lease_id=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )

            safety_kinds = [
                RunnerCommandKind.CANCEL.value,
                RunnerCommandKind.TARGET_HTTP_CANCEL.value,
                RunnerCommandKind.BROWSER_CLOSE.value,
                RunnerCommandKind.TERMINAL_CLOSE.value,
            ]
            candidate = select(RunnerCommandRecord.id).where(
                RunnerCommandRecord.node_id == node_id,
                RunnerCommandRecord.target_runner_instance_id == principal.instance_id,
                RunnerCommandRecord.target_runner_epoch == principal.epoch,
                RunnerCommandRecord.status == RunnerCommandStatus.PENDING.value,
            )
            if safety_only:
                candidate = candidate.where(RunnerCommandRecord.kind.in_(safety_kinds))
            candidate_id = await session.scalar(
                candidate.order_by(
                    case(
                        (
                            RunnerCommandRecord.kind.in_(safety_kinds),
                            0,
                        ),
                        else_=1,
                    ),
                    RunnerCommandRecord.created_at,
                    RunnerCommandRecord.id,
                ).limit(1)
            )
            if candidate_id is None:
                return None
            claimed = await session.execute(
                update(RunnerCommandRecord)
                .where(
                    RunnerCommandRecord.id == candidate_id,
                    RunnerCommandRecord.target_runner_instance_id == principal.instance_id,
                    RunnerCommandRecord.target_runner_epoch == principal.epoch,
                    RunnerCommandRecord.status == RunnerCommandStatus.PENDING.value,
                )
                .values(
                    status=RunnerCommandStatus.LEASED.value,
                    lease_id=lease_id,
                    lease_expires_at=leased_until,
                    attempts=RunnerCommandRecord.attempts + 1,
                    updated_at=now,
                )
            )
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                return None
            record = await session.get(RunnerCommandRecord, candidate_id)
            if record is None:
                raise EntityNotFoundError("RunnerCommand", candidate_id)
            command = runner_command_from_record(record)
        return command

    async def renew_lease(
        self,
        command_id: str,
        *,
        principal: RunnerPrincipal,
        lease_id: str,
        leased_until: datetime,
        now: datetime,
    ) -> RunnerCommand:
        if leased_until <= now:
            raise ValueError("leased_until must be later than now")
        async with self._session_factory() as session, session.begin():
            renewed = await session.execute(
                update(RunnerCommandRecord)
                .where(
                    RunnerCommandRecord.id == command_id,
                    RunnerCommandRecord.target_runner_instance_id == principal.instance_id,
                    RunnerCommandRecord.target_runner_epoch == principal.epoch,
                    RunnerCommandRecord.status == RunnerCommandStatus.LEASED.value,
                    RunnerCommandRecord.lease_id == lease_id,
                    RunnerCommandRecord.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=leased_until,
                    updated_at=now,
                )
            )
            if renewed.rowcount != 1:  # type: ignore[attr-defined]
                raise RepositoryConflictError("runner command lease does not match or expired")
            record = await session.get(RunnerCommandRecord, command_id)
            if record is None:
                raise EntityNotFoundError("RunnerCommand", command_id)
            command = runner_command_from_record(record)
        return command

    async def finish(
        self,
        command_id: str,
        *,
        principal: RunnerPrincipal,
        lease_id: str,
        status: RunnerCommandStatus,
        result: dict[str, object],
        error: str,
        completed_at: datetime,
    ) -> RunnerCommand:
        if status not in {RunnerCommandStatus.COMPLETED, RunnerCommandStatus.FAILED}:
            raise ValueError("runner command final status must be completed or failed")
        async with self._session_factory() as session, session.begin():
            record = await session.get(RunnerCommandRecord, command_id, with_for_update=True)
            if record is None:
                raise EntityNotFoundError("RunnerCommand", command_id)
            if (
                record.target_runner_instance_id != principal.instance_id
                or record.target_runner_epoch != principal.epoch
            ):
                raise RepositoryConflictError("runner command owner does not match")
            if record.status in {
                RunnerCommandStatus.COMPLETED.value,
                RunnerCommandStatus.FAILED.value,
            }:
                if record.lease_id == lease_id:
                    return runner_command_from_record(record)
                raise RepositoryConflictError("runner command was already completed")
            if record.status != RunnerCommandStatus.LEASED.value or record.lease_id != lease_id:
                raise RepositoryConflictError("runner command lease does not match")
            record.status = status.value
            record.result_json = result
            record.error = error
            record.updated_at = completed_at
            record.completed_at = completed_at
            await session.flush()
            command = runner_command_from_record(record)
        return command


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

    async def list_for_reconciliation(
        self,
        *,
        status: RunStatus,
        created_through: datetime,
        after_created_at: datetime | None = None,
        after_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[Run]:
        """List a bounded safety-fence snapshot with a stable keyset cursor."""

        if status not in {
            RunStatus.PAUSING,
            RunStatus.CANCELLING,
            RunStatus.COMPLETING,
        }:
            raise ValueError("reconciliation status must be a safety fence")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if (after_created_at is None) != (after_id is None):
            raise ValueError("reconciliation cursor timestamp and ID must be provided together")

        statement = (
            select(RunRecord)
            .where(
                RunRecord.status == status.value,
                RunRecord.created_at <= created_through,
            )
            .order_by(RunRecord.created_at.asc(), RunRecord.id.asc())
            .limit(limit)
        )
        if after_created_at is not None and after_id is not None:
            statement = statement.where(
                or_(
                    RunRecord.created_at > after_created_at,
                    and_(
                        RunRecord.created_at == after_created_at,
                        RunRecord.id > after_id,
                    ),
                )
            )

        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [run_from_record(record) for record in records]

    async def has_nonterminal_model_profile(self, profile_name: str) -> bool:
        terminal_statuses = {
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        }
        statement = (
            select(RunRecord.id)
            .where(
                RunRecord.model_profile == profile_name,
                ~RunRecord.status.in_(terminal_statuses),
            )
            .limit(1)
        )
        async with self._session_factory() as session:
            return await session.scalar(statement) is not None

    async def update_status(self, run_id: str, target: RunStatus) -> Run:
        last_conflict: IntegrityError | OperationalError | None = None
        for attempt in range(10):
            try:
                async with _serialized_run_write(self._session_factory) as session:
                    statement = select(RunRecord).where(RunRecord.id == run_id).with_for_update()
                    record = await session.scalar(statement)
                    if record is None:
                        raise EntityNotFoundError("Run", run_id)

                    run = run_from_record(record)
                    if target is RunStatus.RUNNING or target in FINALIZATION_TARGETS:
                        finalization_intent = await self._read_finalization_intent(session, run_id)
                        if target is RunStatus.RUNNING and finalization_intent is not None:
                            raise RepositoryConflictError(
                                f"run {run_id!r} cannot enter running while finalizing as "
                                f"{finalization_intent.target.value!r}"
                            )
                        if target in FINALIZATION_TARGETS:
                            self._validate_finalization_target(
                                run_id,
                                target,
                                finalization_intent,
                            )
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
            except (IntegrityError, OperationalError) as exc:
                last_conflict = exc
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not update status for run {run_id!r} after concurrent retries"
        ) from last_conflict

    async def complete_if_no_pending_user_messages(
        self,
        run_id: str,
        *,
        consumed_user_message_ids: Sequence[str],
    ) -> tuple[Run, Sequence[str]]:
        """Compatibility alias for the non-terminal completion fence.

        Older callers used this name when the message race and the terminal
        transition were one repository operation. Terminal completion now
        requires affirmative stop evidence from every effect controller, so a
        repository helper must never bypass the shared safety gate.
        """

        return await self.fence_completion_if_no_pending_user_messages(
            run_id,
            consumed_user_message_ids=consumed_user_message_ids,
        )

    async def fence_completion_if_no_pending_user_messages(
        self,
        run_id: str,
        *,
        consumed_user_message_ids: Sequence[str],
        defer_cleanup_event: bool = False,
    ) -> tuple[Run, Sequence[str]]:
        """Atomically close effect/message admission without claiming completion.

        The durable COMPLETING fence is intentionally separate from the final
        COMPLETED transition. Temporal cleanup must first obtain affirmative
        stop evidence for Executions, Browsers, and Target HTTP work.
        """

        consumed = {item for item in consumed_user_message_ids if item}
        last_conflict: IntegrityError | OperationalError | None = None
        for attempt in range(10):
            try:
                async with _serialized_run_write(self._session_factory) as session:
                    record = await session.scalar(
                        select(RunRecord).where(RunRecord.id == run_id).with_for_update()
                    )
                    if record is None:
                        raise EntityNotFoundError("Run", run_id)

                    run = run_from_record(record)
                    if run.status is RunStatus.COMPLETED:
                        return run, ()
                    if run.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
                        raise RepositoryConflictError(
                            f"run {run_id!r} cannot complete from {run.status.value!r}"
                        )

                    queued_statement = (
                        select(RunEventRecord.id)
                        .where(
                            RunEventRecord.run_id == run_id,
                            RunEventRecord.event_type == "user.message_queued",
                        )
                        .order_by(RunEventRecord.sequence)
                    )
                    queued = (await session.scalars(queued_statement)).all()
                    pending = tuple(
                        message_id for message_id in queued if message_id not in consumed
                    )
                    if pending:
                        return run, pending

                    run = await self._apply_finalization_fence(
                        session,
                        record,
                        run,
                        RunStatus.COMPLETED,
                        defer_cleanup_event=defer_cleanup_event,
                    )
                    await session.flush()
                    return run, ()
            except (IntegrityError, OperationalError) as exc:
                last_conflict = exc
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not fence completion for run {run_id!r} after concurrent retries"
        ) from last_conflict

    async def fence_finalization(
        self,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool = False,
    ) -> Run:
        if target not in {RunStatus.COMPLETED, RunStatus.FAILED}:
            raise ValueError("finalization target must be completed or failed")
        last_conflict: IntegrityError | OperationalError | None = None
        for attempt in range(10):
            try:
                async with _serialized_run_write(self._session_factory) as session:
                    record = await session.scalar(
                        select(RunRecord).where(RunRecord.id == run_id).with_for_update()
                    )
                    if record is None:
                        raise EntityNotFoundError("Run", run_id)
                    run = await self._apply_finalization_fence(
                        session,
                        record,
                        run_from_record(record),
                        target,
                        defer_cleanup_event=defer_cleanup_event,
                    )
                    await session.flush()
                    return run
            except RepositoryConflictError:
                raise
            except (IntegrityError, OperationalError) as exc:
                last_conflict = exc
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not fence finalization for run {run_id!r} after concurrent retries"
        ) from last_conflict

    async def commit_finalization(
        self,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool = False,
    ) -> Run:
        """Commit a fenced terminal state and its canonical cleanup fact atomically."""

        if target not in FINALIZATION_TARGETS:
            raise ValueError("finalization target must be completed or failed")
        last_conflict: IntegrityError | OperationalError | None = None
        for attempt in range(10):
            try:
                async with _serialized_run_write(self._session_factory) as session:
                    record = await session.scalar(
                        select(RunRecord).where(RunRecord.id == run_id).with_for_update()
                    )
                    if record is None:
                        raise EntityNotFoundError("Run", run_id)

                    run = run_from_record(record)
                    intent = await self._read_finalization_intent(session, run_id)
                    self._validate_finalization_target(run_id, target, intent)
                    if run.status is not target:
                        if run.status is not RunStatus.COMPLETING or intent is None:
                            raise RepositoryConflictError(
                                f"run {run_id!r} cannot commit {target.value!r} finalization "
                                f"from {run.status.value!r}"
                            )
                        previous = run.status
                        changed_at = utc_now()
                        run.transition_to(target, at=changed_at)
                        apply_run_to_record(run, record)
                        session.add(
                            event_to_record(
                                RunEvent(
                                    run_id=run_id,
                                    sequence=await _next_event_sequence(session, run_id),
                                    event_type="run.status_changed",
                                    payload={"from": previous.value, "to": target.value},
                                    created_at=changed_at,
                                )
                            )
                        )
                        # The canonical cleanup event must receive the following
                        # sequence and live in the same transaction as the state.
                        await session.flush()

                    if not defer_cleanup_event:
                        await self._write_cleanup_event_if_needed(session, run_id, target)
                    await session.flush()
                    return run
            except RepositoryConflictError:
                raise
            except (IntegrityError, OperationalError) as exc:
                last_conflict = exc
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not commit finalization for run {run_id!r} after concurrent retries"
        ) from last_conflict

    async def record_finalization_intent(
        self,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool = False,
    ) -> Run:
        if target not in {RunStatus.COMPLETED, RunStatus.FAILED}:
            raise ValueError("finalization target must be completed or failed")
        last_conflict: IntegrityError | OperationalError | None = None
        for attempt in range(10):
            try:
                async with _serialized_run_write(self._session_factory) as session:
                    record = await session.scalar(
                        select(RunRecord).where(RunRecord.id == run_id).with_for_update()
                    )
                    if record is None:
                        raise EntityNotFoundError("Run", run_id)
                    run = run_from_record(record)
                    if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                        if run.status is target:
                            return run
                        raise RepositoryConflictError(
                            f"run {run.id!r} cannot record a {target.value!r} intent "
                            f"from {run.status.value!r}"
                        )
                    if run.status is RunStatus.CANCELLING:
                        raise RepositoryConflictError(
                            f"run {run.id!r} cancellation superseded its "
                            f"{target.value!r} finalization intent"
                        )
                    if (
                        run.status in {RunStatus.PAUSING, RunStatus.PAUSED}
                        and target is not RunStatus.FAILED
                    ):
                        raise RepositoryConflictError(
                            f"run {run.id!r} pause superseded its "
                            f"{target.value!r} finalization intent"
                        )
                    if run.status not in {RunStatus.PAUSING, RunStatus.PAUSED}:
                        run = await self._apply_finalization_fence(
                            session,
                            record,
                            run,
                            target,
                            defer_cleanup_event=defer_cleanup_event,
                        )
                        await session.flush()
                        return run
                    existing_intent = await self._read_finalization_intent(session, run_id)
                    self._validate_finalization_target(run_id, target, existing_intent)
                    await self._write_finalization_intent_if_needed(
                        session,
                        run_id,
                        target,
                        defer_cleanup_event=defer_cleanup_event,
                        existing_intent=existing_intent,
                        created_at=utc_now(),
                    )
                    await session.flush()
                    return run
            except RepositoryConflictError:
                raise
            except (IntegrityError, OperationalError) as exc:
                last_conflict = exc
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not record finalization intent for run {run_id!r} after concurrent retries"
        ) from last_conflict

    async def get_finalization_intent(self, run_id: str) -> RunFinalizationIntent | None:
        async with self._session_factory() as session:
            run_exists = await session.scalar(select(RunRecord.id).where(RunRecord.id == run_id))
            if run_exists is None:
                raise EntityNotFoundError("Run", run_id)
            return await self._read_finalization_intent(session, run_id)

    async def _apply_finalization_fence(
        self,
        session: AsyncSession,
        record: RunRecord,
        run: Run,
        target: RunStatus,
        *,
        defer_cleanup_event: bool,
    ) -> Run:
        if run.status is target:
            return run
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise RepositoryConflictError(
                f"run {run.id!r} cannot finalize as {target.value!r} from {run.status.value!r}"
            )

        existing_intent = await self._read_finalization_intent(session, run.id)
        self._validate_finalization_target(run.id, target, existing_intent)

        changed_at = utc_now()
        if run.status is not RunStatus.COMPLETING:
            previous = run.status
            run.transition_to(RunStatus.COMPLETING, at=changed_at)
            apply_run_to_record(run, record)
            session.add(
                event_to_record(
                    RunEvent(
                        run_id=run.id,
                        sequence=await _next_event_sequence(session, run.id),
                        event_type="run.status_changed",
                        payload={
                            "from": previous.value,
                            "to": RunStatus.COMPLETING.value,
                        },
                        created_at=changed_at,
                    )
                )
            )
            # Make the status event's sequence visible before allocating the
            # immediately following intent event in the same transaction.
            await session.flush()

        await self._write_finalization_intent_if_needed(
            session,
            run.id,
            target,
            defer_cleanup_event=defer_cleanup_event,
            existing_intent=existing_intent,
            created_at=changed_at,
        )
        return run

    async def _read_finalization_intent(
        self,
        session: AsyncSession,
        run_id: str,
    ) -> RunFinalizationIntent | None:
        intent_records = (
            await session.scalars(
                select(RunEventRecord)
                .where(
                    RunEventRecord.run_id == run_id,
                    RunEventRecord.event_type == FINALIZATION_INTENT_EVENT_TYPE,
                )
                .order_by(RunEventRecord.sequence)
            )
        ).all()
        try:
            return resolve_finalization_intent([event_from_record(item) for item in intent_records])
        except ValueError as exc:
            raise RepositoryConflictError(
                f"run {run_id!r} has an invalid finalization intent: {exc}"
            ) from exc

    @staticmethod
    def _validate_finalization_target(
        run_id: str,
        target: RunStatus,
        existing_intent: RunFinalizationIntent | None,
    ) -> None:
        if existing_intent is not None and existing_intent.target is not target:
            raise RepositoryConflictError(
                f"run {run_id!r} is already finalizing as {existing_intent.target.value!r}"
            )

    async def _write_finalization_intent_if_needed(
        self,
        session: AsyncSession,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool,
        existing_intent: RunFinalizationIntent | None,
        created_at: datetime,
    ) -> None:
        requested_intent = RunFinalizationIntent(
            target=target,
            defer_cleanup_event=defer_cleanup_event,
        )
        if existing_intent is not None and (
            existing_intent.defer_cleanup_event or not requested_intent.defer_cleanup_event
        ):
            return
        session.add(
            event_to_record(
                RunEvent(
                    run_id=run_id,
                    sequence=await _next_event_sequence(session, run_id),
                    event_type=FINALIZATION_INTENT_EVENT_TYPE,
                    payload=requested_intent.payload(),
                    created_at=created_at,
                )
            )
        )

    async def _write_cleanup_event_if_needed(
        self,
        session: AsyncSession,
        run_id: str,
        target: RunStatus,
    ) -> None:
        event_id = cleanup_event_id(run_id, target)
        payload = cleanup_event_payload(target)
        existing_record = await session.get(RunEventRecord, event_id)
        if existing_record is not None:
            existing = event_from_record(existing_record)
            if (
                existing.run_id != run_id
                or existing.event_type != "run.cleaned_up"
                or existing.payload != payload
            ):
                raise RepositoryConflictError(
                    f"event ID {event_id!r} is already assigned to a different event"
                )
            return
        session.add(
            event_to_record(
                RunEvent(
                    id=event_id,
                    run_id=run_id,
                    sequence=await _next_event_sequence(session, run_id),
                    event_type="run.cleaned_up",
                    payload=payload,
                )
            )
        )

    async def update_model_profile(self, run_id: str, model_profile: str) -> Run:
        normalized = model_profile.strip()
        if not normalized:
            raise ValueError("model_profile must not be empty")
        async with self._session_factory() as session, session.begin():
            record = await session.get(RunRecord, run_id)
            if record is None:
                raise EntityNotFoundError("Run", run_id)
            record.model_profile = normalized
            await session.flush()
            return run_from_record(record)


class SQLAlchemyRunEventRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str | None = None,
    ) -> RunEvent:
        resolved_payload = payload or {}
        last_conflict: IntegrityError | None = None
        for attempt in range(10):
            try:
                async with self._session_factory() as session, session.begin():
                    run_exists = await session.scalar(
                        select(RunRecord.id).where(RunRecord.id == run_id).with_for_update()
                    )
                    if run_exists is None:
                        raise EntityNotFoundError("Run", run_id)

                    event_sequence = await _next_event_sequence(session, run_id)
                    if event_id is None:
                        event = RunEvent(
                            run_id=run_id,
                            sequence=event_sequence,
                            event_type=event_type,
                            payload=resolved_payload,
                        )
                    else:
                        event = RunEvent(
                            id=event_id,
                            run_id=run_id,
                            sequence=event_sequence,
                            event_type=event_type,
                            payload=resolved_payload,
                        )
                    session.add(event_to_record(event))
                    await session.flush()
                    return event
            except IntegrityError as exc:
                last_conflict = exc
                if event_id is not None:
                    # A concurrent idempotent request may have committed the
                    # caller-selected event ID first. Never treat a primary-key
                    # collision with another Run or event type as success: a
                    # user-controlled message UUID must not suppress a system
                    # cleanup event with a deterministic ID.
                    existing = await self.get(event_id)
                    if existing is not None:
                        return self._validate_idempotent_event(
                            existing,
                            run_id=run_id,
                            event_type=event_type,
                            payload=resolved_payload,
                        )
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not append event for run {run_id!r} after concurrent retries"
        ) from last_conflict

    async def get(self, event_id: str) -> RunEvent | None:
        async with self._session_factory() as session:
            record = await session.get(RunEventRecord, event_id)
        return event_from_record(record) if record is not None else None

    async def append_terminal_projection_if_current(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str,
        session_id: str,
        expected_terminal_status: TerminalStatus,
        expected_execution_status: ExecutionStatus,
    ) -> RunEvent | None:
        """Serialize a Terminal event against its durable projection state.

        The Run row is locked first because event sequencing is Run-scoped,
        followed by Terminal and Execution.  Terminal CAS writes never hold an
        Execution or Run lock, so this order cannot form a lock cycle with
        ``SQLAlchemyTerminalRepository.save_if_status``.  SQLite uses
        ``BEGIN IMMEDIATE`` through ``_serialized_run_write``; server databases
        use the row locks below.  In both cases, either the lower-state event
        commits before a higher Terminal transition, or it observes that
        transition/status update and is explicitly skipped.
        """

        base_payload = dict(payload or {})
        last_conflict: IntegrityError | OperationalError | None = None
        for attempt in range(10):
            resolved_payload: dict[str, object] | None = None
            try:
                async with _serialized_run_write(self._session_factory) as session:
                    run_record = await session.scalar(
                        select(RunRecord).where(RunRecord.id == run_id).with_for_update()
                    )
                    if run_record is None:
                        raise EntityNotFoundError("Run", run_id)

                    terminal_record = await session.scalar(
                        select(TerminalSessionRecord)
                        .where(
                            TerminalSessionRecord.id == session_id,
                            TerminalSessionRecord.run_id == run_id,
                        )
                        .with_for_update()
                    )
                    if terminal_record is None:
                        raise EntityNotFoundError("TerminalSession", session_id)

                    execution_record = await session.scalar(
                        select(ExecutionRecord)
                        .where(
                            ExecutionRecord.id == terminal_record.execution_id,
                            ExecutionRecord.run_id == run_id,
                        )
                        .with_for_update()
                    )
                    if execution_record is None:
                        raise EntityNotFoundError("Execution", terminal_record.execution_id)

                    durable_terminal_status = TerminalStatus(terminal_record.status)
                    durable_execution_status = ExecutionStatus(execution_record.status)
                    if (
                        durable_terminal_status is not expected_terminal_status
                        or durable_execution_status is not expected_execution_status
                        or (
                            expected_terminal_status is TerminalStatus.CLOSED
                            and execution_record.physical_stop_confirmed_at is None
                        )
                    ):
                        return None

                    # Projection identity comes from locked durable rows, not
                    # from a potentially stale caller.  This keeps the payload
                    # stable for deterministic event-ID retries.
                    resolved_payload = {
                        **base_payload,
                        "session_id": terminal_record.id,
                        "execution_id": execution_record.id,
                        "status": durable_execution_status.value,
                    }
                    existing_record = await session.get(RunEventRecord, event_id)
                    if existing_record is not None:
                        return self._validate_idempotent_event(
                            event_from_record(existing_record),
                            run_id=run_id,
                            event_type=event_type,
                            payload=resolved_payload,
                        )

                    event = RunEvent(
                        id=event_id,
                        run_id=run_id,
                        sequence=await _next_event_sequence(session, run_id),
                        event_type=event_type,
                        payload=resolved_payload,
                    )
                    session.add(event_to_record(event))
                    await session.flush()
                    return event
            except RepositoryConflictError:
                raise
            except (IntegrityError, OperationalError) as exc:
                last_conflict = exc
                if resolved_payload is not None:
                    existing = await self.get(event_id)
                    if existing is not None:
                        return self._validate_idempotent_event(
                            existing,
                            run_id=run_id,
                            event_type=event_type,
                            payload=resolved_payload,
                        )
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not append terminal projection event for {session_id!r} "
            "after concurrent retries"
        ) from last_conflict

    async def append_user_message(
        self,
        run_id: str,
        message: str,
        *,
        event_id: str | None = None,
    ) -> RunEvent:
        """Queue a message only while the Run can still consume it.

        This is deliberately separate from the unrestricted audit ``append``:
        lifecycle and cleanup events must remain writable after a Run closes,
        but a user instruction must never be durably accepted after the
        pause, cancellation, or completion admission fence has won.
        """

        closed_statuses = {
            RunStatus.PAUSING,
            RunStatus.COMPLETING,
            RunStatus.CANCELLING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
        resolved_payload: dict[str, object] = {"message": message}
        reserved_system_event_ids = {
            cleanup_event_id(run_id, target)
            for target in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)
        }
        reserved_system_event_ids.add(report_failure_event_id(run_id))
        if event_id is not None and event_id in reserved_system_event_ids:
            raise RepositoryConflictError(
                f"event ID {event_id!r} is reserved for Run lifecycle events"
            )
        last_conflict: IntegrityError | OperationalError | None = None
        for attempt in range(10):
            try:
                async with _serialized_run_write(self._session_factory) as session:
                    run_record = await session.scalar(
                        select(RunRecord).where(RunRecord.id == run_id).with_for_update()
                    )
                    if run_record is None:
                        raise EntityNotFoundError("Run", run_id)
                    run_status = RunStatus(run_record.status)
                    if run_status in closed_statuses:
                        raise RepositoryConflictError(
                            f"run {run_id!r} does not accept user messages while "
                            f"it is {run_status.value!r}"
                        )

                    if event_id is not None:
                        existing_record = await session.get(RunEventRecord, event_id)
                        if existing_record is not None:
                            return self._validate_idempotent_event(
                                event_from_record(existing_record),
                                run_id=run_id,
                                event_type="user.message_queued",
                                payload=resolved_payload,
                            )

                    event = RunEvent(
                        id=event_id or str(uuid4()),
                        run_id=run_id,
                        sequence=await _next_event_sequence(session, run_id),
                        event_type="user.message_queued",
                        payload=resolved_payload,
                    )
                    session.add(event_to_record(event))
                    await session.flush()
                    return event
            except RepositoryConflictError:
                raise
            except (IntegrityError, OperationalError) as exc:
                last_conflict = exc
                if event_id is not None:
                    existing = await self.get(event_id)
                    if existing is not None:
                        return self._validate_idempotent_event(
                            existing,
                            run_id=run_id,
                            event_type="user.message_queued",
                            payload=resolved_payload,
                        )
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not append a user message for run {run_id!r} after concurrent retries"
        ) from last_conflict

    @staticmethod
    def _validate_idempotent_event(
        existing: RunEvent,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> RunEvent:
        if (
            existing.run_id != run_id
            or existing.event_type != event_type
            or existing.payload != payload
        ):
            raise RepositoryConflictError(
                f"event ID {existing.id!r} is already assigned to a different event"
            )
        return existing

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


_FINDING_MUTABLE_FIELDS = (
    "title",
    "severity",
    "status",
    "affected_assets",
    "description",
    "evidence",
    "reproduction_steps",
    "impact",
    "recommendation",
)


def _finding_payload(finding: Finding) -> tuple[object, ...]:
    return tuple(getattr(finding, field_name) for field_name in _FINDING_MUTABLE_FIELDS)


def _validate_finding_identity(current: Finding, incoming: Finding) -> None:
    for field_name in ("id", "run_id", "created_at"):
        if getattr(current, field_name) != getattr(incoming, field_name):
            raise RepositoryConflictError(
                f"Finding {current.id!r} field {field_name!r} is immutable after creation"
            )


class SQLAlchemyFindingRepository:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def create(self, finding: Finding) -> Finding:
        try:
            async with _serialized_run_write(self._session_factory) as session:
                run_exists = await session.scalar(
                    select(RunRecord.id).where(RunRecord.id == finding.run_id).with_for_update()
                )
                existing = await session.scalar(
                    select(FindingRecord.id).where(FindingRecord.id == finding.id).with_for_update()
                )
                if run_exists is None or existing is not None:
                    raise RepositoryConflictError(f"could not create finding {finding.id!r}")
                created_at = next_mutation_at(self._clock)
                authoritative = finding.model_copy(
                    update={"created_at": created_at, "updated_at": created_at}
                )
                session.add(
                    finding_to_record(
                        authoritative,
                        created_at=created_at,
                        updated_at=created_at,
                    )
                )
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create finding {finding.id!r}") from exc
        return authoritative

    async def get(self, finding_id: str) -> Finding | None:
        async with self._session_factory() as session:
            record = await session.get(FindingRecord, finding_id)
        return finding_from_record(record) if record is not None else None

    async def save(
        self,
        finding: Finding,
        *,
        expected_updated_at: datetime,
    ) -> tuple[Finding, bool]:
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(FindingRecord).where(FindingRecord.id == finding.id).with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("Finding", finding.id)
            current = finding_from_record(record)
            _validate_finding_identity(current, finding)
            payload_matches = _finding_payload(current) == _finding_payload(finding)
            if expected_updated_at != current.updated_at:
                if payload_matches:
                    return current, False
                raise RepositoryConflictError(
                    f"Finding {finding.id!r} was updated by another writer"
                )
            if payload_matches:
                return current, False
            apply_finding_to_record(finding, record)
            record.updated_at = next_mutation_at(self._clock, stored=record.updated_at)
            await session.flush()
            persisted = finding_from_record(record)
        return persisted, True

    async def list(
        self,
        run_id: str,
        *,
        severity: FindingSeverity | None = None,
        status: FindingStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Finding]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")

        statement = select(FindingRecord).where(FindingRecord.run_id == run_id)
        if severity is not None:
            statement = statement.where(FindingRecord.severity == severity.value)
        if status is not None:
            statement = statement.where(FindingRecord.status == status.value)
        statement = (
            statement.order_by(FindingRecord.created_at, FindingRecord.id)
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [finding_from_record(record) for record in records]


class SQLAlchemyReportRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, report: Report) -> Report:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(report_to_record(report))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create report {report.id!r}") from exc
        return report

    async def get(self, report_id: str) -> Report | None:
        async with self._session_factory() as session:
            record = await session.get(ReportRecord, report_id)
        return report_from_record(record) if record is not None else None

    async def list(
        self,
        run_id: str,
        *,
        format: ReportFormat | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Report]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")
        statement = select(ReportRecord).where(ReportRecord.run_id == run_id)
        if format is not None:
            statement = statement.where(ReportRecord.format == format.value)
        statement = (
            statement.order_by(ReportRecord.created_at, ReportRecord.id).limit(limit).offset(offset)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [report_from_record(record) for record in records]


class SQLAlchemyApprovalRepository:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        decision_failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._decision_failpoint = decision_failpoint

    async def create_request(
        self,
        tool_call: ToolCall,
        approval: Approval,
    ) -> tuple[Approval, bool]:
        existing = await self._get_by_sdk_call_id(tool_call.run_id, tool_call.sdk_call_id)
        if existing is not None:
            return existing, False
        try:
            async with self._session_factory() as session, session.begin():
                session.add(tool_call_to_record(tool_call))
                await session.flush()
                session.add(approval_to_record(approval))
                await session.flush()
            return approval, True
        except IntegrityError as exc:
            existing = await self._get_by_sdk_call_id(tool_call.run_id, tool_call.sdk_call_id)
            if existing is not None:
                return existing, False
            raise RepositoryConflictError(
                f"could not create approval request {approval.id!r}"
            ) from exc

    async def get(self, approval_id: str) -> Approval | None:
        async with self._session_factory() as session:
            record = await session.get(ApprovalRecord, approval_id)
        return approval_from_record(record) if record is not None else None

    async def get_tool_call(self, tool_call_id: str) -> ToolCall | None:
        async with self._session_factory() as session:
            record = await session.get(ToolCallRecord, tool_call_id)
        return tool_call_from_record(record) if record is not None else None

    async def list(
        self,
        run_id: str,
        *,
        status: ApprovalStatus | None = None,
    ) -> Sequence[Approval]:
        statement = select(ApprovalRecord).where(ApprovalRecord.run_id == run_id)
        if status is not None:
            statement = statement.where(ApprovalRecord.status == status.value)
        statement = statement.order_by(ApprovalRecord.created_at, ApprovalRecord.id)
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [approval_from_record(record) for record in records]

    async def decide(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        decided_by: str,
        reason: str | None = None,
        blocked_run_statuses: Collection[RunStatus] = (),
    ) -> tuple[Approval, bool]:
        blocked = frozenset(blocked_run_statuses)
        last_conflict: IntegrityError | OperationalError | None = None
        for attempt in range(10):
            try:
                async with _serialized_run_write(self._session_factory) as session:
                    record = await session.scalar(
                        select(ApprovalRecord)
                        .where(ApprovalRecord.id == approval_id)
                        .with_for_update()
                    )
                    if record is None:
                        raise EntityNotFoundError("Approval", approval_id)
                    approval = approval_from_record(record)
                    run_record = await session.scalar(
                        select(RunRecord).where(RunRecord.id == approval.run_id).with_for_update()
                    )
                    if run_record is None:
                        raise EntityNotFoundError("Run", approval.run_id)
                    run_status = RunStatus(run_record.status)
                    if run_status in blocked:
                        raise RepositoryConflictError(
                            f"approval {approval.id!r} is not actionable while run "
                            f"{approval.run_id!r} is {run_status.value!r}"
                        )
                    if approval.status is status:
                        return approval, False
                    approval.decide(status, decided_by=decided_by, reason=reason)
                    apply_approval_to_record(approval, record)
                    tool_call = await session.get(ToolCallRecord, approval.tool_call_id)
                    if tool_call is not None:
                        tool_call.approval_status = status.value
                    await session.flush()
                    return approval, True
            except RepositoryConflictError:
                raise
            except (IntegrityError, OperationalError) as exc:
                last_conflict = exc
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not decide approval {approval_id!r} after concurrent retries"
        ) from last_conflict

    async def decide_runtime(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        decided_by: str,
        feedback: str | None = None,
        blocked_run_statuses: Collection[RunStatus] = (),
    ) -> tuple[Approval, bool]:
        """Commit every durable effect of one Runtime Approval decision together."""

        if decision is ApprovalDecision.REJECT_WITH_FEEDBACK and not feedback:
            raise ValueError("reject_with_feedback requires feedback")
        expected_status = _approval_status_for_decision(decision)
        blocked = frozenset(blocked_run_statuses)
        last_conflict: IntegrityError | OperationalError | None = None
        for attempt in range(10):
            try:
                async with _serialized_run_write(self._session_factory) as session:
                    # Approval.run_id is immutable. This first lookup only discovers
                    # which Run row must be locked before the mutable aggregate rows.
                    run_id = await session.scalar(
                        select(ApprovalRecord.run_id).where(ApprovalRecord.id == approval_id)
                    )
                    if run_id is None:
                        raise EntityNotFoundError("Approval", approval_id)
                    run_record = await session.scalar(
                        select(RunRecord).where(RunRecord.id == run_id).with_for_update()
                    )
                    if run_record is None:
                        raise EntityNotFoundError("Run", run_id)
                    run_status = RunStatus(run_record.status)
                    if run_status in blocked:
                        raise RepositoryConflictError(
                            f"approval {approval_id!r} is not actionable while run "
                            f"{run_id!r} is {run_status.value!r}"
                        )

                    approval_record = await session.scalar(
                        select(ApprovalRecord)
                        .where(ApprovalRecord.id == approval_id)
                        .with_for_update()
                    )
                    if approval_record is None:
                        raise EntityNotFoundError("Approval", approval_id)
                    tool_call_record = await session.scalar(
                        select(ToolCallRecord)
                        .where(ToolCallRecord.id == approval_record.tool_call_id)
                        .with_for_update()
                    )
                    if tool_call_record is None:
                        raise EntityNotFoundError("ToolCall", approval_record.tool_call_id)
                    runtime_record = await session.scalar(
                        select(RuntimeApprovalRequestRecord)
                        .where(RuntimeApprovalRequestRecord.id == approval_id)
                        .with_for_update()
                    )
                    if runtime_record is not None and runtime_record.run_id != run_id:
                        raise RepositoryConflictError(
                            f"runtime approval {approval_id!r} belongs to a different Run"
                        )
                    grant_record = await session.scalar(
                        select(ApprovalGrantRecord)
                        .where(
                            ApprovalGrantRecord.run_id == run_id,
                            ApprovalGrantRecord.tool_id == tool_call_record.tool_id,
                        )
                        .with_for_update()
                    )

                    changed = False
                    runtime_was_terminal = (
                        runtime_record is not None
                        and runtime_record.status != ApprovalStatus.PENDING.value
                    )
                    if runtime_was_terminal:
                        _require_runtime_record_match(
                            runtime_record,
                            approval_id=approval_id,
                            status=expected_status,
                            decision=decision,
                            decided_by=decided_by,
                            feedback=feedback,
                        )

                    if approval_record.status == ApprovalStatus.PENDING.value:
                        decided_at = (
                            runtime_record.decided_at
                            if runtime_was_terminal and runtime_record is not None
                            else utc_now()
                        )
                        if decided_at is None:
                            _raise_decision_conflict(
                                approval_id,
                                "The terminal Runtime Approval has no decision timestamp",
                                runtime_record=runtime_record,
                                requested_decision=decision,
                            )
                        approval = approval_from_record(approval_record)
                        approval.decide(
                            expected_status,
                            decided_by=decided_by,
                            reason=feedback,
                            decision=decision,
                            at=decided_at,
                        )
                        apply_approval_to_record(approval, approval_record)
                        tool_call_record.approval_status = expected_status.value
                        changed = True
                    else:
                        changed = (
                            _backfill_legacy_public_decision(
                                approval_record,
                                runtime_record=runtime_record,
                            )
                            or changed
                        )
                        approval = approval_from_record(approval_record)
                        _require_public_approval_match(
                            approval,
                            status=expected_status,
                            decision=decision,
                            decided_by=decided_by,
                            feedback=feedback,
                        )
                        decided_at = approval.decided_at
                        if decided_at is None:
                            _raise_decision_conflict(
                                approval_id,
                                "The terminal public Approval has no decision timestamp",
                                runtime_record=runtime_record,
                                requested_decision=decision,
                            )
                        if runtime_was_terminal and runtime_record is not None:
                            if runtime_record.decided_at != decided_at:
                                _raise_decision_conflict(
                                    approval_id,
                                    "The public and Runtime Approval timestamps disagree",
                                    runtime_record=runtime_record,
                                    requested_decision=decision,
                                )
                    await session.flush()
                    self._hit_decision_failpoint("after_public")

                    if runtime_record is not None and not runtime_was_terminal:
                        if (
                            runtime_record.status != ApprovalStatus.PENDING.value
                            or runtime_record.decision is not None
                            or runtime_record.feedback is not None
                            or runtime_record.decided_by is not None
                            or runtime_record.decided_at is not None
                        ):
                            raise RepositoryConflictError(
                                f"runtime approval {approval_id!r} has an invalid partial decision"
                            )
                        runtime_record.status = expected_status.value
                        runtime_record.decision = decision.value
                        runtime_record.feedback = feedback
                        runtime_record.decided_by = decided_by
                        runtime_record.decided_at = decided_at
                        changed = True
                    await session.flush()
                    self._hit_decision_failpoint("after_runtime")

                    if decision is ApprovalDecision.APPROVE_TOOL_FOR_RUN and grant_record is None:
                        grant = ApprovalGrant(
                            run_id=run_id,
                            tool_id=tool_call_record.tool_id,
                            created_by=decided_by,
                            created_at=decided_at,
                        )
                        session.add(approval_grant_to_record(grant))
                        changed = True
                    await session.flush()
                    self._hit_decision_failpoint("after_grant")

                    event_type = (
                        "tool.approved"
                        if expected_status is ApprovalStatus.APPROVED
                        else "tool.rejected"
                    )
                    event_payload: dict[str, object] = {
                        "approval_id": approval.id,
                        "tool_call_id": approval.tool_call_id,
                        "sdk_call_id": tool_call_record.sdk_call_id,
                        "tool_name": approval.tool_name,
                        "decided_by": approval.decided_by,
                        "reason": approval.decision_feedback,
                        "approve_for_run": (decision is ApprovalDecision.APPROVE_TOOL_FOR_RUN),
                    }
                    existing_events = (
                        await session.scalars(
                            select(RunEventRecord)
                            .where(
                                RunEventRecord.run_id == run_id,
                                RunEventRecord.event_type == event_type,
                            )
                            .with_for_update()
                        )
                    ).all()
                    decision_event = next(
                        (
                            record
                            for record in existing_events
                            if record.payload_json.get("approval_id") == approval_id
                        ),
                        None,
                    )
                    if decision_event is None:
                        decision_event = RunEventRecord(
                            id=_approval_decision_event_id(
                                approval_id,
                                expected_status,
                            ),
                            run_id=run_id,
                            sequence=await _next_event_sequence(session, run_id),
                            event_type=event_type,
                            payload_json=event_payload,
                            created_at=decided_at,
                        )
                        session.add(decision_event)
                        changed = True
                    elif decision_event.payload_json != event_payload:
                        decision_event.payload_json = event_payload
                        changed = True
                    await session.flush()
                    self._hit_decision_failpoint("after_event")
                    return approval_from_record(approval_record), changed
            except (RepositoryConflictError, EntityNotFoundError):
                raise
            except (IntegrityError, OperationalError) as exc:
                last_conflict = exc
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not decide runtime approval {approval_id!r} after concurrent retries"
        ) from last_conflict

    def _hit_decision_failpoint(self, stage: str) -> None:
        if self._decision_failpoint is not None:
            self._decision_failpoint(stage)

    async def grant_for_run(
        self,
        run_id: str,
        tool_id: str,
        *,
        created_by: str,
    ) -> ApprovalGrant:
        existing = await self._get_grant(run_id, tool_id)
        if existing is not None:
            return existing
        grant = ApprovalGrant(run_id=run_id, tool_id=tool_id, created_by=created_by)
        try:
            async with self._session_factory() as session, session.begin():
                session.add(approval_grant_to_record(grant))
                await session.flush()
            return grant
        except IntegrityError as exc:
            existing = await self._get_grant(run_id, tool_id)
            if existing is not None:
                return existing
            raise RepositoryConflictError(
                f"could not grant approval for tool {tool_id!r} in run {run_id!r}"
            ) from exc

    async def is_granted(self, run_id: str, tool_id: str) -> bool:
        return await self._get_grant(run_id, tool_id) is not None

    async def _get_by_sdk_call_id(self, run_id: str, sdk_call_id: str) -> Approval | None:
        statement = (
            select(ApprovalRecord)
            .join(ToolCallRecord, ToolCallRecord.id == ApprovalRecord.tool_call_id)
            .where(
                ToolCallRecord.run_id == run_id,
                ToolCallRecord.sdk_call_id == sdk_call_id,
            )
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return approval_from_record(record) if record is not None else None

    async def _get_grant(self, run_id: str, tool_id: str) -> ApprovalGrant | None:
        statement = select(ApprovalGrantRecord).where(
            ApprovalGrantRecord.run_id == run_id,
            ApprovalGrantRecord.tool_id == tool_id,
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return approval_grant_from_record(record) if record is not None else None


class SQLAlchemyTerminalRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, terminal: TerminalSession) -> TerminalSession:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(terminal_to_record(terminal))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not create terminal session {terminal.id!r}"
            ) from exc
        return terminal

    async def get(self, session_id: str) -> TerminalSession | None:
        async with self._session_factory() as session:
            record = await session.get(TerminalSessionRecord, session_id)
        return terminal_from_record(record) if record is not None else None

    async def get_by_execution(self, execution_id: str) -> TerminalSession | None:
        statement = select(TerminalSessionRecord).where(
            TerminalSessionRecord.execution_id == execution_id
        )
        async with self._session_factory() as session:
            record = await session.scalar(statement)
        return terminal_from_record(record) if record is not None else None

    async def save(self, terminal: TerminalSession) -> TerminalSession:
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(TerminalSessionRecord)
                .where(TerminalSessionRecord.id == terminal.id)
                .with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("TerminalSession", terminal.id)
            current = terminal_from_record(record)
            if not _terminal_status_update_is_monotonic(current.status, terminal.status):
                return current
            apply_terminal_to_record(terminal, record)
            await session.flush()
        return terminal

    async def save_if_status(
        self,
        terminal: TerminalSession,
        *,
        expected: Collection[TerminalStatus],
    ) -> tuple[TerminalSession, bool]:
        expected_statuses = set(expected)
        if not expected_statuses:
            raise ValueError("expected terminal statuses cannot be empty")
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(TerminalSessionRecord)
                .where(TerminalSessionRecord.id == terminal.id)
                .with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("TerminalSession", terminal.id)
            current = terminal_from_record(record)
            if current.status not in expected_statuses or not (
                _terminal_status_update_is_monotonic(current.status, terminal.status)
            ):
                return current, False
            apply_terminal_to_record(terminal, record)
            await session.flush()
        return terminal, True

    async def list_open(self) -> Sequence[TerminalSession]:
        statement = (
            select(TerminalSessionRecord)
            .where(TerminalSessionRecord.status == TerminalStatus.OPEN.value)
            .order_by(TerminalSessionRecord.created_at, TerminalSessionRecord.id)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [terminal_from_record(record) for record in records]

    async def list_active(self) -> Sequence[TerminalSession]:
        statement = (
            select(TerminalSessionRecord)
            .where(
                TerminalSessionRecord.status.in_(
                    [TerminalStatus.CREATED.value, TerminalStatus.OPEN.value]
                )
            )
            .order_by(TerminalSessionRecord.created_at, TerminalSessionRecord.id)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [terminal_from_record(record) for record in records]


_TERMINAL_STATUS_TRANSITIONS = {
    TerminalStatus.CREATED: frozenset(
        {TerminalStatus.OPEN, TerminalStatus.CLOSED, TerminalStatus.LOST}
    ),
    TerminalStatus.OPEN: frozenset({TerminalStatus.CLOSED, TerminalStatus.LOST}),
    TerminalStatus.LOST: frozenset({TerminalStatus.CLOSED}),
    TerminalStatus.CLOSED: frozenset(),
}


def _terminal_status_update_is_monotonic(
    current: TerminalStatus,
    incoming: TerminalStatus,
) -> bool:
    return incoming is current or incoming in _TERMINAL_STATUS_TRANSITIONS[current]


_EXECUTION_IMMUTABLE_IDENTITY_FIELDS = (
    "pid",
    "process_group_id",
    "containment_id",
    "process_created_at",
    "executable_path",
    "tool_id",
    "tool_version",
    "platform_system",
    "platform_release",
    "platform_architecture",
)
_EXECUTION_STRICT_ADMISSION_FIELDS = (
    "launch_fingerprint",
    "run_id",
    "session_id",
    "tool_call_id",
    "attempt_group",
    "node_id",
    "executor_type",
    "command_text",
    "cwd",
    "env_diff",
    "stdout_path",
    "stderr_path",
)
_EXECUTION_DUPLICATE_ADMISSION_FIELDS = tuple(
    field_name
    for field_name in _EXECUTION_STRICT_ADMISSION_FIELDS
    if field_name not in {"stdout_path", "stderr_path"}
)
_EXECUTION_FIRST_WRITE_WINS_FIELDS = (
    "started_at",
    "physical_stop_confirmed_at",
)
_EXECUTION_MUTABLE_RECORD_FIELDS = (
    "session_id",
    "tool_call_id",
    "attempt_group",
    "node_id",
    "owner_runner_instance_id",
    "owner_runner_epoch",
    "executor_type",
    "argv_json",
    "command_text",
    "tool_id",
    "tool_version",
    "executable_path",
    "cwd",
    "env_diff_json",
    "platform_system",
    "platform_release",
    "platform_architecture",
    "status",
    "pid",
    "process_group_id",
    "containment_id",
    "exit_code",
    "stdout_path",
    "stderr_path",
    "process_created_at",
    "started_at",
    "finished_at",
    "physical_stop_confirmed_at",
)


def _execution_metadata_state(record: ExecutionRecord) -> tuple[object, ...]:
    return tuple(getattr(record, field_name) for field_name in _EXECUTION_MUTABLE_RECORD_FIELDS)


def _execution_lifecycle_timestamps(
    execution: Execution | ExecutionRecord,
) -> tuple[datetime | None, ...]:
    return (
        execution.created_at,
        execution.process_created_at,
        execution.started_at,
        execution.finished_at,
        execution.physical_stop_confirmed_at,
    )


def _validate_execution_bound_fields(current: Execution, incoming: Execution) -> bool:
    """Return whether ``incoming`` is stale; reject split-brain identity changes."""

    if current.created_at != incoming.created_at:
        raise RepositoryConflictError(
            f"Execution {current.id!r} creation time is immutable after creation"
        )
    if current.execution_key != incoming.execution_key:
        raise RepositoryConflictError(
            f"Execution {current.id!r} key is already bound to "
            f"{current.execution_key!r}, not {incoming.execution_key!r}"
        )
    for field_name in _EXECUTION_STRICT_ADMISSION_FIELDS:
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if proposed != persisted:
            raise RepositoryConflictError(
                f"Execution {current.id!r} admission field {field_name!r} "
                f"is already bound to {persisted!r}, not {proposed!r}"
            )
    if current.owner is not None and incoming.owner != current.owner:
        raise RepositoryConflictError(
            f"Execution {current.id!r} owner is already bound to "
            f"{current.owner.model_dump(mode='json')!r}"
        )
    stale = False
    for field_name in _EXECUTION_IMMUTABLE_IDENTITY_FIELDS:
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if persisted in {None, ""}:
            continue
        if proposed in {None, ""}:
            stale = True
        elif proposed != persisted:
            raise RepositoryConflictError(
                f"Execution {current.id!r} physical identity field {field_name!r} "
                f"is already bound to {persisted!r}, not {proposed!r}"
            )
    if current.argv:
        if not incoming.argv:
            stale = True
        elif incoming.argv != current.argv:
            raise RepositoryConflictError(
                f"Execution {current.id!r} resolved argv is already bound to "
                f"{current.argv!r}, not {incoming.argv!r}"
            )
    for field_name in _EXECUTION_FIRST_WRITE_WINS_FIELDS:
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if persisted is not None and proposed != persisted:
            stale = True
    return stale


def _validate_execution_duplicate(current: Execution, incoming: Execution) -> None:
    """Reject a same-key create carrying different logical launch semantics."""

    for field_name in _EXECUTION_DUPLICATE_ADMISSION_FIELDS:
        if field_name == "launch_fingerprint":
            # Rows admitted before launch fingerprints remain replayable by a
            # fully described current request after all reconstructable fields
            # below match. A fingerprinted row never accepts an opaque replay.
            if current.launch_fingerprint is None and incoming.launch_fingerprint is not None:
                continue
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if proposed != persisted:
            raise RepositoryConflictError(
                f"Execution key {current.execution_key!r} is already bound to "
                f"admission field {field_name!r}={persisted!r}, not {proposed!r}"
            )
    if incoming.owner != current.owner:
        raise RepositoryConflictError(
            f"Execution key {current.execution_key!r} is already bound to owner "
            f"{current.owner.model_dump(mode='json') if current.owner is not None else None!r}"
        )
    shell_resolved_argv_replay = (
        current.executor_type is ExecutorType.SHELL and bool(current.argv) and not incoming.argv
    )
    if incoming.argv != current.argv and not shell_resolved_argv_replay:
        raise RepositoryConflictError(
            f"Execution key {current.execution_key!r} is already bound to resolved argv "
            f"{current.argv!r}, not {incoming.argv!r}"
        )
    for field_name in ("tool_id", "tool_version"):
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if proposed != persisted:
            raise RepositoryConflictError(
                f"Execution key {current.execution_key!r} is already bound to "
                f"{field_name} {persisted!r}, not {proposed!r}"
            )


_EXECUTION_STATUS_TRANSITIONS = {
    ExecutionStatus.QUEUED: frozenset(
        {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.CREATED: frozenset(
        {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.STARTING: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.FAILED: frozenset({ExecutionStatus.CANCELLED}),
    ExecutionStatus.LOST: frozenset({ExecutionStatus.CANCELLED}),
    ExecutionStatus.COMPLETED: frozenset(),
    ExecutionStatus.EXITED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
    ExecutionStatus.HARD_TIMEOUT: frozenset(),
}


def _execution_status_update_is_monotonic(
    current: ExecutionStatus,
    incoming: ExecutionStatus,
) -> bool:
    return incoming is current or incoming in _EXECUTION_STATUS_TRANSITIONS[current]


class SQLAlchemyExecutionRepository:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def create_if_absent(self, execution: Execution) -> tuple[Execution, bool]:
        try:
            async with _serialized_run_write(self._session_factory) as session:
                existing = await session.scalar(
                    select(ExecutionRecord)
                    .where(ExecutionRecord.execution_key == execution.execution_key)
                    .with_for_update()
                )
                if existing is not None:
                    authoritative = execution_from_record(existing)
                    _validate_execution_duplicate(authoritative, execution)
                    return authoritative, False
                updated_at = next_mutation_at(
                    self._clock,
                    lifecycle_timestamps=_execution_lifecycle_timestamps(execution),
                )
                session.add(execution_to_record(execution, updated_at=updated_at))
                await session.flush()
            return execution, True
        except IntegrityError as exc:
            existing = await self.get_by_key(execution.execution_key)
            if existing is not None:
                _validate_execution_duplicate(existing, execution)
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

    async def find_admission(
        self,
        identity: ExecutionAdmissionIdentity,
    ) -> Execution | None:
        predicates = [ExecutionRecord.execution_key == identity.execution_key]
        if identity.execution_id is not None:
            predicates.append(ExecutionRecord.id == identity.execution_id)
        statement = select(ExecutionRecord).where(or_(*predicates))
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        for record in records:
            execution = execution_from_record(record)
            if identity.matches(execution):
                return execution
        return None

    async def save(self, execution: Execution) -> Execution:
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(ExecutionRecord).where(ExecutionRecord.id == execution.id).with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("Execution", execution.id)
            current = execution_from_record(record)
            if _validate_execution_bound_fields(current, execution):
                raise RepositoryConflictError(
                    f"Stale execution update would clear first-write-wins physical "
                    f"identity for {execution.id!r}"
                )
            if not _execution_status_update_is_monotonic(current.status, execution.status):
                return current
            before = _execution_metadata_state(record)
            apply_execution_to_record(execution, record)
            if _execution_metadata_state(record) == before:
                return execution
            record.updated_at = next_mutation_at(
                self._clock,
                stored=record.updated_at,
                lifecycle_timestamps=_execution_lifecycle_timestamps(record),
            )
            await session.flush()
        return execution

    async def save_if_status(
        self,
        execution: Execution,
        *,
        expected: Collection[ExecutionStatus],
    ) -> tuple[Execution, bool]:
        """Save only while the durable execution is in an expected state.

        Runner start and stop paths race by design.  A late STARTING/RUNNING
        write must never revive a CANCELLED (or otherwise terminal) record
        after a safety controller already confirmed the stop.
        """

        expected_statuses = set(expected)
        if not expected_statuses:
            raise ValueError("expected execution statuses cannot be empty")
        async with _serialized_run_write(self._session_factory) as session:
            record = await session.scalar(
                select(ExecutionRecord).where(ExecutionRecord.id == execution.id).with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("Execution", execution.id)
            current = execution_from_record(record)
            stale = _validate_execution_bound_fields(current, execution)
            if (
                current.status not in expected_statuses
                or stale
                or not _execution_status_update_is_monotonic(current.status, execution.status)
            ):
                return current, False
            before = _execution_metadata_state(record)
            apply_execution_to_record(execution, record)
            if _execution_metadata_state(record) == before:
                return execution, True
            record.updated_at = next_mutation_at(
                self._clock,
                stored=record.updated_at,
                lifecycle_timestamps=_execution_lifecycle_timestamps(record),
            )
            await session.flush()
        return execution, True

    async def list(
        self,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Execution]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")
        statement = (
            select(ExecutionRecord)
            .where(ExecutionRecord.run_id == run_id)
            .order_by(ExecutionRecord.started_at, ExecutionRecord.id)
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [execution_from_record(record) for record in records]

    async def list_active(self) -> Sequence[Execution]:
        statement = (
            select(ExecutionRecord)
            .where(
                ExecutionRecord.status.in_(
                    [
                        ExecutionStatus.QUEUED.value,
                        ExecutionStatus.CREATED.value,
                        ExecutionStatus.STARTING.value,
                        ExecutionStatus.RUNNING.value,
                    ]
                )
            )
            .order_by(ExecutionRecord.started_at, ExecutionRecord.id)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [execution_from_record(record) for record in records]


def _approval_status_for_decision(decision: ApprovalDecision) -> ApprovalStatus:
    if decision in {
        ApprovalDecision.APPROVE_ONCE,
        ApprovalDecision.APPROVE_TOOL_FOR_RUN,
    }:
        return ApprovalStatus.APPROVED
    return ApprovalStatus.REJECTED


def _approval_decision_event_id(
    approval_id: str,
    status: ApprovalStatus,
) -> str:
    return str(uuid5(NAMESPACE_URL, f"riftx:approval:{approval_id}:{status.value}"))


def _backfill_legacy_public_decision(
    approval: ApprovalRecord,
    *,
    runtime_record: RuntimeApprovalRequestRecord | None,
) -> bool:
    """Repair an old terminal public row before enforcing the complete tuple."""

    if approval.decision is not None:
        return False
    if (
        runtime_record is not None
        and runtime_record.status != ApprovalStatus.PENDING.value
        and runtime_record.decision is not None
    ):
        approval.decision = runtime_record.decision
        approval.decision_feedback = runtime_record.feedback
        return True
    if approval.status == ApprovalStatus.APPROVED.value:
        # Grants are scoped only by Run/Tool and carry no source Approval ID.
        # A later Approval may have created the surviving grant, so it cannot
        # widen this legacy row's decision during an idempotent retry.
        approval.decision = ApprovalDecision.APPROVE_ONCE.value
        approval.decision_feedback = None
        return True
    if approval.status == ApprovalStatus.REJECTED.value:
        feedback = approval.reason if approval.reason.strip() else None
        approval.decision = (
            ApprovalDecision.REJECT_WITH_FEEDBACK.value
            if feedback is not None
            else ApprovalDecision.REJECT.value
        )
        approval.decision_feedback = feedback
        return True
    return False


def _require_public_approval_match(
    approval: Approval,
    *,
    status: ApprovalStatus,
    decision: ApprovalDecision,
    decided_by: str,
    feedback: str | None,
) -> None:
    if (
        approval.status is status
        and approval.decision is decision
        and approval.decided_by == decided_by
        and approval.decision_feedback == feedback
        and approval.decided_at is not None
    ):
        return
    raise RepositoryDecisionConflictError(
        "The public Approval already has a conflicting durable decision",
        details={
            "approval_id": approval.id,
            "public_status": approval.status.value,
            "public_decision": (approval.decision.value if approval.decision is not None else None),
            "public_decided_by": approval.decided_by,
            "public_feedback": approval.decision_feedback,
            "requested_decision": decision.value,
        },
    )


def _require_runtime_record_match(
    request: RuntimeApprovalRequestRecord | None,
    *,
    approval_id: str,
    status: ApprovalStatus,
    decision: ApprovalDecision,
    decided_by: str,
    feedback: str | None,
) -> None:
    if (
        request is not None
        and request.status == status.value
        and request.decision == decision.value
        and request.decided_by == decided_by
        and request.feedback == feedback
        and request.decided_at is not None
    ):
        return
    _raise_decision_conflict(
        approval_id,
        "The Runtime Approval already has a conflicting durable decision",
        runtime_record=request,
        requested_decision=decision,
    )


def _raise_decision_conflict(
    approval_id: str,
    message: str,
    *,
    runtime_record: RuntimeApprovalRequestRecord | None,
    requested_decision: ApprovalDecision,
) -> None:
    raise RepositoryDecisionConflictError(
        message,
        details={
            "approval_id": approval_id,
            "runtime_status": runtime_record.status if runtime_record is not None else None,
            "runtime_decision": (runtime_record.decision if runtime_record is not None else None),
            "runtime_decided_by": (
                runtime_record.decided_by if runtime_record is not None else None
            ),
            "runtime_feedback": (runtime_record.feedback if runtime_record is not None else None),
            "requested_decision": requested_decision.value,
        },
    )


async def _next_event_sequence(session: AsyncSession, run_id: str) -> int:
    current = await session.scalar(
        select(func.max(RunEventRecord.sequence)).where(RunEventRecord.run_id == run_id)
    )
    return (current or 0) + 1
