from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from threading import Event

from riftx.audit import (
    DetectorInput,
    DetectorMatch,
    DetectorRegistry,
    DetectorRuleMetadata,
    LocalAuditFailure,
    LocalAuditJobService,
    LocalAuditJobStatus,
    LocalAuditWorker,
    LocalAuditWorkerConfig,
)
from riftx.persistence import Database, SQLAlchemyLocalAuditJobRepository


def _source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        parent = Path(directory)
        for name in (*directories, *files):
            path = parent / name
            relative = path.relative_to(root).as_posix().encode()
            value = path.lstat()
            digest.update(relative + b"\0" + str(stat.S_IFMT(value.st_mode)).encode() + b"\0")
            if stat.S_ISLNK(value.st_mode):
                digest.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(value.st_mode):
                digest.update(path.read_bytes())
    return digest.hexdigest()


def _worker(
    repository: SQLAlchemyLocalAuditJobRepository,
    tmp_path: Path,
    *,
    registry: DetectorRegistry | None = None,
) -> LocalAuditWorker:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return LocalAuditWorker(
        repository,
        LocalAuditWorkerConfig(
            allowed_roots=(tmp_path,),
            protected_paths=(state, tmp_path / "riftx.db"),
            staging_root=state / "staging",
            snapshot_root=state / "snapshots",
            max_file_bytes=64 * 1024,
            max_repository_bytes=1024 * 1024,
            max_manifest_entries=100,
            max_text_characters=64 * 1024,
            max_total_matches=100,
            max_matches_per_rule_file=20,
        ),
        registry=registry,
    )


async def test_local_audit_job_completes_read_only_and_survives_restart(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text(
        'password = "correct-horse-battery-staple"\neval(user_input)\n',
        encoding="utf-8",
    )
    before = _source_digest(source)
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    repository = SQLAlchemyLocalAuditJobRepository(database.session_factory)
    worker = _worker(repository, tmp_path)
    service = LocalAuditJobService(
        repository,
        worker,
        id_factory=lambda: "audit-local-complete",
    )

    draft = await service.create(str(source))
    queued = await service.start(draft.id)
    replayed = await service.start(draft.id)
    completed = await worker.run(draft.id)

    assert draft.status is LocalAuditJobStatus.DRAFT
    assert queued.status is LocalAuditJobStatus.QUEUED
    assert replayed == queued
    assert completed.status is LocalAuditJobStatus.COMPLETED
    assert completed.total_files == completed.scanned_files == 1
    assert completed.findings
    assert all("correct-horse" not in value.evidence_excerpt for value in completed.findings)
    assert completed.json_report is not None and completed.markdown_report is not None
    assert _source_digest(source) == before
    await database.dispose()

    reopened = Database(database_url)
    await reopened.create_schema()
    restarted = SQLAlchemyLocalAuditJobRepository(reopened.session_factory)
    assert await restarted.get(draft.id) == completed
    await reopened.dispose()


class _BlockingDetector:
    metadata = DetectorRuleMetadata(
        rule_id="test.blocking",
        version="1.0.0",
        implementation_digest="a" * 64,
        title="Blocking test rule",
    )

    def __init__(self, entered: Event, release: Event) -> None:
        self._entered = entered
        self._release = release

    def detect(self, detector_input: DetectorInput):
        self._entered.set()
        assert self._release.wait(timeout=10)
        return (
            DetectorMatch(
                line=1,
                column=1,
                message="Late finding",
                evidence=detector_input.content,
            ),
        )


async def test_cancel_race_publishes_no_late_finding(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("dangerous()\n", encoding="utf-8")
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyLocalAuditJobRepository(database.session_factory)
    entered = Event()
    release = Event()
    worker = _worker(
        repository,
        tmp_path,
        registry=DetectorRegistry((_BlockingDetector(entered, release),)),
    )
    service = LocalAuditJobService(
        repository,
        worker,
        id_factory=lambda: "audit-local-cancel",
    )
    job = await service.create(str(source))
    await service.start(job.id)

    task = asyncio.create_task(worker.run(job.id))
    assert await asyncio.to_thread(entered.wait, 10)
    cancelling = await service.cancel(job.id)
    release.set()
    cancelled = await task

    assert cancelling.status is LocalAuditJobStatus.SCANNING
    assert cancelling.cancel_requested is True
    assert cancelled.status is LocalAuditJobStatus.CANCELLED
    assert cancelled.findings == ()
    assert cancelled.json_report is None
    await database.dispose()


async def test_restart_converges_interrupted_jobs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    repository = SQLAlchemyLocalAuditJobRepository(database.session_factory)
    worker = _worker(repository, tmp_path)
    service = LocalAuditJobService(repository, worker)

    interrupted = await service.create(str(source))
    await service.start(interrupted.id)
    claimed, acquired = await repository.claim(interrupted.id)
    assert acquired and claimed.status is LocalAuditJobStatus.SCANNING

    cancelling = await service.create(str(source))
    await service.start(cancelling.id)
    claimed, acquired = await repository.claim(cancelling.id)
    assert acquired and claimed.status is LocalAuditJobStatus.SCANNING
    await repository.request_cancel(cancelling.id)
    await database.dispose()

    reopened = Database(database_url)
    await reopened.create_schema()
    restarted = SQLAlchemyLocalAuditJobRepository(reopened.session_factory)
    assert await restarted.recover_interrupted() == (1, 1)
    failed = await restarted.get(interrupted.id)
    cancelled = await restarted.get(cancelling.id)

    assert failed is not None
    assert failed.status is LocalAuditJobStatus.FAILED
    assert failed.failure_code is LocalAuditFailure.INTERRUPTED
    assert cancelled is not None
    assert cancelled.status is LocalAuditJobStatus.CANCELLED
    assert cancelled.findings == ()
    await reopened.dispose()


async def test_source_rejection_converges_to_stable_failed_status(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = tmp_path / "outside"
    source.mkdir()
    (source / "app.py").write_text("print('data only')\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyLocalAuditJobRepository(database.session_factory)
    worker = LocalAuditWorker(
        repository,
        LocalAuditWorkerConfig(
            allowed_roots=(allowed,),
            protected_paths=(state, tmp_path / "riftx.db"),
            staging_root=state / "staging",
            snapshot_root=state / "snapshots",
            max_file_bytes=64 * 1024,
            max_repository_bytes=1024 * 1024,
            max_manifest_entries=100,
        ),
    )
    service = LocalAuditJobService(
        repository,
        worker,
        id_factory=lambda: "audit-local-rejected",
    )

    job = await service.create(str(source))
    await service.start(job.id)
    failed = await worker.run(job.id)

    assert failed.status is LocalAuditJobStatus.FAILED
    assert failed.failure_code is LocalAuditFailure.SOURCE_REJECTED
    assert str(source) not in repr(failed)
    assert failed.findings == ()
    await database.dispose()
