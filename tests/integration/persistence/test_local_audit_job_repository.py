from __future__ import annotations

from pathlib import Path

from riftx.audit import (
    LocalAuditFailure,
    LocalAuditFinding,
    LocalAuditJobResult,
    LocalAuditJobService,
    LocalAuditJobStatus,
)
from riftx.persistence import Database, SQLAlchemyLocalAuditJobRepository


async def test_historical_local_audit_result_survives_restart(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    repository = SQLAlchemyLocalAuditJobRepository(database.session_factory)
    job = await repository.create(
        audit_id="audit-local-complete",
        source_path=str(tmp_path),
        include_paths=(),
        exclude_paths=(),
    )
    await repository.enqueue(job.id)
    claimed, acquired = await repository.claim(job.id)
    assert acquired and claimed.status is LocalAuditJobStatus.SCANNING
    result = LocalAuditJobResult(
        source_identity_digest="1" * 64,
        snapshot_digest="2" * 64,
        manifest_digest="3" * 64,
        inventory_digest="4" * 64,
        detector_run_digest="5" * 64,
        report_digest="6" * 64,
        total_files=1,
        scanned_files=1,
        findings=(
            LocalAuditFinding(
                id="finding-1",
                rule_id="python.eval",
                rule_version="1.0.0",
                title="Dynamic evaluation",
                severity="high",
                confidence=0.9,
                relative_path="app.py",
                blob_digest="7" * 64,
                line=1,
                column=1,
                end_line=None,
                end_column=None,
                evidence_excerpt="eval(...)\n",
            ),
        ),
        json_report="{}\n",
        markdown_report="# Audit\n",
    )
    completed = await repository.complete_or_cancel(job.id, result)
    await database.dispose()

    reopened = Database(database_url)
    await reopened.create_schema()
    service = LocalAuditJobService(
        SQLAlchemyLocalAuditJobRepository(reopened.session_factory)
    )
    try:
        assert await service.status(job.id) == completed
        assert service.runnable is False
    finally:
        await service.close()
        await reopened.dispose()


async def test_historical_local_audit_cancel_and_recovery_remain_available(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyLocalAuditJobRepository(database.session_factory)
    service = LocalAuditJobService(repository)
    draft = await repository.create(
        audit_id="audit-local-cancel",
        source_path=str(tmp_path),
        include_paths=(),
        exclude_paths=(),
    )
    cancelled = await service.cancel(draft.id)
    interrupted = await repository.create(
        audit_id="audit-local-interrupted",
        source_path=str(tmp_path),
        include_paths=(),
        exclude_paths=(),
    )
    await repository.enqueue(interrupted.id)
    claimed, acquired = await repository.claim(interrupted.id)

    assert cancelled.status is LocalAuditJobStatus.CANCELLED
    assert acquired and claimed.status is LocalAuditJobStatus.SCANNING
    assert await service.recover() == (1, 0)
    recovered = await service.status(interrupted.id)
    assert recovered is not None
    assert recovered.status is LocalAuditJobStatus.FAILED
    assert recovered.failure_code is LocalAuditFailure.INTERRUPTED
    await database.dispose()
