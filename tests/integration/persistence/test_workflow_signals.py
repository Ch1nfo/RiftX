from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from sqlalchemy import select, update
from tests.integration.persistence.test_audit_repositories import (
    _create_audit,
    _create_engagement,
    _project,
    _snapshot,
)

from riftx.application.errors import (
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
)
from riftx.application.ports import AuditControlTransition
from riftx.application.services.workflow_signals import (
    WorkflowSignalDispatcher,
    WorkflowSignalOutcomeUnknown,
    WorkflowSignalTerminallyRejected,
)
from riftx.domain import (
    AuditLifecycleStatus,
    AuditPhase,
    Engagement,
    EntryPoint,
    EntryPointKind,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    PentestAdmission,
    PentestBudget,
    Run,
    RunKind,
    RunStatus,
    Scope,
)
from riftx.domain.workflow_signal import (
    WorkflowSignalDeliveryState,
    WorkflowSignalIntent,
    WorkflowSignalKind,
    WorkflowSignalSourceKind,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAuditControlUnitOfWork,
    SQLAlchemyAuditProjectRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
    SQLAlchemySnapshotRepository,
)
from riftx.persistence.orm import (
    AuditScanRecord,
    ExecutionRecord,
    RunEventRecord,
    RunRecord,
)
from riftx.persistence.workflow_signals import (
    SQLAlchemyWorkflowSignalIntentRepository,
    WorkflowSignalIntentRecord,
    _intent_to_record,
    _require_exact_owner_binding,
    _require_exact_source_binding,
)
from riftx.temporal.workflow_signal_transport import RoutedWorkflowSignalTransport

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _general_database(tmp_path: Path) -> tuple[Database, Run]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'workflow-signals.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-general", name="General")
    )
    run = Run(
        id="run-general",
        engagement_id="engagement-general",
        kind=RunKind.GENERAL,
        node_id="local",
        objective=Objective(description="Test durable signals"),
        workspace_path="/tmp/riftx/run-general",
        temporal_workflow_id="riftx-run-run-general",
    )
    await SQLAlchemyRunRepository(database.session_factory).create(run)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    running = await _persist_running_execution(
        executions,
        _execution(tmp_path, run, execution_id="execution-1"),
    )
    completed = running.model_copy(deep=True)
    completed.transition_to(
        ExecutionStatus.COMPLETED,
        at=NOW + timedelta(seconds=3),
        exit_code=0,
    )
    _, saved = await executions.save_if_status(
        completed,
        expected={ExecutionStatus.RUNNING},
    )
    assert saved is True
    return database, run


async def _pentest_database(tmp_path: Path) -> tuple[Database, Run]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pentest-signals.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(
            id="engagement-pentest",
            name="Pentest",
            authorization_reference="authorization://pentest-signals",
        )
    )
    run = Run(
        id="pentest-1",
        engagement_id="engagement-pentest",
        kind=RunKind.PENTEST,
        node_id="local",
        objective=Objective(description="Test Pentest durable signals"),
        entry_points=[EntryPoint(kind=EntryPointKind.DOMAIN, value="example.test")],
        scope=Scope(domains=["example.test"]),
        pentest_admission=PentestAdmission(
            budget=PentestBudget(
                max_duration_seconds=3600,
                max_model_calls=100,
                max_tokens=100_000,
                max_tool_calls=200,
                max_target_interactions=50,
                max_concurrent_target_interactions=2,
            )
        ),
        workspace_path="/tmp/riftx/pentest-1",
        temporal_workflow_id="riftx-pentest-pentest-1",
    )
    await SQLAlchemyRunRepository(database.session_factory).create(run)
    return database, run


def _completion(run: Run, *, payload_status: str = "completed") -> WorkflowSignalIntent:
    return WorkflowSignalIntent.general_run(
        run_id=run.id,
        workflow_id=run.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.EXECUTION_COMPLETED,
        source_event_kind=WorkflowSignalSourceKind.EXECUTION_TERMINAL,
        source_event_id="execution-1",
        source_state_version=1,
        payload={
            "execution_id": "execution-1",
            **({"status": payload_status} if payload_status != "completed" else {}),
        },
        created_at=NOW,
    )


def _execution(tmp_path: Path, run: Run, *, execution_id: str) -> Execution:
    return Execution(
        id=execution_id,
        execution_key=f"key:{execution_id}",
        run_id=run.id,
        node_id=run.node_id,
        executor_type=ExecutorType.PROCESS,
        argv=["printf", "ok"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{execution_id}.stdout"),
        stderr_path=str(tmp_path / f"{execution_id}.stderr"),
        created_at=NOW,
    )


async def _persist_running_execution(
    repository: SQLAlchemyExecutionRepository,
    execution: Execution,
) -> Execution:
    await repository.create_if_absent(execution)
    starting = execution.model_copy(deep=True)
    starting.transition_to(ExecutionStatus.STARTING, at=NOW + timedelta(seconds=1))
    _, saved = await repository.save_if_status(
        starting,
        expected={ExecutionStatus.CREATED},
    )
    assert saved is True
    running = starting.model_copy(deep=True)
    running.transition_to(ExecutionStatus.RUNNING, at=NOW + timedelta(seconds=2))
    _, saved = await repository.save_if_status(
        running,
        expected={ExecutionStatus.STARTING},
    )
    assert saved is True
    return running


async def _all_signal_records(database: Database) -> list[WorkflowSignalIntentRecord]:
    async with database.session_factory() as session:
        return list(
            await session.scalars(
                select(WorkflowSignalIntentRecord).order_by(
                    WorkflowSignalIntentRecord.created_at,
                    WorkflowSignalIntentRecord.id,
                )
            )
        )


async def _seed_started_audit_state(
    database: Database,
    *,
    audit_id: str,
    run_id: str,
    audit_status: AuditLifecycleStatus,
    run_status: RunStatus,
    state_version: int = 2,
) -> None:
    async with database.session_factory() as session, session.begin():
        scan_result = await session.execute(
            update(AuditScanRecord)
            .where(AuditScanRecord.id == audit_id)
            .values(
                lifecycle_status=audit_status.value,
                current_phase=AuditPhase.MAP_SCOPE.value,
                state_version=state_version,
                started_at=NOW,
            )
        )
        run_result = await session.execute(
            update(RunRecord)
            .where(RunRecord.id == run_id)
            .values(status=run_status.value, started_at=NOW)
        )
        assert scan_result.rowcount == run_result.rowcount == 1  # type: ignore[attr-defined]


async def _project_audit_control(
    database: Database,
    *,
    audit_id: str,
    run_id: str,
    source_audit_status: AuditLifecycleStatus,
    source_run_status: RunStatus,
    target_audit_status: AuditLifecycleStatus,
    target_run_status: RunStatus,
    signal_kind: WorkflowSignalKind,
    expected_state_version: int,
    event_suffix: str,
) -> None:
    await SQLAlchemyAuditControlUnitOfWork(database.session_factory).transition(
        AuditControlTransition(
            audit_id=audit_id,
            run_id=run_id,
            expected_audit_state_version=expected_state_version,
            expected_audit_lifecycle=source_audit_status,
            expected_run_status=source_run_status,
            target_audit_lifecycle=target_audit_status,
            target_run_status=target_run_status,
            operation=signal_kind.value,
            reason_code={
                WorkflowSignalKind.PAUSE: "audit_pause_requested",
                WorkflowSignalKind.RESUME: "audit_resume_requested",
                WorkflowSignalKind.CANCEL: "audit_cancel_requested",
            }[signal_kind],
            occurred_at=NOW + timedelta(minutes=1),
            audit_event_id=f"audit-control-{event_suffix}",
            run_event_id=f"run-control-{event_suffix}",
            workflow_signal_kind=signal_kind,
        )
    )


async def test_create_is_exactly_idempotent_and_payload_divergence_conflicts(
    tmp_path: Path,
) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    intent = _completion(run)

    first, created = await repository.create(intent)
    replay, replay_created = await repository.create(
        _completion(run).model_copy(update={"id": "different-id"})
    )

    assert created is True
    assert replay_created is False
    assert replay.id == first.id
    with pytest.raises(RepositoryConflictError):
        await repository.create(_completion(run, payload_status="failed"))
    await database.dispose()


async def test_create_in_session_rolls_back_with_business_transaction(tmp_path: Path) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    intent = _completion(run)

    with pytest.raises(RuntimeError, match="rollback"):
        async with database.session_factory() as session, session.begin():
            await repository.create_in_session(session, intent)
            raise RuntimeError("rollback")

    assert await repository.get(intent.id) is None
    await database.dispose()


async def test_execution_terminal_transition_atomically_creates_one_signal_intent(
    tmp_path: Path,
) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=True,
    )
    running = await _persist_running_execution(
        repository,
        _execution(tmp_path, run, execution_id="execution-terminal"),
    )
    completed = running.model_copy(deep=True)
    completed.transition_to(
        ExecutionStatus.COMPLETED,
        at=NOW + timedelta(seconds=3),
        exit_code=0,
    )

    persisted, saved = await repository.save_if_status(
        completed,
        expected={ExecutionStatus.RUNNING},
    )
    replay, replay_saved = await repository.save_if_status(
        completed,
        expected={ExecutionStatus.RUNNING},
    )

    assert saved is True
    assert persisted.status is ExecutionStatus.COMPLETED
    assert replay_saved is False
    assert replay.status is ExecutionStatus.COMPLETED
    records = await _all_signal_records(database)
    assert len(records) == 1
    assert records[0].signal_kind == "execution_completed"
    assert records[0].source_event_id == completed.id
    assert records[0].payload_json == '{"execution_id":"execution-terminal"}'
    assert records[0].delivery_state == "pending"
    await database.dispose()


async def test_pentest_execution_terminal_uses_distinct_workflow_owner(
    tmp_path: Path,
) -> None:
    database, run = await _pentest_database(tmp_path)
    repository = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=True,
    )
    running = await _persist_running_execution(
        repository,
        _execution(tmp_path, run, execution_id="execution-pentest"),
    )
    completed = running.model_copy(deep=True)
    completed.transition_to(
        ExecutionStatus.COMPLETED,
        at=NOW + timedelta(seconds=3),
        exit_code=0,
    )

    _, saved = await repository.save_if_status(
        completed,
        expected={ExecutionStatus.RUNNING},
    )

    assert saved is True
    records = await _all_signal_records(database)
    assert len(records) == 1
    assert records[0].owner_kind == "pentest_run"
    assert records[0].owner_identity == "pentest_run:pentest-1"
    assert records[0].run_kind == "pentest"
    assert records[0].workflow_protocol_version == "riftx.pentest-run-workflow/v1"
    assert records[0].workflow_id == "riftx-pentest-pentest-1"
    signal_repository = SQLAlchemyWorkflowSignalIntentRepository(
        database.session_factory
    )
    stored = await signal_repository.get(records[0].id)
    assert stored is not None
    await signal_repository.validate_for_delivery(stored)
    await database.dispose()


async def test_execution_and_signal_intent_roll_back_together_on_conflict(
    tmp_path: Path,
) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=True,
    )
    running = await _persist_running_execution(
        repository,
        _execution(tmp_path, run, execution_id="execution-conflict"),
    )
    conflicting = WorkflowSignalIntent.general_run(
        run_id=run.id,
        workflow_id=run.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.EXECUTION_COMPLETED,
        source_event_kind=WorkflowSignalSourceKind.EXECUTION_TERMINAL,
        source_event_id=running.id,
        source_state_version=1,
        payload={"execution_id": running.id, "conflicting": True},
        created_at=NOW,
    )
    async with database.session_factory() as session, session.begin():
        session.add(_intent_to_record(conflicting))
    completed = running.model_copy(deep=True)
    completed.transition_to(
        ExecutionStatus.COMPLETED,
        at=NOW + timedelta(seconds=3),
        exit_code=0,
    )

    with pytest.raises(RepositoryConflictError, match="different immutable facts"):
        await repository.save_if_status(
            completed,
            expected={ExecutionStatus.RUNNING},
        )

    durable = await repository.get(running.id)
    assert durable is not None
    assert durable.status is ExecutionStatus.RUNNING
    records = await _all_signal_records(database)
    assert len(records) == 1
    assert records[0].payload_json == (
        '{"conflicting":true,"execution_id":"execution-conflict"}'
    )
    await database.dispose()


async def test_non_emitting_execution_repository_never_creates_completion_intent(
    tmp_path: Path,
) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyExecutionRepository(database.session_factory)
    running = await _persist_running_execution(
        repository,
        _execution(tmp_path, run, execution_id="execution-stop-projection"),
    )
    cancelled = running.model_copy(deep=True)
    cancelled.transition_to(
        ExecutionStatus.CANCELLED,
        at=NOW + timedelta(seconds=3),
    )
    cancelled.physical_stop_confirmed_at = NOW + timedelta(seconds=3)

    _, saved = await repository.save_if_status(
        cancelled,
        expected={ExecutionStatus.RUNNING},
    )

    assert saved is True
    assert await _all_signal_records(database) == []
    await database.dispose()


async def test_code_audit_terminal_execution_never_falls_back_to_general_signal(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-execution-signal.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-audit", name="Audit")
    )
    run = Run(
        id="run-audit",
        engagement_id="engagement-audit",
        kind=RunKind.CODE_AUDIT,
        node_id="local",
        objective=Objective(description="Audit completion routing"),
        workspace_path=str(tmp_path),
        temporal_workflow_id="riftx-code-audit-audit-1",
    )
    await SQLAlchemyRunRepository(database.session_factory).create(run)
    repository = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=True,
    )
    running = await _persist_running_execution(
        repository,
        _execution(tmp_path, run, execution_id="audit-execution"),
    )
    failed = running.model_copy(deep=True)
    failed.transition_to(
        ExecutionStatus.FAILED,
        at=NOW + timedelta(seconds=3),
    )

    _, saved = await repository.save_if_status(
        failed,
        expected={ExecutionStatus.RUNNING},
    )

    assert saved is True
    assert await _all_signal_records(database) == []
    await database.dispose()


async def test_repository_rejects_workflow_id_not_owned_by_run(tmp_path: Path) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    intent = WorkflowSignalIntent.general_run(
        run_id=run.id,
        workflow_id="riftx-run-wrong",
        signal_kind=WorkflowSignalKind.CANCEL,
        source_event_kind=WorkflowSignalSourceKind.CONTROL_INTENT,
        source_event_id="cancel-1",
        source_state_version=1,
        payload={"run_id": run.id},
        created_at=NOW,
    )

    with pytest.raises(RepositoryConflictError, match="authoritative Run owner"):
        await repository.create(intent)
    await database.dispose()


async def test_repository_rejects_missing_child_sources_without_writing(
    tmp_path: Path,
) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    missing_execution = WorkflowSignalIntent.general_run(
        run_id=run.id,
        workflow_id=run.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.EXECUTION_COMPLETED,
        source_event_kind=WorkflowSignalSourceKind.EXECUTION_TERMINAL,
        source_event_id="missing-execution",
        source_state_version=1,
        payload={"execution_id": "missing-execution"},
        created_at=NOW,
    )
    missing_approval = WorkflowSignalIntent.general_run(
        run_id=run.id,
        workflow_id=run.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.APPROVE,
        source_event_kind=WorkflowSignalSourceKind.APPROVAL_DECISION,
        source_event_id="missing-approval-event",
        source_state_version=1,
        payload={"approval_id": "missing-approval"},
        created_at=NOW,
    )
    undefined_safety_source = WorkflowSignalIntent.general_run(
        run_id=run.id,
        workflow_id=run.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.SAFETY_RECONCILE,
        source_event_kind=WorkflowSignalSourceKind.SAFETY_RECONCILIATION,
        source_event_id="undefined-safety-source",
        source_state_version=1,
        payload={},
        created_at=NOW,
    )

    with pytest.raises(EntityNotFoundError):
        await repository.create(missing_execution)
    with pytest.raises(EntityNotFoundError):
        await repository.create(missing_approval)
    with pytest.raises(RepositoryConflictError, match="no authoritative binding"):
        await repository.create(undefined_safety_source)

    assert await _all_signal_records(database) == []
    await database.dispose()


async def test_repository_rejects_foreign_execution_source_without_writing(
    tmp_path: Path,
) -> None:
    database, run = await _general_database(tmp_path)
    foreign_run = Run(
        id="run-foreign",
        engagement_id=run.engagement_id,
        kind=RunKind.GENERAL,
        node_id=run.node_id,
        objective=Objective(description="Foreign Workflow signal source"),
        workspace_path=str(tmp_path / "foreign"),
        temporal_workflow_id="riftx-run-run-foreign",
    )
    await SQLAlchemyRunRepository(database.session_factory).create(foreign_run)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    running = await _persist_running_execution(
        executions,
        _execution(tmp_path, foreign_run, execution_id="execution-foreign"),
    )
    completed = running.model_copy(deep=True)
    completed.transition_to(
        ExecutionStatus.COMPLETED,
        at=NOW + timedelta(seconds=3),
        exit_code=0,
    )
    _, saved = await executions.save_if_status(
        completed,
        expected={ExecutionStatus.RUNNING},
    )
    assert saved is True
    intent = WorkflowSignalIntent.general_run(
        run_id=run.id,
        workflow_id=run.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.EXECUTION_COMPLETED,
        source_event_kind=WorkflowSignalSourceKind.EXECUTION_TERMINAL,
        source_event_id=completed.id,
        source_state_version=1,
        payload={"execution_id": completed.id},
        created_at=NOW,
    )

    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    with pytest.raises(RepositoryConflictError, match="Execution source"):
        await repository.create(intent)

    assert await _all_signal_records(database) == []
    await database.dispose()


async def test_general_execution_delivery_fences_run_without_locking_execution() -> None:
    run = Run(
        id="run-lock-order",
        engagement_id="engagement-lock-order",
        kind=RunKind.GENERAL,
        node_id="local",
        objective=Objective(description="Workflow signal lock order"),
        workspace_path="/tmp/riftx/run-lock-order",
        temporal_workflow_id="riftx-run-run-lock-order",
    )
    intent = WorkflowSignalIntent.general_run(
        run_id=run.id,
        workflow_id=run.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.EXECUTION_COMPLETED,
        source_event_kind=WorkflowSignalSourceKind.EXECUTION_TERMINAL,
        source_event_id="execution-lock-order",
        source_state_version=1,
        payload={"execution_id": "execution-lock-order"},
        created_at=NOW,
    )
    durable_run = SimpleNamespace(
        kind=RunKind.GENERAL.value,
        temporal_workflow_id=run.temporal_workflow_id,
    )
    durable_execution = SimpleNamespace(
        run_id=run.id,
        audit_id=None,
        plan_digest=None,
        status=ExecutionStatus.COMPLETED.value,
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[durable_run, durable_execution]),
    )

    await _require_exact_owner_binding(session, intent)  # type: ignore[arg-type]
    await _require_exact_source_binding(session, intent)  # type: ignore[arg-type]

    assert session.get.await_args_list == [
        call(RunRecord, run.id, with_for_update=True),
        call(ExecutionRecord, "execution-lock-order"),
    ]


@pytest.mark.parametrize(
    ("corrupt_field", "corrupt_value"),
    [
        ("run_id", "run-foreign"),
        ("audit_id", "audit-foreign"),
        ("plan_digest", "a" * 64),
        ("status", ExecutionStatus.RUNNING.value),
    ],
)
async def test_nonlocking_execution_source_read_still_validates_exact_binding(
    corrupt_field: str,
    corrupt_value: str,
) -> None:
    run = Run(
        id="run-source-validation",
        engagement_id="engagement-source-validation",
        kind=RunKind.GENERAL,
        node_id="local",
        objective=Objective(description="Workflow signal source validation"),
        workspace_path="/tmp/riftx/run-source-validation",
        temporal_workflow_id="riftx-run-run-source-validation",
    )
    intent = WorkflowSignalIntent.general_run(
        run_id=run.id,
        workflow_id=run.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.EXECUTION_COMPLETED,
        source_event_kind=WorkflowSignalSourceKind.EXECUTION_TERMINAL,
        source_event_id="execution-source-validation",
        source_state_version=1,
        payload={"execution_id": "execution-source-validation"},
        created_at=NOW,
    )
    source = {
        "run_id": run.id,
        "audit_id": None,
        "plan_digest": None,
        "status": ExecutionStatus.FAILED.value,
    }
    source[corrupt_field] = corrupt_value
    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(**source)))

    with pytest.raises(RepositoryConflictError, match="Execution source"):
        await _require_exact_source_binding(session, intent)  # type: ignore[arg-type]

    session.get.assert_awaited_once_with(
        ExecutionRecord,
        "execution-source-validation",
    )


async def test_delivery_claim_is_single_winner_and_cas_delivered(tmp_path: Path) -> None:
    database, run = await _general_database(tmp_path)
    first = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    second = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    intent, _ = await first.create(_completion(run))

    left, right = await asyncio.gather(
        first.claim_delivery_batch(
            lease_owner="worker-left",
            now=NOW,
            lease_duration=timedelta(seconds=30),
            limit=1,
        ),
        second.claim_delivery_batch(
            lease_owner="worker-right",
            now=NOW,
            lease_duration=timedelta(seconds=30),
            limit=1,
        ),
    )
    winners = [*left, *right]

    assert len(winners) == 1
    claimed = winners[0]
    assert claimed.delivery_state is WorkflowSignalDeliveryState.CLAIMED
    assert claimed.attempt == 1
    delivered = await first.mark_delivered(
        intent.id,
        lease_owner=claimed.lease_owner or "",
        expected_state_version=claimed.state_version,
        receipt_digest=_digest("delivery-receipt"),
        delivered_at=NOW + timedelta(seconds=1),
    )
    assert delivered.delivery_state is WorkflowSignalDeliveryState.DELIVERED
    assert delivered.delivery_receipt_digest == _digest("delivery-receipt")
    with pytest.raises(RepositoryConflictError):
        await first.mark_delivered(
            intent.id,
            lease_owner=claimed.lease_owner or "",
            expected_state_version=claimed.state_version,
            receipt_digest=_digest("duplicate-receipt"),
            delivered_at=NOW + timedelta(seconds=2),
        )
    await database.dispose()


async def test_expired_delivery_claim_requires_reconciliation_before_retry(
    tmp_path: Path,
) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    intent, _ = await repository.create(_completion(run))
    claimed = (
        await repository.claim_delivery_batch(
            lease_owner="dispatcher",
            now=NOW,
            lease_duration=timedelta(seconds=1),
            limit=1,
        )
    )[0]

    recovered = await repository.recover_expired_delivery_claims(
        now=NOW + timedelta(seconds=2),
        next_attempt_at=NOW + timedelta(seconds=3),
        limit=10,
    )
    assert recovered == 1
    unknown = await repository.get(intent.id)
    assert unknown is not None
    assert unknown.delivery_state is WorkflowSignalDeliveryState.OUTCOME_UNKNOWN
    assert unknown.attempt == claimed.attempt
    assert not await repository.claim_delivery_batch(
        lease_owner="must-not-redeliver",
        now=NOW + timedelta(seconds=4),
        lease_duration=timedelta(seconds=30),
    )

    reconciliation = (
        await repository.claim_reconciliation_batch(
            lease_owner="reconciler",
            now=NOW + timedelta(seconds=4),
            lease_duration=timedelta(seconds=30),
        )
    )[0]
    observed = await repository.mark_observed_delivered(
        intent.id,
        lease_owner="reconciler",
        expected_state_version=reconciliation.state_version,
        receipt_digest=_digest("observed-delivery"),
        observed_at=NOW + timedelta(seconds=5),
    )
    assert observed.delivery_state is WorkflowSignalDeliveryState.OBSERVED_DELIVERED
    await database.dispose()


async def test_superseded_intent_is_terminal_and_never_claimed_again(
    tmp_path: Path,
) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    intent, _ = await repository.create(_completion(run))
    claimed = (
        await repository.claim_delivery_batch(
            lease_owner="dispatcher",
            now=NOW,
            lease_duration=timedelta(seconds=30),
            limit=1,
        )
    )[0]

    superseded = await repository.mark_superseded(
        intent.id,
        lease_owner="dispatcher",
        expected_state_version=claimed.state_version,
        error_code="run_terminal",
        updated_at=NOW + timedelta(seconds=1),
    )

    assert superseded.delivery_state is WorkflowSignalDeliveryState.SUPERSEDED
    assert superseded.last_error_code == "run_terminal"
    assert superseded.next_attempt_at is None
    assert superseded.lease_owner is None
    assert not await repository.claim_delivery_batch(
        lease_owner="must-not-redeliver",
        now=NOW + timedelta(days=1),
        lease_duration=timedelta(seconds=30),
    )
    assert not await repository.claim_reconciliation_batch(
        lease_owner="must-not-reconcile",
        now=NOW + timedelta(days=1),
        lease_duration=timedelta(seconds=30),
    )
    await database.dispose()


async def test_code_audit_intent_requires_exact_scan_run_and_workflow_binding(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-signals.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(
        database,
        audit_id="audit-1",
        run_id="run-audit",
        queued=True,
    )
    await _seed_started_audit_state(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        audit_status=AuditLifecycleStatus.RUNNING,
        run_status=RunStatus.RUNNING,
    )
    await _project_audit_control(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        source_audit_status=AuditLifecycleStatus.RUNNING,
        source_run_status=RunStatus.RUNNING,
        target_audit_status=AuditLifecycleStatus.CANCELLING,
        target_run_status=RunStatus.CANCELLING,
        signal_kind=WorkflowSignalKind.CANCEL,
        expected_state_version=2,
        event_suffix="cancel-1",
    )
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    records = await _all_signal_records(database)
    assert len(records) == 1
    stored = await repository.get(records[0].id)
    assert stored is not None
    assert stored.audit_id == scan.id
    await repository.validate_for_delivery(stored)

    mismatched = WorkflowSignalIntent.code_audit(
        audit_id=scan.id,
        run_id="different-run",
        workflow_id=scan.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.CANCEL,
        source_event_kind=WorkflowSignalSourceKind.CONTROL_INTENT,
        source_event_id="audit-control-cancel-1",
        source_state_version=3,
        payload={"audit_id": scan.id},
        created_at=NOW,
    )
    with pytest.raises(EntityNotFoundError):
        await repository.create(mismatched)
    await database.dispose()


@pytest.mark.parametrize(
    (
        "signal_kind",
        "source_audit_status",
        "source_run_status",
        "target_audit_status",
        "target_run_status",
    ),
    [
        (
            WorkflowSignalKind.PAUSE,
            AuditLifecycleStatus.RUNNING,
            RunStatus.RUNNING,
            AuditLifecycleStatus.PAUSING,
            RunStatus.PAUSING,
        ),
        (
            WorkflowSignalKind.RESUME,
            AuditLifecycleStatus.PAUSED,
            RunStatus.PAUSED,
            AuditLifecycleStatus.RUNNING,
            RunStatus.RUNNING,
        ),
        (
            WorkflowSignalKind.CANCEL,
            AuditLifecycleStatus.RUNNING,
            RunStatus.RUNNING,
            AuditLifecycleStatus.CANCELLING,
            RunStatus.CANCELLING,
        ),
    ],
)
async def test_started_audit_control_atomically_stages_one_owner_bound_intent(
    tmp_path: Path,
    signal_kind: WorkflowSignalKind,
    source_audit_status: AuditLifecycleStatus,
    source_run_status: RunStatus,
    target_audit_status: AuditLifecycleStatus,
    target_run_status: RunStatus,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{tmp_path / f'audit-{signal_kind.value}-atomic.db'}"
    )
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(
        database,
        audit_id=f"audit-{signal_kind.value}",
        run_id=f"run-{signal_kind.value}",
        queued=True,
    )
    await _seed_started_audit_state(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        audit_status=source_audit_status,
        run_status=source_run_status,
    )

    await _project_audit_control(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        source_audit_status=source_audit_status,
        source_run_status=source_run_status,
        target_audit_status=target_audit_status,
        target_run_status=target_run_status,
        signal_kind=signal_kind,
        expected_state_version=2,
        event_suffix=signal_kind.value,
    )

    records = await _all_signal_records(database)
    assert len(records) == 1
    intent = await SQLAlchemyWorkflowSignalIntentRepository(
        database.session_factory
    ).get(records[0].id)
    assert intent is not None
    assert intent.owner_kind.value == "code_audit"
    assert intent.audit_id == scan.id
    assert intent.run_id == scan.run_id
    assert intent.signal_kind is signal_kind
    assert intent.source_event_id == f"audit-control-{signal_kind.value}"
    assert intent.source_state_version == 3
    assert intent.payload == {"audit_id": scan.id}
    async with database.session_factory() as session:
        source_event = await session.get(RunEventRecord, intent.source_event_id)
        durable_scan = await session.get(AuditScanRecord, scan.id)
        durable_run = await session.get(RunRecord, scan.run_id)
    assert source_event is not None
    assert source_event.event_type == "audit.control_projected"
    assert durable_scan is not None
    assert durable_scan.lifecycle_status == target_audit_status.value
    assert durable_run is not None
    assert durable_run.status == target_run_status.value
    await database.dispose()


async def test_draft_audit_cancel_projects_fence_without_workflow_intent(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-draft-cancel.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(
        database,
        audit_id="audit-draft",
        run_id="run-draft",
    )

    await _project_audit_control(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        source_audit_status=AuditLifecycleStatus.DRAFT,
        source_run_status=RunStatus.CREATED,
        target_audit_status=AuditLifecycleStatus.CANCELLING,
        target_run_status=RunStatus.CANCELLING,
        signal_kind=WorkflowSignalKind.CANCEL,
        expected_state_version=1,
        event_suffix="draft-cancel",
    )

    assert await _all_signal_records(database) == []
    async with database.session_factory() as session:
        durable_scan = await session.get(AuditScanRecord, scan.id)
        durable_run = await session.get(RunRecord, scan.run_id)
        source_event = await session.get(RunEventRecord, "audit-control-draft-cancel")
    assert durable_scan is not None
    assert durable_scan.lifecycle_status == AuditLifecycleStatus.CANCELLING.value
    assert durable_run is not None
    assert durable_run.status == RunStatus.CANCELLING.value
    assert source_event is not None
    await database.dispose()


async def test_audit_control_projection_rolls_back_state_and_events_if_intent_conflicts(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-control-rollback.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(
        database,
        audit_id="audit-rollback",
        run_id="run-rollback",
        queued=True,
    )
    await _seed_started_audit_state(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        audit_status=AuditLifecycleStatus.RUNNING,
        run_status=RunStatus.RUNNING,
    )
    conflicting = WorkflowSignalIntent.code_audit(
        audit_id=scan.id,
        run_id=scan.run_id,
        workflow_id=scan.temporal_workflow_id,
        signal_kind=WorkflowSignalKind.PAUSE,
        source_event_kind=WorkflowSignalSourceKind.CONTROL_INTENT,
        source_event_id="audit-control-rollback-pause",
        source_state_version=3,
        payload={"audit_id": scan.id, "conflicting": True},
        created_at=NOW + timedelta(minutes=1),
    )
    async with database.session_factory() as session, session.begin():
        session.add(_intent_to_record(conflicting))

    with pytest.raises(RepositoryConflictError, match="different immutable facts"):
        await _project_audit_control(
            database,
            audit_id=scan.id,
            run_id=scan.run_id,
            source_audit_status=AuditLifecycleStatus.RUNNING,
            source_run_status=RunStatus.RUNNING,
            target_audit_status=AuditLifecycleStatus.PAUSING,
            target_run_status=RunStatus.PAUSING,
            signal_kind=WorkflowSignalKind.PAUSE,
            expected_state_version=2,
            event_suffix="rollback-pause",
        )

    async with database.session_factory() as session:
        durable_scan = await session.get(AuditScanRecord, scan.id)
        durable_run = await session.get(RunRecord, scan.run_id)
        control_event = await session.get(
            RunEventRecord,
            "audit-control-rollback-pause",
        )
        run_event = await session.get(RunEventRecord, "run-control-rollback-pause")
    assert durable_scan is not None
    assert durable_scan.lifecycle_status == AuditLifecycleStatus.RUNNING.value
    assert durable_scan.state_version == 2
    assert durable_run is not None
    assert durable_run.status == RunStatus.RUNNING.value
    assert control_event is run_event is None
    records = await _all_signal_records(database)
    assert len(records) == 1
    assert records[0].payload_json == (
        f'{{"audit_id":"{scan.id}","conflicting":true}}'
    )
    await database.dispose()


async def test_audit_control_source_must_be_present_exact_and_same_run(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-control-source.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(
        database,
        audit_id="audit-source",
        run_id="run-source",
        queued=True,
    )
    await _seed_started_audit_state(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        audit_status=AuditLifecycleStatus.RUNNING,
        run_status=RunStatus.RUNNING,
    )
    await _project_audit_control(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        source_audit_status=AuditLifecycleStatus.RUNNING,
        source_run_status=RunStatus.RUNNING,
        target_audit_status=AuditLifecycleStatus.PAUSING,
        target_run_status=RunStatus.PAUSING,
        signal_kind=WorkflowSignalKind.PAUSE,
        expected_state_version=2,
        event_suffix="source-pause",
    )
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    baseline = await _all_signal_records(database)
    assert len(baseline) == 1

    def candidate(source_event_id: str) -> WorkflowSignalIntent:
        return WorkflowSignalIntent.code_audit(
            audit_id=scan.id,
            run_id=scan.run_id,
            workflow_id=scan.temporal_workflow_id,
            signal_kind=WorkflowSignalKind.PAUSE,
            source_event_kind=WorkflowSignalSourceKind.CONTROL_INTENT,
            source_event_id=source_event_id,
            source_state_version=3,
            payload={"audit_id": scan.id},
            created_at=NOW + timedelta(minutes=1),
        )

    with pytest.raises(EntityNotFoundError):
        await repository.create(candidate("missing-control-source"))

    foreign_run = Run(
        id="run-foreign-control-source",
        engagement_id="engagement-1",
        kind=RunKind.GENERAL,
        node_id="local",
        objective=Objective(description="Foreign control event"),
        workspace_path=str(tmp_path / "foreign-control-source"),
        temporal_workflow_id="riftx-run-run-foreign-control-source",
    )
    await SQLAlchemyRunRepository(database.session_factory).create(foreign_run)
    exact_payload = {
        "audit_id": scan.id,
        "operation": "pause",
        "reason_code": "audit_pause_requested",
        "from_audit_lifecycle": "running",
        "to_audit_lifecycle": "pausing",
        "from_run_status": "running",
        "to_run_status": "pausing",
        "audit_state_version": 3,
    }
    async with database.session_factory() as session, session.begin():
        session.add(
            RunEventRecord(
                id="foreign-control-source",
                run_id=foreign_run.id,
                sequence=99,
                event_type="audit.control_projected",
                payload_json=exact_payload,
                created_at=NOW + timedelta(minutes=1),
            )
        )
        session.add(
            RunEventRecord(
                id="corrupt-control-source",
                run_id=scan.run_id,
                sequence=99,
                event_type="audit.control_projected",
                payload_json={**exact_payload, "operation": "cancel"},
                created_at=NOW + timedelta(minutes=1),
            )
        )

    with pytest.raises(RepositoryConflictError, match="Audit control source"):
        await repository.create(candidate("foreign-control-source"))
    with pytest.raises(RepositoryConflictError, match="Audit control source"):
        await repository.create(candidate("corrupt-control-source"))
    assert len(await _all_signal_records(database)) == 1
    await database.dispose()


async def test_concurrent_cancel_fence_supersedes_pending_audit_resume_before_router(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-resume-cancel.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(
        database,
        audit_id="audit-resume-cancel",
        run_id="run-resume-cancel",
        queued=True,
    )
    await _seed_started_audit_state(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        audit_status=AuditLifecycleStatus.PAUSED,
        run_status=RunStatus.PAUSED,
    )
    await _project_audit_control(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        source_audit_status=AuditLifecycleStatus.PAUSED,
        source_run_status=RunStatus.PAUSED,
        target_audit_status=AuditLifecycleStatus.RUNNING,
        target_run_status=RunStatus.RUNNING,
        signal_kind=WorkflowSignalKind.RESUME,
        expected_state_version=2,
        event_suffix="pending-resume",
    )
    await _project_audit_control(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        source_audit_status=AuditLifecycleStatus.RUNNING,
        source_run_status=RunStatus.RUNNING,
        target_audit_status=AuditLifecycleStatus.CANCELLING,
        target_run_status=RunStatus.CANCELLING,
        signal_kind=WorkflowSignalKind.CANCEL,
        expected_state_version=3,
        event_suffix="winning-cancel",
    )
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    intents = [
        item
        for record in await _all_signal_records(database)
        if (item := await repository.get(record.id)) is not None
    ]
    resume = next(
        item for item in intents if item.signal_kind is WorkflowSignalKind.RESUME
    )
    router = SimpleNamespace()
    transport = RoutedWorkflowSignalTransport(
        router,  # type: ignore[arg-type]
        runs=SQLAlchemyRunRepository(database.session_factory),
        sources=repository,
    )

    with pytest.raises(WorkflowSignalTerminallyRejected) as captured:
        await transport.send(resume)

    assert captured.value.error_code == "workflow_signal_rejected"
    await database.dispose()



async def test_audit_signal_accepted_then_disconnected_stays_unknown_and_probeable(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-signal-unknown.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(
        database,
        audit_id="audit-unknown",
        run_id="run-unknown",
        queued=True,
    )
    await _seed_started_audit_state(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        audit_status=AuditLifecycleStatus.RUNNING,
        run_status=RunStatus.RUNNING,
    )
    await _project_audit_control(
        database,
        audit_id=scan.id,
        run_id=scan.run_id,
        source_audit_status=AuditLifecycleStatus.RUNNING,
        source_run_status=RunStatus.RUNNING,
        target_audit_status=AuditLifecycleStatus.PAUSING,
        target_run_status=RunStatus.PAUSING,
        signal_kind=WorkflowSignalKind.PAUSE,
        expected_state_version=2,
        event_suffix="unknown-pause",
    )
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    transport = SimpleNamespace(
        send=AsyncMock(
            side_effect=WorkflowSignalOutcomeUnknown(
                "accepted_then_disconnected"
            )
        )
    )
    dispatch_at = NOW + timedelta(minutes=2)
    result = await WorkflowSignalDispatcher(
        repository=repository,
        transport=transport,  # type: ignore[arg-type]
        lease_owner="audit-dispatcher",
        clock=lambda: dispatch_at,
    ).dispatch_batch()

    assert result.claimed == result.outcome_unknown == 1
    record = (await _all_signal_records(database))[0]
    unknown = await repository.get(record.id)
    assert unknown is not None
    assert unknown.delivery_state is WorkflowSignalDeliveryState.OUTCOME_UNKNOWN
    assert unknown.last_error_code == "accepted_then_disconnected"
    assert unknown.attempt == 1
    probes = await repository.claim_reconciliation_batch(
        lease_owner="audit-probe",
        now=dispatch_at + timedelta(seconds=3),
        lease_duration=timedelta(seconds=30),
        limit=10,
    )
    assert [item.id for item in probes] == [unknown.id]
    transport.send.assert_awaited_once()
    await database.dispose()


async def test_noncanonical_persisted_payload_fails_integrity_validation(
    tmp_path: Path,
) -> None:
    database, run = await _general_database(tmp_path)
    repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    intent, _ = await repository.create(_completion(run))
    async with database.session_factory() as session, session.begin():
        await session.execute(
            update(WorkflowSignalIntentRecord)
            .where(WorkflowSignalIntentRecord.id == intent.id)
            .values(payload_json='{ "execution_id": "execution-1" }')
        )

    with pytest.raises(RepositoryIntegrityError):
        await repository.get(intent.id)
    await database.dispose()
