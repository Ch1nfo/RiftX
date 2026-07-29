from datetime import UTC, datetime
from pathlib import Path

from riftx.domain import Node, NodeStatus
from riftx.persistence import Database, SQLAlchemyNodeRepository


async def test_node_repository_persists_metadata_and_status_filter(tmp_path: Path) -> None:
    database_path = tmp_path / "nodes.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    database = Database(database_url)
    await database.create_schema()
    repository = SQLAlchemyNodeRepository(database.session_factory)
    now = datetime(2026, 7, 29, tzinfo=UTC)

    await repository.create(
        Node(
            id="runner-a",
            name="Kali A",
            platform="linux",
            architecture="x86_64",
            runner_version="2.0.0",
            status=NodeStatus.ONLINE,
            capabilities=["port_scan", "scripting"],
            labels={"zone": "lab"},
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await repository.create(
        Node(
            id="runner-b",
            name="Windows B",
            platform="windows",
            architecture="amd64",
            status=NodeStatus.OFFLINE,
            created_at=now,
            updated_at=now,
        )
    )
    await database.dispose()

    reopened = Database(database_url)
    persisted_repository = SQLAlchemyNodeRepository(reopened.session_factory)
    persisted = await persisted_repository.get("runner-a")
    online = await persisted_repository.list(status=NodeStatus.ONLINE)

    assert persisted is not None
    assert persisted.runner_version == "2.0.0"
    assert persisted.capabilities == ["port_scan", "scripting"]
    assert persisted.labels == {"zone": "lab"}
    assert persisted.last_seen_at == now
    assert [node.id for node in online] == ["runner-a"]
    await reopened.dispose()
