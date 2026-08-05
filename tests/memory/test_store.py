from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import riftx.persistence.memory_repositories as memory_repository_module
from riftx.application.errors import ApplicationConflictError
from riftx.context import ContextCompiler
from riftx.domain import Engagement, Objective, Run, RunKind
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
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
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


async def test_code_audit_run_memory_mutations_fail_before_persistence(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-memory-fence.db'}")
    await database.create_schema()
    repository = SQLAlchemyMemoryRepository(database.session_factory)
    audit_run = Run(
        kind=RunKind.CODE_AUDIT,
        id="audit-run",
        engagement_id="engagement-1",
        node_id="local",
        objective=Objective(description="Audit Memory fence"),
        workspace_path=str(tmp_path / "audit-output"),
    )
    general_run = audit_run.model_copy(
        update={"id": "general-run", "kind": RunKind.GENERAL}
    )

    class _Runs:
        async def get(self, run_id: str) -> Run | None:
            return {audit_run.id: audit_run, general_run.id: general_run}.get(run_id)

    service = MemoryService(repository, run_repository=_Runs())  # type: ignore[arg-type]
    audit_memory = memory(
        "audit-memory",
        scope_type=MemoryScopeType.RUN,
        scope_id=audit_run.id,
    )
    await repository.create(audit_memory)
    general = await service.create(
        CreateMemory(
            memory_type=MemoryType.SEMANTIC,
            scope_type=MemoryScopeType.RUN,
            scope_id=general_run.id,
            title="General memory",
            content="Allowed only for a general Run.",
            summary="General Run memory",
            source_refs=["user://general"],
        )
    )
    baseline = await repository.list_all()

    operations = (
        service.create(
            CreateMemory(
                memory_type=MemoryType.SEMANTIC,
                scope_type=MemoryScopeType.RUN,
                scope_id=audit_run.id,
                title="Forged audit memory",
                content="Must not persist.",
                summary="Must not persist",
                source_refs=["user://forged"],
            )
        ),
        service.update(audit_memory.id, {"scope_id": general_run.id}),
        service.update(general.id, {"scope_id": audit_run.id}),
        service.delete(audit_memory.id),
        service.pin(audit_memory.id, pinned=True),
        service.create(
            CreateMemory(
                memory_type=MemoryType.SEMANTIC,
                scope_type=MemoryScopeType.RUN,
                scope_id=general_run.id,
                title="Forbidden supersede",
                content="Must not supersede Audit Memory.",
                summary="Forbidden supersede",
                source_refs=["user://forged"],
                supersedes=audit_memory.id,
            )
        ),
    )
    for operation in operations:
        with pytest.raises(ApplicationConflictError) as captured:
            await operation
        assert captured.value.code == "run_kind_operation_unsupported"

    assert await repository.list_all() == baseline
    await database.dispose()


async def test_default_memory_queries_filter_audit_and_orphan_run_scope_in_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-memory-read-scope.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-memory", name="Memory read scope")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    general_run = Run(
        kind=RunKind.GENERAL,
        id="general-memory-run",
        engagement_id="engagement-memory",
        node_id="local",
        objective=Objective(description="General Memory visibility"),
        workspace_path=str(tmp_path / "general"),
    )
    audit_run = general_run.model_copy(
        update={"id": "audit-memory-run", "kind": RunKind.CODE_AUDIT}
    )
    await runs.create(general_run)
    await runs.create(audit_run)

    repository = SQLAlchemyMemoryRepository(database.session_factory)
    records = (
        memory(
            "general-run-memory",
            scope_type=MemoryScopeType.RUN,
            scope_id=general_run.id,
        ),
        memory(
            "audit-run-memory",
            scope_type=MemoryScopeType.RUN,
            scope_id=audit_run.id,
            content="RIFTX_AUDIT_MEMORY_CONTENT_CANARY",
        ),
        memory(
            "orphan-run-memory",
            scope_type=MemoryScopeType.RUN,
            scope_id="missing-run",
            content="RIFTX_ORPHAN_MEMORY_CONTENT_CANARY",
        ),
        memory(
            "engagement-memory",
            scope_type=MemoryScopeType.ENGAGEMENT,
            scope_id="engagement-memory",
        ),
    )
    for record in records:
        await repository.create(record)

    hydrated_ids: list[str] = []
    original_from_record = memory_repository_module._from_record

    def recording_from_record(record):
        hydrated_ids.append(record.id)
        return original_from_record(record)

    monkeypatch.setattr(memory_repository_module, "_from_record", recording_from_record)
    visible = await repository.list_all()

    assert {item.id for item in visible} == {
        "general-run-memory",
        "engagement-memory",
    }
    assert set(hydrated_ids) == {"general-run-memory", "engagement-memory"}

    hydrated_ids.clear()
    explicit = await repository.list_scope(MemoryScopeType.RUN, audit_run.id)
    retrieved = await repository.list_for_retrieval_scope(
        MemoryRetrievalScope(run_id=audit_run.id)
    )
    assert [item.id for item in explicit] == ["audit-run-memory"]
    assert [item.id for item in retrieved] == ["audit-run-memory"]
    assert hydrated_ids == ["audit-run-memory", "audit-run-memory"]
    await database.dispose()
