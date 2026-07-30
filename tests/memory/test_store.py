from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from riftx.domain.base import utc_now
from riftx.memory import MemoryRecord, MemoryScopeType, MemoryStatus, MemoryType
from riftx.persistence import Database
from riftx.persistence.memory_repositories import SQLAlchemyMemoryRepository


def memory(memory_id: str, *, scope_id: str = "engagement-1", **changes: object) -> MemoryRecord:
    values: dict[str, object] = {
        "id": memory_id,
        "memory_type": MemoryType.SEMANTIC,
        "scope_type": MemoryScopeType.ENGAGEMENT,
        "scope_id": scope_id,
        "title": "Staging proxy",
        "content": "The engagement uses SOCKS5 on 127.0.0.1:1080.",
        "summary": "Engagement SOCKS5 proxy",
        "retrieval_keywords": ["proxy", "socks5"],
        "confidence": 0.95,
        "importance": 0.8,
        "source_refs": ["user://messages/message-1"],
    }
    values.update(changes)
    return MemoryRecord.model_validate(values)


def test_memory_rejects_missing_sources_and_invalid_ttl() -> None:
    with pytest.raises(ValidationError, match="source reference"):
        memory("memory-no-source", source_refs=[])
    now = utc_now()
    with pytest.raises(ValidationError, match="valid_from"):
        memory(
            "memory-invalid-ttl",
            valid_from=now,
            valid_until=now - timedelta(seconds=1),
        )


async def test_memory_store_round_trip_edit_pin_delete_and_supersede(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}")
    await database.create_schema()
    repository = SQLAlchemyMemoryRepository(database.session_factory)
    original = memory("memory-1")

    assert await repository.create(original) == original
    loaded = await repository.get(original.id)
    assert loaded == original

    assert loaded is not None
    loaded.content = "The engagement uses SOCKS5 on 127.0.0.1:2080."
    loaded.summary = "Updated engagement SOCKS5 proxy"
    loaded.pinned = True
    await repository.save(loaded)
    edited = await repository.get(original.id)
    assert edited is not None
    assert edited.pinned is True
    assert edited.content.endswith("2080.")

    replacement = memory(
        "memory-2",
        content="The engagement now uses SOCKS5 on 127.0.0.1:3080.",
        summary="Replacement engagement proxy",
        supersedes=original.id,
        source_refs=["artifact://runs/run-1/executions/execution-1/stdout"],
    )
    await repository.supersede(replacement)
    superseded = await repository.get(original.id)
    assert superseded is not None and superseded.status is MemoryStatus.SUPERSEDED
    assert await repository.get(replacement.id) == replacement

    replacement.status = MemoryStatus.DELETED
    await repository.save(replacement)
    deleted = await repository.get(replacement.id)
    assert deleted is not None and deleted.status is MemoryStatus.DELETED
    assert len(await repository.list_all()) == 2
    await database.dispose()
