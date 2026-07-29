from pathlib import Path

import pytest

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain import (
    Engagement,
    InvalidStateTransitionError,
    Objective,
    Run,
    RunStatus,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)


def make_run(*, run_id: str = "run-1", engagement_id: str = "engagement-1") -> Run:
    return Run(
        id=run_id,
        engagement_id=engagement_id,
        node_id="local-node",
        objective=Objective(description="Verify persistence"),
        workspace_path=f"/tmp/riftx/{run_id}",
    )


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
    timeline = await reopened_events.list_after("run-1")

    assert persisted is not None
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


async def test_repository_lists_and_filters_runs(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Test"))
    await runs.create(make_run(run_id="run-1"))
    await runs.create(make_run(run_id="run-2"))
    await runs.update_status("run-2", RunStatus.PREPARING)

    created = await runs.list(status=RunStatus.CREATED)
    preparing = await runs.list(status=RunStatus.PREPARING)

    assert [run.id for run in created] == ["run-1"]
    assert [run.id for run in preparing] == ["run-2"]
    await database.dispose()


async def test_repository_translates_constraint_conflicts(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)

    with pytest.raises(RepositoryConflictError, match="could not create run"):
        await runs.create(make_run(engagement_id="missing"))

    await database.dispose()


async def test_event_repository_rejects_unknown_run(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    events = SQLAlchemyRunEventRepository(database.session_factory)

    with pytest.raises(EntityNotFoundError, match="was not found"):
        await events.append("missing", "agent.message", {})

    await database.dispose()
