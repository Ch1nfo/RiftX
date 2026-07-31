"""SQLAlchemy implementations of RiftX repository ports."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain import (
    Approval,
    ApprovalGrant,
    ApprovalStatus,
    Artifact,
    Engagement,
    Execution,
    ExecutionStatus,
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
    TerminalSessionRecord,
    ToolCallRecord,
)

SessionFactory = async_sessionmaker[AsyncSession]


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

    async def get(self, node_id: str) -> RunnerCredential | None:
        async with self._session_factory() as session:
            record = await session.get(RunnerCredentialRecord, node_id)
        return runner_credential_from_record(record) if record is not None else None

    async def save(self, credential: RunnerCredential) -> RunnerCredential:
        async with self._session_factory() as session, session.begin():
            record = await session.get(RunnerCredentialRecord, credential.node_id)
            if record is None:
                session.add(runner_credential_to_record(credential))
            else:
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
        lease_id: str,
        leased_until: datetime,
        now: datetime,
    ) -> RunnerCommand | None:
        if leased_until <= now:
            raise ValueError("leased_until must be later than now")
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(RunnerCommandRecord)
                .where(
                    RunnerCommandRecord.node_id == node_id,
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

            candidate_id = await session.scalar(
                select(RunnerCommandRecord.id)
                .where(
                    RunnerCommandRecord.node_id == node_id,
                    RunnerCommandRecord.status == RunnerCommandStatus.PENDING.value,
                )
                .order_by(
                    case(
                        (
                            RunnerCommandRecord.kind.in_(
                                [
                                    RunnerCommandKind.CANCEL.value,
                                    RunnerCommandKind.TERMINAL_CLOSE.value,
                                ]
                            ),
                            0,
                        ),
                        else_=1,
                    ),
                    RunnerCommandRecord.created_at,
                    RunnerCommandRecord.id,
                )
                .limit(1)
            )
            if candidate_id is None:
                return None
            claimed = await session.execute(
                update(RunnerCommandRecord)
                .where(
                    RunnerCommandRecord.id == candidate_id,
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

    async def finish(
        self,
        command_id: str,
        *,
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
    ) -> RunEvent:
        last_conflict: IntegrityError | None = None
        for attempt in range(10):
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
                last_conflict = exc
                await asyncio.sleep(attempt / 1000)
        raise RepositoryConflictError(
            f"could not append event for run {run_id!r} after concurrent retries"
        ) from last_conflict

    async def get(self, event_id: str) -> RunEvent | None:
        async with self._session_factory() as session:
            record = await session.get(RunEventRecord, event_id)
        return event_from_record(record) if record is not None else None

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


class SQLAlchemyFindingRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, finding: Finding) -> Finding:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(finding_to_record(finding))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(f"could not create finding {finding.id!r}") from exc
        return finding

    async def get(self, finding_id: str) -> Finding | None:
        async with self._session_factory() as session:
            record = await session.get(FindingRecord, finding_id)
        return finding_from_record(record) if record is not None else None

    async def save(self, finding: Finding) -> Finding:
        async with self._session_factory() as session, session.begin():
            record = await session.scalar(
                select(FindingRecord).where(FindingRecord.id == finding.id).with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("Finding", finding.id)
            if record.run_id != finding.run_id:
                raise RepositoryConflictError(f"cannot move finding {finding.id!r} between runs")
            apply_finding_to_record(finding, record)
            await session.flush()
        return finding

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
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

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
    ) -> tuple[Approval, bool]:
        async with self._session_factory() as session, session.begin():
            record = await session.scalar(
                select(ApprovalRecord).where(ApprovalRecord.id == approval_id).with_for_update()
            )
            if record is None:
                raise EntityNotFoundError("Approval", approval_id)
            approval = approval_from_record(record)
            if approval.status is status:
                return approval, False
            approval.decide(status, decided_by=decided_by, reason=reason)
            apply_approval_to_record(approval, record)
            tool_call = await session.get(ToolCallRecord, approval.tool_call_id)
            if tool_call is not None:
                tool_call.approval_status = status.value
            await session.flush()
        return approval, True

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
        async with self._session_factory() as session, session.begin():
            record = await session.get(TerminalSessionRecord, terminal.id)
            if record is None:
                raise EntityNotFoundError("TerminalSession", terminal.id)
            apply_terminal_to_record(terminal, record)
            await session.flush()
        return terminal

    async def list_open(self) -> Sequence[TerminalSession]:
        statement = (
            select(TerminalSessionRecord)
            .where(TerminalSessionRecord.status == TerminalStatus.OPEN.value)
            .order_by(TerminalSessionRecord.created_at, TerminalSessionRecord.id)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
        return [terminal_from_record(record) for record in records]


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


async def _next_event_sequence(session: AsyncSession, run_id: str) -> int:
    current = await session.scalar(
        select(func.max(RunEventRecord.sequence)).where(RunEventRecord.run_id == run_id)
    )
    return (current or 0) + 1
