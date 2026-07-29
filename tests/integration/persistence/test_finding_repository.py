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


async def _database_with_run(path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Test")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
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
    repository = SQLAlchemyFindingRepository(database.session_factory)
    high = Finding(
        id="finding-high",
        run_id="run-1",
        title="High finding",
        severity=FindingSeverity.HIGH,
        status=FindingStatus.CONFIRMED,
        evidence=[FindingEvidence(description="evidence")],
    )
    low = Finding(
        id="finding-low",
        run_id="run-1",
        title="Low finding",
        severity=FindingSeverity.LOW,
    )

    await repository.create(high)
    await repository.create(low)

    assert await repository.get(high.id) == high
    assert list(await repository.list("run-1", severity=FindingSeverity.HIGH)) == [high]
    assert list(await repository.list("run-1", status=FindingStatus.DRAFT)) == [low]

    updated = high.model_copy(
        update={
            "title": "Updated high finding",
            "status": FindingStatus.RESOLVED,
        }
    )
    await repository.save(updated)
    assert await repository.get(high.id) == updated
    await database.dispose()


async def test_finding_repository_enforces_run_and_id_constraints(tmp_path: Path) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    repository = SQLAlchemyFindingRepository(database.session_factory)
    finding = Finding(
        id="finding-1",
        run_id="run-1",
        title="Finding",
        severity=FindingSeverity.INFO,
    )
    await repository.create(finding)

    with pytest.raises(RepositoryConflictError):
        await repository.create(finding)
    with pytest.raises(RepositoryConflictError):
        await repository.create(finding.model_copy(update={"id": "other", "run_id": "missing"}))
    with pytest.raises(RepositoryConflictError):
        await repository.save(finding.model_copy(update={"run_id": "missing"}))
    await database.dispose()
