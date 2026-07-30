from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from riftx.context import ContextCompiler
from riftx.domain.base import utc_now
from riftx.memory import (
    CreateMemory,
    MemoryRecord,
    MemoryRetrievalScope,
    MemoryScopeType,
    MemoryService,
    MemoryStatus,
    MemoryType,
)
from riftx.memory.context_source import RetrievedMemoryContextSource
from riftx.persistence import Database
from riftx.persistence.memory_repositories import SQLAlchemyMemoryRepository
from riftx.runtime.lifecycle import ContextCompileRequest


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


async def test_scope_ttl_supersede_pin_and_keyword_retrieval(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'retrieval.db'}")
    await database.create_schema()
    repository = SQLAlchemyMemoryRepository(database.session_factory)
    service = MemoryService(repository)
    now = utc_now()
    current = memory("current", retrieval_keywords=["nginx", "tls"])
    other_engagement = memory("other", scope_id="engagement-2")
    expired = memory("expired", valid_until=now - timedelta(seconds=1))
    pinned = memory(
        "pinned",
        memory_type=MemoryType.INSTRUCTION,
        title="Reporting rule",
        content="Always retain exact command arguments.",
        summary="Retain exact commands",
        retrieval_keywords=["report"],
        pinned=True,
    )
    await repository.create(current)
    await repository.create(other_engagement)
    await repository.create(expired)
    await repository.create(pinned)
    replacement = memory(
        "replacement",
        supersedes=current.id,
        title="Updated TLS service",
        content="The staging endpoint runs nginx 1.24.",
        summary="nginx 1.24 on staging",
        retrieval_keywords=["nginx", "tls"],
    )
    await repository.supersede(replacement)
    scope = MemoryRetrievalScope(engagement_id="engagement-1")

    retrieved = await service.retrieve("nginx tls", scope=scope, at=now)

    assert [item.id for item in retrieved] == ["pinned", "replacement"]
    assert "other" not in {item.id for item in retrieved}
    assert "expired" not in {item.id for item in retrieved}
    assert "current" not in {item.id for item in retrieved}
    await service.delete("replacement")
    assert [item.id for item in await service.retrieve("nginx", scope=scope)] == [
        "pinned"
    ]
    await database.dispose()


class _FailingMemoryRepository:
    async def list_all(self) -> list[MemoryRecord]:
        raise RuntimeError("retrieval backend unavailable")


async def test_retrieval_failure_degrades_to_empty_context() -> None:
    service = MemoryService(_FailingMemoryRepository())  # type: ignore[arg-type]
    assert await service.retrieve(
        "anything",
        scope=MemoryRetrievalScope(run_id="run-1"),
    ) == []
    compiler = ContextCompiler(sources=[RetrievedMemoryContextSource(service)])

    compiled = await compiler.compile(
        ContextCompileRequest(
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
            model_profile="model-a",
            objective="Continue without Memory",
        )
    )

    assert compiled.loaded_memory_ids == []


async def test_retrieved_memory_is_loaded_and_manifested(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'context-memory.db'}")
    await database.create_schema()
    service = MemoryService(SQLAlchemyMemoryRepository(database.session_factory))
    created = await service.create(
        CreateMemory(
            memory_type=MemoryType.PROCEDURAL,
            scope_type=MemoryScopeType.NODE,
            scope_id="node-1",
            title="Nuclei rate limit",
            content="Use a low Nuclei rate limit behind the staging WAF.",
            summary="Lower Nuclei rate behind WAF",
            source_refs=["user://messages/message-2"],
            retrieval_keywords=["nuclei", "waf"],
        )
    )
    compiler = ContextCompiler(sources=[RetrievedMemoryContextSource(service)])

    compiled = await compiler.compile(
        ContextCompileRequest(
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
            model_profile="model-a",
            objective="Scan staging",
            input_text="Run nuclei behind the WAF",
            run_contract={"node_id": "node-1", "engagement_id": "engagement-1"},
        )
    )

    assert compiled.loaded_memory_ids == [created.id]
    assert any(
        item.get("content", {}).get("memory_id") == created.id
        for item in compiled.input_items
    )
    await database.dispose()
