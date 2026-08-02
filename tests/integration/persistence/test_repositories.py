import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

import riftx.persistence.repositories as repository_module
from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.application.finalization import cleanup_event_id, report_failure_event_id
from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    InvalidStateTransitionError,
    Objective,
    Run,
    RunKind,
    RunnerPrincipal,
    RunStatus,
    TerminalSession,
    TerminalStatus,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)


def make_run(
    *,
    run_id: str = "run-1",
    engagement_id: str = "engagement-1",
    kind: RunKind = RunKind.GENERAL,
) -> Run:
    return Run(
        kind=kind,
        id=run_id,
        engagement_id=engagement_id,
        node_id="local-node",
        objective=Objective(description="Verify persistence"),
        workspace_path=f"/tmp/riftx/{run_id}",
    )


def make_execution(*, execution_id: str) -> Execution:
    execution = Execution(
        id=execution_id,
        execution_key=f"execution-key:{execution_id}",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=["probe"],
        cwd="/tmp",
        stdout_path=f"/tmp/{execution_id}.stdout",
        stderr_path=f"/tmp/{execution_id}.stderr",
    )
    execution.transition_to(ExecutionStatus.STARTING)
    return execution


async def test_execution_identity_cas_rejects_split_brain_and_stale_terminal_write(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution-identity.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Execution identity CAS")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(make_run())
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    execution = make_execution(execution_id="execution-identity-cas")
    await executions.create_if_absent(execution)
    first = execution.model_copy(deep=True)
    first.pid = 42001
    first.process_group_id = 42001
    first.containment_id = "sql-containment-first"
    first.process_created_at = datetime(2026, 8, 1, tzinfo=UTC)
    first.executable_path = "/usr/bin/first"
    first.tool_id = "tool-first"
    first.tool_version = "1.0"
    first.platform_system = "Linux"
    first.platform_release = "first-release"
    first.platform_architecture = "x86_64"
    second = execution.model_copy(deep=True)
    second.pid = 42002
    second.process_group_id = 42002
    second.containment_id = "sql-containment-second"
    second.process_created_at = datetime(2026, 8, 2, tzinfo=UTC)
    second.executable_path = "/usr/bin/second"
    second.tool_id = "tool-second"
    second.tool_version = "2.0"
    second.platform_system = "Linux"
    second.platform_release = "second-release"
    second.platform_architecture = "aarch64"

    outcomes = await asyncio.gather(
        executions.save_if_status(first, expected={ExecutionStatus.STARTING}),
        executions.save_if_status(second, expected={ExecutionStatus.STARTING}),
        return_exceptions=True,
    )

    assert sum(isinstance(item, tuple) and item[1] is True for item in outcomes) == 1
    assert sum(isinstance(item, RepositoryConflictError) for item in outcomes) == 1
    bound = await executions.get(execution.id)
    assert bound is not None
    stale_callback = execution.model_copy(deep=True)
    callback_current, callback_saved = await executions.save_if_status(
        stale_callback,
        expected={ExecutionStatus.STARTING},
    )
    assert callback_saved is False
    assert callback_current.pid == bound.pid
    assert callback_current.tool_id == bound.tool_id
    assert callback_current.platform_architecture == bound.platform_architecture
    stale_cancel = execution.model_copy(deep=True)
    stale_cancel.transition_to(ExecutionStatus.CANCELLED)
    stale_cancel.physical_stop_confirmed_at = datetime(2026, 8, 3, tzinfo=UTC)

    current, saved = await executions.save_if_status(
        stale_cancel,
        expected={ExecutionStatus.STARTING},
    )

    assert saved is False
    assert current.status is ExecutionStatus.STARTING
    assert current.pid == bound.pid
    assert current.process_group_id == bound.process_group_id
    assert current.containment_id == bound.containment_id
    assert current.process_created_at == bound.process_created_at
    assert current.physical_stop_confirmed_at is None
    owner = RunnerPrincipal(instance_id="runner-owner", epoch=3)
    owner_binding = bound.model_copy(deep=True)
    owner_binding.owner = owner
    owned, owner_saved = await executions.save_if_status(
        owner_binding,
        expected={ExecutionStatus.STARTING},
    )
    assert owner_saved is True
    assert owned.owner == owner

    with pytest.raises(RepositoryConflictError, match="owner is already bound"):
        await executions.save_if_status(
            bound,
            expected={ExecutionStatus.STARTING},
        )
    wrong_key = owned.model_copy(deep=True)
    wrong_key.execution_key = "replacement-key"
    with pytest.raises(RepositoryConflictError, match="key is already bound"):
        await executions.save_if_status(
            wrong_key,
            expected={ExecutionStatus.STARTING},
        )
    await database.dispose()


async def test_terminal_status_is_monotonic_across_sql_repository_instances(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'terminal-status.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Terminal status CAS")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(make_run())
    execution = make_execution(execution_id="execution-terminal-status")
    await SQLAlchemyExecutionRepository(database.session_factory).create_if_absent(execution)
    repository = SQLAlchemyTerminalRepository(database.session_factory)
    terminal = TerminalSession(
        id="terminal-status-cas",
        run_id="run-1",
        execution_id=execution.id,
    )
    await repository.create(terminal)
    opened = terminal.model_copy(deep=True)
    opened.transition_to(TerminalStatus.OPEN)
    closed = terminal.model_copy(deep=True)
    closed.transition_to(TerminalStatus.CLOSED)

    await asyncio.gather(
        SQLAlchemyTerminalRepository(database.session_factory).save(opened),
        SQLAlchemyTerminalRepository(database.session_factory).save(closed),
    )

    persisted = await repository.get(terminal.id)
    assert persisted is not None and persisted.status is TerminalStatus.CLOSED
    stale_current, stale_saved = await SQLAlchemyTerminalRepository(
        database.session_factory
    ).save_if_status(
        opened,
        expected={TerminalStatus.CREATED},
    )
    assert stale_saved is False
    assert stale_current.status is TerminalStatus.CLOSED
    assert (
        await SQLAlchemyTerminalRepository(database.session_factory).save(opened)
    ).status is TerminalStatus.CLOSED
    await database.dispose()


async def test_terminal_projection_event_is_conditional_and_idempotent(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'terminal-events.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Terminal event projection")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(make_run())
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    terminals = SQLAlchemyTerminalRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    execution = make_execution(execution_id="execution-terminal-events")
    execution.executor_type = ExecutorType.PTY
    await executions.create_if_absent(execution)
    running = execution.model_copy(deep=True)
    running.transition_to(ExecutionStatus.RUNNING)
    _, saved = await executions.save_if_status(
        running,
        expected={ExecutionStatus.STARTING},
    )
    assert saved is True
    terminal = TerminalSession(
        id="terminal-events",
        run_id="run-1",
        execution_id=execution.id,
    )
    terminal.transition_to(TerminalStatus.OPEN)
    await terminals.create(terminal)

    skipped = await events.append_terminal_projection_if_current(
        "run-1",
        "terminal.opened",
        {"backend": "remote"},
        event_id="terminal-events-opened",
        session_id=terminal.id,
        expected_terminal_status=TerminalStatus.CREATED,
        expected_execution_status=ExecutionStatus.RUNNING,
    )
    assert skipped is None

    first = await events.append_terminal_projection_if_current(
        "run-1",
        "terminal.opened",
        {
            "backend": "remote",
            "session_id": "stale-session",
            "execution_id": "stale-execution",
            "status": "starting",
        },
        event_id="terminal-events-opened",
        session_id=terminal.id,
        expected_terminal_status=TerminalStatus.OPEN,
        expected_execution_status=ExecutionStatus.RUNNING,
    )
    repeated = await events.append_terminal_projection_if_current(
        "run-1",
        "terminal.opened",
        {
            "backend": "remote",
            "session_id": "different-stale-session",
            "execution_id": "different-stale-execution",
            "status": "exited",
        },
        event_id="terminal-events-opened",
        session_id=terminal.id,
        expected_terminal_status=TerminalStatus.OPEN,
        expected_execution_status=ExecutionStatus.RUNNING,
    )
    assert first is not None
    assert repeated == first
    assert first.payload == {
        "backend": "remote",
        "session_id": terminal.id,
        "execution_id": execution.id,
        "status": "running",
    }

    cancelled = running.model_copy(deep=True)
    cancelled.transition_to(ExecutionStatus.CANCELLED)
    cancelled.physical_stop_confirmed_at = datetime(2026, 8, 1, tzinfo=UTC)
    _, saved = await executions.save_if_status(
        cancelled,
        expected={ExecutionStatus.RUNNING},
    )
    assert saved is True
    closed = terminal.model_copy(deep=True)
    closed.transition_to(TerminalStatus.CLOSED)
    _, saved = await terminals.save_if_status(
        closed,
        expected={TerminalStatus.OPEN},
    )
    assert saved is True

    stale_opened = await events.append_terminal_projection_if_current(
        "run-1",
        "terminal.opened",
        {"backend": "remote"},
        event_id="another-opened-attempt",
        session_id=terminal.id,
        expected_terminal_status=TerminalStatus.OPEN,
        expected_execution_status=ExecutionStatus.RUNNING,
    )
    assert stale_opened is None
    closed_event = await events.append_terminal_projection_if_current(
        "run-1",
        "terminal.closed",
        {"backend": "remote", "status": "exited"},
        event_id="terminal-events-closed",
        session_id=terminal.id,
        expected_terminal_status=TerminalStatus.CLOSED,
        expected_execution_status=ExecutionStatus.CANCELLED,
    )
    repeated_closed = await events.append_terminal_projection_if_current(
        "run-1",
        "terminal.closed",
        {"backend": "remote", "status": "completed"},
        event_id="terminal-events-closed",
        session_id=terminal.id,
        expected_terminal_status=TerminalStatus.CLOSED,
        expected_execution_status=ExecutionStatus.CANCELLED,
    )
    assert closed_event is not None
    assert repeated_closed == closed_event
    assert closed_event.payload["status"] == "cancelled"
    await database.dispose()


async def test_run_and_events_survive_database_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    database = Database(database_url)
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)

    await engagements.create(Engagement(id="engagement-1", name="Test engagement"))
    await runs.create(make_run())
    await runs.create(make_run(run_id="audit-run-1", kind=RunKind.CODE_AUDIT))
    await runs.update_status("run-1", RunStatus.PREPARING)
    running = await runs.update_status("run-1", RunStatus.RUNNING)
    message_event = await events.append("run-1", "agent.message", {"content": "started"})

    assert running.started_at is not None
    assert message_event.sequence == 4
    await database.dispose()

    reopened = Database(database_url)
    reopened_runs = SQLAlchemyRunRepository(reopened.session_factory)
    reopened_events = SQLAlchemyRunEventRepository(reopened.session_factory)

    persisted = await reopened_runs.get("run-1")
    persisted_audit = await reopened_runs.get("audit-run-1")
    timeline = await reopened_events.list_after("run-1")

    assert persisted is not None
    assert persisted.kind is RunKind.GENERAL
    assert persisted_audit is not None
    assert persisted_audit.kind is RunKind.CODE_AUDIT
    assert persisted.status is RunStatus.RUNNING
    assert persisted.started_at == running.started_at
    assert [event.sequence for event in timeline] == [1, 2, 3, 4]
    assert [event.event_type for event in timeline] == [
        "run.created",
        "run.status_changed",
        "run.status_changed",
        "agent.message",
    ]
    await reopened.dispose()


async def test_status_transition_and_event_are_atomic(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    await runs.create(make_run())

    with pytest.raises(InvalidStateTransitionError):
        await runs.update_status("run-1", RunStatus.COMPLETED)

    persisted = await runs.get("run-1")
    timeline = await events.list_after("run-1")
    assert persisted is not None
    assert persisted.status is RunStatus.CREATED
    assert [event.event_type for event in timeline] == ["run.created"]
    await database.dispose()


async def test_status_update_retries_transient_serialization_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Retry serialization"))
    await runs.create(make_run())
    original_serialized_write = repository_module._serialized_run_write
    attempts = 0

    @asynccontextmanager
    async def transiently_locked(session_factory):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError("BEGIN IMMEDIATE", {}, RuntimeError("database is locked"))
        async with original_serialized_write(session_factory) as session:
            yield session

    monkeypatch.setattr(repository_module, "_serialized_run_write", transiently_locked)

    updated = await runs.update_status("run-1", RunStatus.PREPARING)

    assert attempts == 3
    assert updated.status is RunStatus.PREPARING
    await database.dispose()


async def test_status_update_bounds_persistent_serialization_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Bound serialization"))
    await runs.create(make_run())
    attempts = 0

    @asynccontextmanager
    async def persistently_locked(session_factory):
        nonlocal attempts
        del session_factory
        attempts += 1
        raise OperationalError("BEGIN IMMEDIATE", {}, RuntimeError("database is locked"))
        yield

    monkeypatch.setattr(repository_module, "_serialized_run_write", persistently_locked)

    with pytest.raises(RepositoryConflictError, match="after concurrent retries"):
        await runs.update_status("run-1", RunStatus.PREPARING)

    persisted = await runs.get("run-1")
    assert attempts == 10
    assert persisted is not None and persisted.status is RunStatus.CREATED
    await database.dispose()


async def test_sqlite_status_updates_serialize_a_safety_fence_against_stale_failure(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Safety fence race"))
    fence_wins = 0
    cleanup_wins = 0

    async def update_after_start(
        start: asyncio.Event,
        run_id: str,
        target: RunStatus,
    ) -> Run:
        await start.wait()
        return await runs.update_status(run_id, target)

    for index in range(40):
        run_id = f"run-status-race-{index}"
        await runs.create(make_run(run_id=run_id))
        await runs.update_status(run_id, RunStatus.PREPARING)
        await runs.update_status(run_id, RunStatus.RUNNING)
        start = asyncio.Event()

        if index % 2:
            cleanup_task = asyncio.create_task(update_after_start(start, run_id, RunStatus.FAILED))
            fence_task = asyncio.create_task(
                update_after_start(start, run_id, RunStatus.CANCELLING)
            )
        else:
            fence_task = asyncio.create_task(
                update_after_start(start, run_id, RunStatus.CANCELLING)
            )
            cleanup_task = asyncio.create_task(update_after_start(start, run_id, RunStatus.FAILED))
        await asyncio.sleep(0)
        start.set()
        fence_result, cleanup_result = await asyncio.gather(
            fence_task,
            cleanup_task,
            return_exceptions=True,
        )

        persisted = await runs.get(run_id)
        assert persisted is not None
        if isinstance(fence_result, BaseException):
            cleanup_wins += 1
            assert isinstance(fence_result, InvalidStateTransitionError)
            assert isinstance(cleanup_result, Run)
            assert persisted.status is RunStatus.FAILED
        else:
            fence_wins += 1
            assert isinstance(cleanup_result, InvalidStateTransitionError)
            assert persisted.status is RunStatus.CANCELLING

        status_events = [
            event
            for event in await events.list_after(run_id)
            if event.event_type == "run.status_changed"
        ]
        terminal_attempts = [
            event
            for event in status_events
            if event.payload.get("to") in {RunStatus.CANCELLING.value, RunStatus.FAILED.value}
        ]
        assert len(terminal_attempts) == 1

    assert fence_wins > 0
    assert cleanup_wins > 0
    await database.dispose()


async def test_repository_lists_and_filters_runs(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    await runs.create(make_run(run_id="run-1"))
    await runs.create(make_run(run_id="run-2", kind=RunKind.CODE_AUDIT))
    await runs.update_status("run-2", RunStatus.PREPARING)

    created = await runs.list(status=RunStatus.CREATED)
    preparing = await runs.list(status=RunStatus.PREPARING)
    general = await runs.list(kind=RunKind.GENERAL)
    general_page = await runs.list(kind=RunKind.GENERAL, limit=1)
    code_audit = await runs.list(kind=RunKind.CODE_AUDIT)
    code_audit_preparing = await runs.list(
        status=RunStatus.PREPARING,
        kind=RunKind.CODE_AUDIT,
    )
    code_audit_created = await runs.list(
        status=RunStatus.CREATED,
        kind=RunKind.CODE_AUDIT,
    )

    assert [run.id for run in created] == ["run-1"]
    assert [run.id for run in preparing] == ["run-2"]
    assert [run.id for run in general] == ["run-1"]
    assert [run.id for run in general_page] == ["run-1"]
    assert [run.id for run in code_audit] == ["run-2"]
    assert [run.id for run in code_audit_preparing] == ["run-2"]
    assert code_audit_created == []
    await database.dispose()


async def test_repository_translates_constraint_conflicts(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)

    with pytest.raises(RepositoryConflictError, match="could not create run"):
        await runs.create(make_run(engagement_id="missing"))

    await database.dispose()


async def test_run_reconciliation_keyset_survives_status_changes_between_pages(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    created = datetime(2025, 1, 1, tzinfo=UTC)
    for run_id, created_at in (
        ("run-a", created),
        ("run-b", created),
        ("run-c", created + timedelta(microseconds=1)),
    ):
        await runs.create(make_run(run_id=run_id).model_copy(update={"created_at": created_at}))
        await runs.update_status(run_id, RunStatus.PAUSING)

    first = await runs.list_for_reconciliation(
        status=RunStatus.PAUSING,
        created_through=created + timedelta(seconds=1),
        limit=2,
    )
    assert [run.id for run in first] == ["run-a", "run-b"]

    await runs.update_status("run-a", RunStatus.PAUSED)
    await runs.update_status("run-b", RunStatus.PAUSED)
    second = await runs.list_for_reconciliation(
        status=RunStatus.PAUSING,
        created_through=created + timedelta(seconds=1),
        after_created_at=first[-1].created_at,
        after_id=first[-1].id,
        limit=2,
    )
    assert [run.id for run in second] == ["run-c"]
    await database.dispose()


async def test_event_repository_rejects_unknown_run(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    events = SQLAlchemyRunEventRepository(database.session_factory)

    with pytest.raises(EntityNotFoundError, match="was not found"):
        await events.append("missing", "agent.message", {})

    await database.dispose()


async def test_event_repository_atomically_reuses_caller_selected_event_id(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    await runs.create(make_run())
    message_event_id = str(uuid4())

    first, second = await asyncio.gather(
        events.append(
            "run-1",
            "user.message_queued",
            {"message": "Begin"},
            event_id=message_event_id,
        ),
        events.append(
            "run-1",
            "user.message_queued",
            {"message": "Begin"},
            event_id=message_event_id,
        ),
    )

    assert first.id == second.id == message_event_id
    timeline = await events.list_after("run-1")
    assert [event.id for event in timeline if event.event_type == "user.message_queued"] == [
        message_event_id
    ]
    await database.dispose()


async def test_event_repository_rejects_mismatched_caller_selected_event_id(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    await runs.create(make_run())
    event_id = str(uuid4())

    await events.append(
        "run-1",
        "user.message_queued",
        {"message": "Begin"},
        event_id=event_id,
    )

    with pytest.raises(RepositoryConflictError, match="different event"):
        await events.append(
            "run-1",
            "run.cleaned_up",
            {"status": "completed"},
            event_id=event_id,
        )

    await database.dispose()


@pytest.mark.parametrize(
    "event_id",
    [
        cleanup_event_id("run-1", RunStatus.FAILED),
        report_failure_event_id("run-1"),
    ],
)
async def test_user_message_cannot_reserve_system_lifecycle_event_id(
    tmp_path: Path,
    event_id: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    await runs.create(make_run())

    with pytest.raises(RepositoryConflictError, match="reserved for Run lifecycle events"):
        await events.append_user_message(
            "run-1",
            "Preempt the cleanup audit event",
            event_id=event_id,
        )

    timeline = await events.list_after("run-1")
    assert all(event.event_type != "user.message_queued" for event in timeline)
    await database.dispose()


async def test_user_message_append_and_compat_completion_fence_have_one_atomic_winner(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    message_wins = 0
    completion_wins = 0

    async def append_after_start(
        start: asyncio.Event,
        run_id: str,
        message_event_id: str,
    ) -> object:
        await start.wait()
        return await events.append_user_message(
            run_id,
            "Racing instruction",
            event_id=message_event_id,
        )

    async def complete_after_start(start: asyncio.Event, run_id: str) -> object:
        await start.wait()
        return await runs.complete_if_no_pending_user_messages(
            run_id,
            consumed_user_message_ids=[],
        )

    for index in range(100):
        run_id = f"run-race-{index}"
        await runs.create(make_run(run_id=run_id))
        await runs.update_status(run_id, RunStatus.PREPARING)
        await runs.update_status(run_id, RunStatus.RUNNING)
        message_event_id = str(uuid4())

        start = asyncio.Event()

        if index % 2:
            completion_task = asyncio.create_task(complete_after_start(start, run_id))
            message_task = asyncio.create_task(append_after_start(start, run_id, message_event_id))
        else:
            message_task = asyncio.create_task(append_after_start(start, run_id, message_event_id))
            completion_task = asyncio.create_task(complete_after_start(start, run_id))
        await asyncio.sleep(0)
        start.set()
        message_result, completion_result = await asyncio.gather(
            message_task,
            completion_task,
            return_exceptions=True,
        )

        if isinstance(message_result, BaseException):
            completion_wins += 1
            assert isinstance(message_result, RepositoryConflictError)
            assert not isinstance(completion_result, BaseException)
            fenced, pending = completion_result
            assert fenced.status is RunStatus.COMPLETING
            assert pending == ()
        else:
            message_wins += 1
            assert not isinstance(completion_result, BaseException)
            still_running, pending = completion_result
            assert still_running.status is RunStatus.RUNNING
            assert pending == (message_event_id,)

        persisted = await runs.get(run_id)
        queued = [
            event
            for event in await events.list_after(run_id)
            if event.event_type == "user.message_queued"
        ]
        assert not (persisted is not None and persisted.status is RunStatus.COMPLETING and queued)

    assert message_wins > 0
    assert completion_wins > 0
    await database.dispose()


async def test_completion_fence_wins_a_stale_pause_transition(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)

    fenced = await runs.fence_finalization("run-1", RunStatus.COMPLETED)
    with pytest.raises(InvalidStateTransitionError):
        await runs.update_status("run-1", RunStatus.PAUSING)

    current = await runs.get("run-1")
    intent = await runs.get_finalization_intent("run-1")
    assert fenced.status is RunStatus.COMPLETING
    assert current is not None and current.status is RunStatus.COMPLETING
    assert intent is not None and intent.target is RunStatus.COMPLETED
    await database.dispose()


async def test_pause_fence_rejects_a_late_completed_intent(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PAUSING)
    await runs.update_status("run-1", RunStatus.PAUSED)

    with pytest.raises(RepositoryConflictError, match="pause superseded"):
        await runs.record_finalization_intent("run-1", RunStatus.COMPLETED)

    current = await runs.get("run-1")
    assert current is not None and current.status is RunStatus.PAUSED
    assert await runs.get_finalization_intent("run-1") is None
    await database.dispose()


async def test_completion_fence_stops_at_completing_until_resource_gate_finishes(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'completion-fence.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Completion fence")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)

    fenced, pending = await runs.fence_completion_if_no_pending_user_messages(
        "run-1",
        consumed_user_message_ids=[],
    )

    assert pending == ()
    assert fenced.status is RunStatus.COMPLETING
    intent = await runs.get_finalization_intent("run-1")
    assert intent is not None
    assert intent.target is RunStatus.COMPLETED
    assert intent.defer_cleanup_event is False
    with pytest.raises(RepositoryConflictError):
        await events.append_user_message("run-1", "Too late")
    persisted = await runs.get("run-1")
    assert persisted is not None and persisted.status is RunStatus.COMPLETING
    completed = await runs.commit_finalization("run-1", RunStatus.COMPLETED)
    assert completed.status is RunStatus.COMPLETED
    assert [
        event.payload
        for event in await events.list_after("run-1")
        if event.event_type == "run.cleaned_up"
    ] == [{"version": 1, "status": "completed", "stop_confirmed": True}]
    await database.dispose()


@pytest.mark.parametrize("target", [RunStatus.COMPLETED, RunStatus.FAILED])
async def test_finalization_commit_rolls_back_status_and_event_then_retries_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: RunStatus,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'{target.value}-rollback.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Atomic finalization rollback")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.fence_finalization("run-1", target)

    async def fail_cleanup_event(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected cleanup-event write crash")

    with monkeypatch.context() as patch:
        patch.setattr(runs, "_write_cleanup_event_if_needed", fail_cleanup_event)
        with pytest.raises(RuntimeError, match="injected cleanup-event write crash"):
            await runs.commit_finalization("run-1", target)

    rolled_back = await runs.get("run-1")
    after_crash = await events.list_after("run-1")
    assert rolled_back is not None and rolled_back.status is RunStatus.COMPLETING
    assert not any(event.event_type == "run.cleaned_up" for event in after_crash)
    assert not any(
        event.event_type == "run.status_changed" and event.payload.get("to") == target.value
        for event in after_crash
    )

    finalized = await runs.commit_finalization("run-1", target)
    retried = await runs.commit_finalization("run-1", target)
    timeline = await events.list_after("run-1")
    terminal_changes = [
        event
        for event in timeline
        if event.event_type == "run.status_changed" and event.payload.get("to") == target.value
    ]
    cleaned = [event for event in timeline if event.event_type == "run.cleaned_up"]
    assert finalized.status is target
    assert retried.status is target
    assert len(terminal_changes) == 1
    assert len(cleaned) == 1
    assert cleaned[0].sequence == terminal_changes[0].sequence + 1
    assert cleaned[0].payload == {
        "version": 1,
        "status": target.value,
        "stop_confirmed": True,
    }
    await database.dispose()


async def test_deferred_finalization_commits_status_before_one_later_cleanup_event(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'deferred-finalization.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Deferred cleanup event")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.fence_finalization(
        "run-1",
        RunStatus.COMPLETED,
        defer_cleanup_event=True,
    )

    finalized = await runs.commit_finalization(
        "run-1",
        RunStatus.COMPLETED,
        defer_cleanup_event=True,
    )
    before_report = await events.list_after("run-1")
    assert finalized.status is RunStatus.COMPLETED
    assert not any(event.event_type == "run.cleaned_up" for event in before_report)

    await runs.commit_finalization("run-1", RunStatus.COMPLETED)
    await runs.commit_finalization("run-1", RunStatus.COMPLETED)
    after_report = await events.list_after("run-1")
    cleaned = [event for event in after_report if event.event_type == "run.cleaned_up"]
    assert len(cleaned) == 1
    assert cleaned[0].payload == {
        "version": 1,
        "status": "completed",
        "stop_confirmed": True,
    }
    await database.dispose()


async def test_finalization_intent_is_immutable_and_only_strengthens_event_deferral(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'finalization-intent.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Finalization intent")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.update_status("run-1", RunStatus.PAUSING)

    await runs.record_finalization_intent("run-1", RunStatus.FAILED)
    await runs.record_finalization_intent(
        "run-1",
        RunStatus.FAILED,
        defer_cleanup_event=True,
    )
    await runs.record_finalization_intent("run-1", RunStatus.FAILED)
    intent = await runs.get_finalization_intent("run-1")

    assert intent is not None
    assert intent.target is RunStatus.FAILED
    assert intent.defer_cleanup_event is True
    paused_fence = await runs.get("run-1")
    assert paused_fence is not None and paused_fence.status is RunStatus.PAUSING
    with pytest.raises(InvalidStateTransitionError):
        await runs.fence_finalization("run-1", RunStatus.FAILED)
    await runs.update_status("run-1", RunStatus.PAUSED)
    fenced = await runs.fence_finalization("run-1", RunStatus.FAILED)
    assert fenced.status is RunStatus.COMPLETING
    with pytest.raises(RepositoryConflictError, match="already finalizing as 'failed'"):
        await runs.fence_finalization("run-1", RunStatus.COMPLETED)
    persisted = await runs.get("run-1")
    assert persisted is not None and persisted.status is RunStatus.COMPLETING
    await database.dispose()


@pytest.mark.parametrize(
    ("intent_target", "conflicting_target"),
    [
        (RunStatus.COMPLETED, RunStatus.FAILED),
        (RunStatus.FAILED, RunStatus.COMPLETED),
    ],
)
async def test_direct_terminal_transition_rejects_conflicting_finalization_intent(
    tmp_path: Path,
    intent_target: RunStatus,
    conflicting_target: RunStatus,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'terminal-intent-conflict.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Terminal intent conflict")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    fenced = await runs.fence_finalization("run-1", intent_target)

    with pytest.raises(
        RepositoryConflictError,
        match=f"already finalizing as '{intent_target.value}'",
    ):
        await runs.update_status("run-1", conflicting_target)

    persisted = await runs.get("run-1")
    intent = await runs.get_finalization_intent("run-1")
    assert fenced.status is RunStatus.COMPLETING
    assert persisted is not None and persisted.status is RunStatus.COMPLETING
    assert intent is not None and intent.target is intent_target
    await database.dispose()


async def test_stale_pause_observation_records_failure_as_atomic_running_fence(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'stale-pause-intent.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Stale pause observation")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.update_status("run-1", RunStatus.PAUSING)
    observed = await runs.get("run-1")
    assert observed is not None and observed.status is RunStatus.PAUSING

    # Resume wins after the Activity observed PAUSING but before it obtains the
    # repository lock used to preserve the failure intent.
    await runs.update_status("run-1", RunStatus.PAUSED)
    await runs.update_status("run-1", RunStatus.RUNNING)
    fenced = await runs.record_finalization_intent("run-1", RunStatus.FAILED)

    assert fenced.status is RunStatus.COMPLETING
    intent = await runs.get_finalization_intent("run-1")
    assert intent is not None and intent.target is RunStatus.FAILED
    await database.dispose()


async def test_pending_message_prevents_both_completion_fence_and_intent(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pending-intent.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Pending completion intent")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    message = await events.append_user_message("run-1", "One more instruction")

    open_run, pending = await runs.fence_completion_if_no_pending_user_messages(
        "run-1",
        consumed_user_message_ids=[],
        defer_cleanup_event=True,
    )

    assert open_run.status is RunStatus.RUNNING
    assert pending == (message.id,)
    assert await runs.get_finalization_intent("run-1") is None
    await database.dispose()


@pytest.mark.parametrize(
    "intent_payloads",
    [
        [{"version": 99, "target_status": "failed", "defer_cleanup_event": False}],
        [
            {"version": 1, "target_status": "completed", "defer_cleanup_event": False},
            {"version": 1, "target_status": "failed", "defer_cleanup_event": False},
        ],
    ],
)
async def test_invalid_or_conflicting_finalization_intent_fails_closed(
    tmp_path: Path,
    intent_payloads: list[dict[str, object]],
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'invalid-intent.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Invalid finalization intent")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.update_status("run-1", RunStatus.COMPLETING)
    for payload in intent_payloads:
        await events.append("run-1", "run.finalization_intent", payload)

    with pytest.raises(RepositoryConflictError, match="invalid finalization intent"):
        await runs.get_finalization_intent("run-1")

    persisted = await runs.get("run-1")
    assert persisted is not None and persisted.status is RunStatus.COMPLETING
    await database.dispose()


@pytest.mark.parametrize(
    "fence_status",
    [RunStatus.PAUSING, RunStatus.CANCELLING, RunStatus.COMPLETING],
)
async def test_user_message_admission_rejects_every_safety_fence(
    tmp_path: Path,
    fence_status: RunStatus,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'message-{fence_status.value}.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Message admission fence")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.update_status("run-1", fence_status)

    with pytest.raises(RepositoryConflictError, match=fence_status.value):
        await events.append_user_message("run-1", "Must wait for the fence")

    timeline = await events.list_after("run-1")
    assert not any(event.event_type == "user.message_queued" for event in timeline)
    await database.dispose()


async def test_paused_run_can_queue_message_for_a_later_resume(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'paused-message.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Paused message")
    )
    await runs.create(make_run())
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.update_status("run-1", RunStatus.PAUSING)
    await runs.update_status("run-1", RunStatus.PAUSED)

    event = await events.append_user_message("run-1", "Continue after resume")

    assert event.event_type == "user.message_queued"
    assert event.payload == {"message": "Continue after resume"}
    await database.dispose()
