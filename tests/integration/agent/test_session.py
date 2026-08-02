from pathlib import Path

import pytest
from agents.memory.session import Session

from riftx.agent import RiftXDatabaseSession
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
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test"),
            workspace_path=str(path.parent),
        )
    )
    return database


async def test_database_session_persists_ordered_sdk_items(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    database = await _database_with_run(database_path)
    sdk_session = RiftXDatabaseSession(
        "run-1",
        database.session_factory,
        max_history_items=2,
    )

    assert isinstance(sdk_session, Session)
    assert sdk_session.session_settings.limit == 2
    await sdk_session.add_items(
        [
            {"role": "user", "content": "你好"},
            {
                "type": "function_call",
                "name": "scan",
                "arguments": "{}",
                "call_id": "call-1",
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "ok",
            },
        ]
    )

    assert await sdk_session.get_items(limit=2) == [
        {
            "type": "function_call",
            "name": "scan",
            "arguments": "{}",
            "call_id": "call-1",
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "ok",
        },
    ]
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    restored_session = RiftXDatabaseSession("run-1", reopened.session_factory)
    assert await restored_session.get_items() == [
        {"role": "user", "content": "你好"},
        {
            "type": "function_call",
            "name": "scan",
            "arguments": "{}",
            "call_id": "call-1",
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "ok",
        },
    ]
    await reopened.dispose()


async def test_database_session_pop_and_clear(tmp_path: Path) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    sdk_session = RiftXDatabaseSession("run-1", database.session_factory)
    await sdk_session.add_items(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
    )

    assert await sdk_session.pop_item() == {"role": "assistant", "content": "second"}
    assert await sdk_session.get_items() == [{"role": "user", "content": "first"}]
    await sdk_session.clear_session()
    assert await sdk_session.get_items() == []
    assert await sdk_session.pop_item() is None
    await database.dispose()


async def test_database_session_rejects_missing_run(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    sdk_session = RiftXDatabaseSession("missing", database.session_factory)

    with pytest.raises(EntityNotFoundError, match="missing"):
        await sdk_session.add_items([{"role": "user", "content": "hello"}])
    await database.dispose()


def test_database_session_validates_limits() -> None:
    with pytest.raises(ValueError, match="positive"):
        RiftXDatabaseSession("run-1", None, max_history_items=0)  # type: ignore[arg-type]
