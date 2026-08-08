from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.integration.persistence._audit_compat import (
    NOW,
    _create_audit,
    _create_engagement,
    _create_project,
    _project,
    _snapshot,
)

from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
from riftx.audit import SnapshotReference, SnapshotReferenceRole
from riftx.domain import AuditVcsKind, SourceTargetKind
from riftx.persistence import (
    Database,
    SQLAlchemySnapshotReferenceRepository,
    SQLAlchemySnapshotRepository,
    SQLAlchemySourceSnapshotSealUnitOfWork,
)
from riftx.persistence.orm import AuditScanRecord


async def _seed(database: Database) -> None:
    await _create_engagement(database, "engagement-1")
    await _create_project(database, _project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    await _create_audit(database)


def _reference(**updates: object) -> SnapshotReference:
    payload: dict[str, object] = {
        "audit_id": "audit-1",
        "snapshot_id": "snapshot-1",
        "project_id": "project-1",
        "role": SnapshotReferenceRole.PRIMARY,
        "created_at": NOW,
    }
    payload.update(updates)
    return SnapshotReference(**payload)  # type: ignore[arg-type]


def _directory_project():
    project = _project()
    return type(project).model_validate(
        {
            **project.model_dump(mode="python"),
            "vcs_kind": AuditVcsKind.DIRECTORY,
            "default_branch": None,
        }
    )


def _directory_snapshot(snapshot_id: str):
    snapshot = _snapshot(snapshot_id)
    return type(snapshot).model_validate(
        {
            **snapshot.model_dump(mode="python"),
            "source_kind": SourceTargetKind.DIRECTORY,
            "commit_sha": None,
        }
    )


async def test_snapshot_reference_add_replay_list_release_and_restart(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'snapshot-reference.db'}"
    database = Database(database_url)
    await database.create_schema()
    await _seed(database)
    references = SQLAlchemySnapshotReferenceRepository(database.session_factory)
    reference = _reference()

    stored, created = await references.add(reference)
    assert (stored, created) == (reference, True)
    replayed, created = await references.add(reference)
    assert (replayed, created) == (reference, False)
    assert await references.list_for_snapshot("snapshot-1", project_id="project-1") == (
        reference,
    )

    with pytest.raises(RepositoryConflictError):
        await references.add(_reference(created_at=NOW + timedelta(seconds=1)))
    await database.dispose()

    reopened = Database(database_url)
    reopened_references = SQLAlchemySnapshotReferenceRepository(reopened.session_factory)
    assert await reopened_references.list_for_snapshot(
        "snapshot-1",
        project_id="project-1",
    ) == (reference,)
    assert await reopened_references.release(
        audit_id="audit-1",
        snapshot_id="snapshot-1",
        role=SnapshotReferenceRole.PRIMARY,
    ) is True
    assert await reopened_references.release(
        audit_id="audit-1",
        snapshot_id="snapshot-1",
        role=SnapshotReferenceRole.PRIMARY,
    ) is False
    await reopened.dispose()


async def test_snapshot_reference_owner_fks_and_concurrent_replay_fail_closed(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'snapshot-owner.db'}")
    await database.create_schema()
    await _seed(database)
    snapshots = SQLAlchemySnapshotRepository(database.session_factory)
    await _create_project(database, _project("project-2"))
    await snapshots.create(_snapshot("snapshot-2", project_id="project-2"))
    references = SQLAlchemySnapshotReferenceRepository(database.session_factory)

    with pytest.raises(RepositoryConflictError):
        await references.add(
            _reference(snapshot_id="snapshot-2", project_id="project-1")
        )
    with pytest.raises(RepositoryConflictError):
        await references.add(_reference(project_id="project-2"))

    outcomes = await asyncio.gather(
        references.add(_reference()),
        references.add(_reference()),
    )
    assert sorted(created for _stored, created in outcomes) == [False, True]
    await database.dispose()


async def test_snapshot_reference_corrupt_digest_is_not_returned(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'snapshot-corrupt.db'}")
    await database.create_schema()
    await _seed(database)
    references = SQLAlchemySnapshotReferenceRepository(database.session_factory)
    await references.add(_reference())
    async with database.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE snapshot_references SET reference_digest = :digest "
                "WHERE audit_id = :audit_id"
            ),
            {"digest": "0" * 64, "audit_id": "audit-1"},
        )

    with pytest.raises(RepositoryIntegrityError) as captured:
        await references.list_for_snapshot("snapshot-1", project_id="project-1")
    assert captured.value.entity == "SnapshotReference"
    assert "snapshot-1" not in str(captured.value.__cause__)
    await database.dispose()


async def test_source_snapshot_seal_is_atomic_replayable_concurrent_and_restart_safe(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'snapshot-seal.db'}"
    database = Database(database_url)
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await _create_project(database, _directory_project())
    await _create_audit(database, snapshot_id=None)
    snapshot = _directory_snapshot("snapshot-local")
    seals = SQLAlchemySourceSnapshotSealUnitOfWork(database.session_factory)

    first, replay = await asyncio.gather(
        seals.seal_primary(audit_id="audit-1", snapshot=snapshot),
        seals.seal_primary(audit_id="audit-1", snapshot=snapshot),
    )
    assert first == replay
    assert first.snapshot == snapshot
    assert first.reference.snapshot_id == snapshot.id
    assert first.audit.value.snapshot_id == snapshot.id
    assert first.audit.state_version == 2
    await database.dispose()

    reopened = Database(database_url)
    async with reopened.session_factory() as session:
        audit = await session.get(AuditScanRecord, "audit-1")
    references = SQLAlchemySnapshotReferenceRepository(reopened.session_factory)
    assert audit is not None
    assert audit.snapshot_id == first.audit.value.snapshot_id
    assert audit.state_version == first.audit.state_version
    assert await references.list_for_snapshot(
        snapshot.id,
        project_id="project-1",
    ) == (first.reference,)
    assert await SQLAlchemySourceSnapshotSealUnitOfWork(
        reopened.session_factory
    ).seal_primary(audit_id="audit-1", snapshot=snapshot) == first
    await reopened.dispose()


async def test_source_snapshot_seal_rolls_back_snapshot_and_reference_on_binding_conflict(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'snapshot-seal-rollback.db'}")
    await database.create_schema()
    await _seed(database)
    conflicting = _snapshot("snapshot-conflicting")
    seals = SQLAlchemySourceSnapshotSealUnitOfWork(database.session_factory)

    with pytest.raises(RepositoryConflictError):
        await seals.seal_primary(audit_id="audit-1", snapshot=conflicting)

    assert await SQLAlchemySnapshotRepository(database.session_factory).get(
        "project-1",
        conflicting.id,
    ) is None
    assert await SQLAlchemySnapshotReferenceRepository(
        database.session_factory
    ).list_for_snapshot(conflicting.id, project_id="project-1") == ()
    await database.dispose()
