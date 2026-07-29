from pathlib import Path

import pytest

from riftx.agent import SQLAlchemyCheckpointStore
from riftx.application.errors import EntityNotFoundError
from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
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
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test"),
            workspace_path=str(path.parent),
        )
    )
    return database


async def test_checkpoint_store_saves_and_resolves_sdk_state(tmp_path: Path) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    store = SQLAlchemyCheckpointStore(database.session_factory)
    state = {"$schemaVersion": "1.0", "current_turn": 2, "items": [{"type": "message"}]}

    saved = await store.save("run-1", state)
    loaded = await store.get(saved.id)
    resolved = await store.resolve(saved.id, "approved")

    assert loaded is not None
    assert loaded.sdk_state == state
    assert loaded.status == "pending"
    assert resolved.status == "approved"
    assert resolved.resolved_at is not None
    await database.dispose()


async def test_checkpoint_store_validates_foreign_keys_and_status(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    store = SQLAlchemyCheckpointStore(database.session_factory)

    with pytest.raises(EntityNotFoundError, match="missing"):
        await store.save("missing", {})
    with pytest.raises(EntityNotFoundError, match="checkpoint"):
        await store.resolve("checkpoint", "approved")
    with pytest.raises(ValueError, match="cannot be pending"):
        await store.resolve("checkpoint", "pending")
    await database.dispose()
