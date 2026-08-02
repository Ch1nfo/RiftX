from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.domain import Artifact, Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
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
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test"),
            workspace_path=str(path.parent),
        )
    )
    return database


def _artifact(artifact_id: str, *, execution_id: str | None = None) -> Artifact:
    return Artifact(
        id=artifact_id,
        run_id="run-1",
        execution_id=execution_id,
        name=f"{artifact_id}.txt",
        path=f"/tmp/{artifact_id}.txt",
        mime_type="text/plain",
        sha256=("a" if artifact_id == "artifact-1" else "b") * 64,
        size=4,
    )


async def test_artifact_repository_create_get_filter_and_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    database = await _database_with_run(database_path)
    repository = SQLAlchemyArtifactRepository(database.session_factory)
    first = _artifact("artifact-1")
    second = _artifact("artifact-2")

    await repository.create(first)
    await repository.create(second)
    await database.dispose()

    restarted = Database(f"sqlite+aiosqlite:///{database_path}")
    repository = SQLAlchemyArtifactRepository(restarted.session_factory)
    assert await repository.get(first.id) == first
    assert list(await repository.list("run-1", limit=1, offset=1)) == [second]
    await restarted.dispose()


async def test_artifact_repository_enforces_run_and_id_constraints(tmp_path: Path) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    repository = SQLAlchemyArtifactRepository(database.session_factory)
    artifact = _artifact("artifact-1")
    await repository.create(artifact)

    with pytest.raises(RepositoryConflictError):
        await repository.create(artifact)
    with pytest.raises(RepositoryConflictError):
        await repository.create(artifact.model_copy(update={"id": "other", "run_id": "missing"}))
    await database.dispose()
