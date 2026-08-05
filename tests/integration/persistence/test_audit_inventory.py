from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.integration.persistence.test_audit_repositories import (
    NOW,
    _create_audit,
    _create_engagement,
    _digest,
    _project,
    _replace,
    _snapshot,
)

from riftx.application.errors import RepositoryConflictError
from riftx.audit import (
    SourceCaptureDecision,
    SourceCaptureReason,
    SourceClassification,
    SourceManifest,
    SourceManifestEntry,
    SourceManifestObjectType,
    SourceManifestOrigin,
    SourceManifestPath,
    SourceManifestSourceKind,
    build_file_inventory,
    build_file_scope_units,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAuditProjectRepository,
    SQLAlchemyAuditScopeRepository,
    SQLAlchemySnapshotRepository,
)


def _entry(relative_path: str, content: bytes) -> SourceManifestEntry:
    return SourceManifestEntry(
        path=SourceManifestPath.from_bytes(relative_path.encode("utf-8")),
        object_type=SourceManifestObjectType.REGULAR_FILE,
        origin=SourceManifestOrigin.LOCAL_DIRECTORY,
        mode=0o100644,
        size=len(content),
        sha256=_digest(content.decode("utf-8")),
        git_blob_id=None,
        language="python",
        classification=SourceClassification.SOURCE,
        decision=SourceCaptureDecision.INCLUDED,
        reason=SourceCaptureReason.INCLUDED,
    )


def _scopes():
    manifest = SourceManifest.create(
        source_kind=SourceManifestSourceKind.DIRECTORY,
        commit_sha=None,
        head_commit_sha=None,
        capture_policy_digest=_digest("inventory-policy"),
        entries=(
            _entry("src/first.py", b"first = 1\n"),
            _entry("src/second.py", b"second = 2\n"),
        ),
    )
    return build_file_scope_units(
        build_file_inventory(manifest),
        audit_id="audit-1",
        snapshot_id="snapshot-1",
        created_at=NOW,
    )


async def _seed(database: Database) -> None:
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    await _create_audit(database)


async def test_inventory_scope_batch_is_atomic_idempotent_and_restart_safe(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'inventory-scopes.db'}"
    database = Database(database_url)
    await database.create_schema()
    await _seed(database)
    repository = SQLAlchemyAuditScopeRepository(database.session_factory)
    scopes = _scopes()

    stored, created_count = await repository.create_many(scopes)
    replayed, replay_created_count = await repository.create_many(scopes)

    assert created_count == len(scopes)
    assert replay_created_count == 0
    assert replayed == stored
    assert tuple(item.value for item in stored) == scopes
    await database.dispose()

    reopened = Database(database_url)
    reopened_repository = SQLAlchemyAuditScopeRepository(reopened.session_factory)
    for expected in scopes:
        persisted = await reopened_repository.get("audit-1", expected.id)
        assert persisted is not None
        assert persisted.value == expected
    await reopened.dispose()


async def test_concurrent_inventory_scope_retries_converge(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'inventory-concurrent.db'}")
    await database.create_schema()
    await _seed(database)
    repository = SQLAlchemyAuditScopeRepository(database.session_factory)
    scopes = _scopes()

    outcomes = await asyncio.gather(
        repository.create_many(scopes),
        repository.create_many(scopes),
    )

    assert sorted(created for _stored, created in outcomes) == [0, len(scopes)]
    assert outcomes[0][0] == outcomes[1][0]
    await database.dispose()


async def test_inventory_scope_batch_conflict_rolls_back_all_new_rows(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'inventory-rollback.db'}")
    await database.create_schema()
    await _seed(database)
    repository = SQLAlchemyAuditScopeRepository(database.session_factory)
    scopes = _scopes()
    await repository.create(scopes[1])
    conflicting = _replace(scopes[1], required_analyses=("different_rules",))

    with pytest.raises(RepositoryConflictError):
        await repository.create_many((scopes[0], conflicting))

    assert await repository.get("audit-1", scopes[0].id) is None
    persisted = await repository.get("audit-1", scopes[1].id)
    assert persisted is not None
    assert persisted.value == scopes[1]
    await database.dispose()


async def test_inventory_scope_batch_rejects_unbound_snapshot_without_writes(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'inventory-binding.db'}")
    await database.create_schema()
    await _seed(database)
    await SQLAlchemySnapshotRepository(database.session_factory).create(
        _snapshot("snapshot-2")
    )
    repository = SQLAlchemyAuditScopeRepository(database.session_factory)
    scopes = tuple(
        _replace(scope, snapshot_id="snapshot-2") for scope in _scopes()
    )

    with pytest.raises(RepositoryConflictError):
        await repository.create_many(scopes)

    assert await repository.list("audit-1") == []
    await database.dispose()
