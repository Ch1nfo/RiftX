from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from sqlalchemy import select, update

from riftx.application.errors import (
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
)
from riftx.domain import (
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
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.orm import (
    ExecutionRecord,
    RunRecord,
)
from riftx.persistence.workflow_signals import (
    SQLAlchemyWorkflowSignalIntentRepository,
    WorkflowSignalIntentRecord,
    _intent_to_record,
    _require_exact_owner_binding,
    _require_exact_source_binding,
)

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
