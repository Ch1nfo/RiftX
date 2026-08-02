import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.domain import (
    Engagement,
    Finding,
    FindingEvidence,
    FindingSeverity,
    FindingStatus,
    Objective,
    Run,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyRunRepository,
)


class RecordingClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return next(self._values)


async def _database_with_run(path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Test")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test"),
            workspace_path=str(path.parent),
        )
    )
    return database


async def test_finding_repository_create_get_and_filter(tmp_path: Path) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    tick = datetime(2026, 1, 1, tzinfo=UTC)
    clock = RecordingClock(tick, tick, tick)
    repository = SQLAlchemyFindingRepository(database.session_factory, clock=clock)
    forged = datetime(2099, 1, 1, tzinfo=UTC)
    high = Finding(
        id="finding-high",
        run_id="run-1",
        title="High finding",
        severity=FindingSeverity.HIGH,
        status=FindingStatus.CONFIRMED,
        evidence=[FindingEvidence(description="evidence")],
        created_at=forged,
        updated_at=forged,
    )
    low = Finding(
        id="finding-low",
        run_id="run-1",
        title="Low finding",
        severity=FindingSeverity.LOW,
    )

    high = await repository.create(high)
    low = await repository.create(low)

    assert high.created_at == tick
    assert high.updated_at == tick

    assert await repository.get(high.id) == high
    assert list(await repository.list("run-1", severity=FindingSeverity.HIGH)) == [high]
    assert list(await repository.list("run-1", status=FindingStatus.DRAFT)) == [low]

    updated = high.model_copy(
        update={
            "title": "Updated high finding",
            "status": FindingStatus.RESOLVED,
        }
    )
    persisted, changed = await repository.save(
        updated,
        expected_updated_at=high.updated_at,
    )
    assert changed is True
    assert persisted.updated_at == tick + timedelta(microseconds=1)
    assert await repository.get(high.id) == persisted
    await database.dispose()


async def test_finding_repository_enforces_run_and_id_constraints(tmp_path: Path) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    tick = datetime(2026, 1, 1, tzinfo=UTC)
    clock = RecordingClock(tick)
    repository = SQLAlchemyFindingRepository(database.session_factory, clock=clock)
    finding = Finding(
        id="finding-1",
        run_id="run-1",
        title="Finding",
        severity=FindingSeverity.INFO,
    )
    finding = await repository.create(finding)

    with pytest.raises(RepositoryConflictError):
        await repository.create(finding)
    with pytest.raises(RepositoryConflictError):
        await repository.create(finding.model_copy(update={"id": "other", "run_id": "missing"}))
    with pytest.raises(RepositoryConflictError):
        await repository.save(
            finding.model_copy(update={"run_id": "missing"}),
            expected_updated_at=finding.updated_at,
        )
    with pytest.raises(RepositoryConflictError, match="created_at.*immutable"):
        await repository.save(
            finding.model_copy(update={"created_at": finding.created_at + timedelta(seconds=1)}),
            expected_updated_at=finding.updated_at,
        )
    assert clock.calls == 1
    await database.dispose()


async def test_finding_repository_clock_is_monotonic_and_skips_noops(
    tmp_path: Path,
) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    tick = datetime(2026, 1, 1, tzinfo=UTC)
    clock = RecordingClock(tick, tick, tick - timedelta(days=1))
    repository = SQLAlchemyFindingRepository(database.session_factory, clock=clock)
    created = await repository.create(
        Finding(
            id="finding-1",
            run_id="run-1",
            title="Finding",
            severity=FindingSeverity.INFO,
        )
    )

    unchanged, changed = await repository.save(
        created.model_copy(update={"updated_at": tick + timedelta(days=100)}),
        expected_updated_at=created.updated_at,
    )
    assert changed is False
    assert unchanged == created
    assert clock.calls == 1

    first, changed = await repository.save(
        created.model_copy(update={"title": "First update"}),
        expected_updated_at=created.updated_at,
    )
    assert changed is True
    assert first.updated_at == tick + timedelta(microseconds=1)

    second, changed = await repository.save(
        first.model_copy(update={"title": "Second update"}),
        expected_updated_at=first.updated_at,
    )
    assert changed is True
    assert second.updated_at == tick + timedelta(microseconds=2)
    assert clock.calls == 3
    assert (await repository.get(created.id)) == second
    await database.dispose()


async def test_finding_repository_serializes_stale_writers(tmp_path: Path) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    tick = datetime(2026, 1, 1, tzinfo=UTC)
    clock = RecordingClock(tick, tick)
    repository = SQLAlchemyFindingRepository(database.session_factory, clock=clock)
    original = await repository.create(
        Finding(
            id="finding-1",
            run_id="run-1",
            title="Finding",
            severity=FindingSeverity.INFO,
        )
    )
    first_writer = original.model_copy(update={"title": "First writer"})
    second_writer = original.model_copy(update={"title": "Second writer"})

    results = await asyncio.gather(
        repository.save(first_writer, expected_updated_at=original.updated_at),
        repository.save(second_writer, expected_updated_at=original.updated_at),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, tuple)]
    conflicts = [result for result in results if isinstance(result, RepositoryConflictError)]
    assert len(successes) == 1
    assert successes[0][1] is True
    assert len(conflicts) == 1
    assert clock.calls == 2
    await database.dispose()


async def test_finding_repository_accepts_stale_idempotent_retry(tmp_path: Path) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    tick = datetime(2026, 1, 1, tzinfo=UTC)
    clock = RecordingClock(tick, tick)
    repository = SQLAlchemyFindingRepository(database.session_factory, clock=clock)
    original = await repository.create(
        Finding(
            id="finding-1",
            run_id="run-1",
            title="Finding",
            severity=FindingSeverity.INFO,
        )
    )
    target = original.model_copy(update={"status": FindingStatus.CONFIRMED})
    persisted, changed = await repository.save(
        target,
        expected_updated_at=original.updated_at,
    )
    assert changed is True

    retried, changed = await repository.save(
        target,
        expected_updated_at=original.updated_at,
    )
    assert changed is False
    assert retried == persisted
    assert clock.calls == 2
    await database.dispose()
