"""SQLAlchemy persistence for simplified single-machine local Audit jobs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from json import JSONDecodeError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from riftx.application.errors import (
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
)
from riftx.audit.jobs import (
    LocalAuditFailure,
    LocalAuditFinding,
    LocalAuditJob,
    LocalAuditJobResult,
    LocalAuditJobStatus,
)
from riftx.domain.base import utc_now

from .orm import LocalAuditJobRecord
from .transactions import SessionFactory, serialized_write


def _from_record(record: LocalAuditJobRecord) -> LocalAuditJob:
    entity_id = record.id if isinstance(record.id, str) else "invalid-id"
    try:
        if not isinstance(record.include_paths_json, list) or not isinstance(
            record.exclude_paths_json, list
        ):
            raise ValueError("invalid path filters")
        if not isinstance(record.findings_json, list):
            raise ValueError("invalid findings")
        findings = tuple(LocalAuditFinding.from_payload(value) for value in record.findings_json)
        if record.finding_count != len(findings):
            raise ValueError("finding count mismatch")
        return LocalAuditJob(
            id=record.id,
            source_path=record.source_path,
            include_paths=tuple(record.include_paths_json),
            exclude_paths=tuple(record.exclude_paths_json),
            status=LocalAuditJobStatus(record.status),
            cancel_requested=record.cancel_requested,
            failure_code=(
                LocalAuditFailure(record.failure_code)
                if record.failure_code is not None
                else None
            ),
            source_identity_digest=record.source_identity_digest,
            snapshot_digest=record.snapshot_digest,
            manifest_digest=record.manifest_digest,
            inventory_digest=record.inventory_digest,
            detector_run_digest=record.detector_run_digest,
            report_digest=record.report_digest,
            total_files=record.total_files,
            scanned_files=record.scanned_files,
            findings=findings,
            json_report=record.json_report,
            markdown_report=record.markdown_report,
            state_version=record.state_version,
            created_at=record.created_at,
            updated_at=record.updated_at,
            queued_at=record.queued_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
        )
    except RepositoryIntegrityError:
        raise
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RepositoryIntegrityError("LocalAuditJob", entity_id) from None


def _clear_results(record: LocalAuditJobRecord) -> None:
    record.source_identity_digest = None
    record.snapshot_digest = None
    record.manifest_digest = None
    record.inventory_digest = None
    record.detector_run_digest = None
    record.report_digest = None
    record.total_files = 0
    record.scanned_files = 0
    record.finding_count = 0
    record.findings_json = []
    record.json_report = None
    record.markdown_report = None


class SQLAlchemyLocalAuditJobRepository:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def create(
        self,
        *,
        audit_id: str,
        source_path: str,
        include_paths: tuple[str, ...],
        exclude_paths: tuple[str, ...],
    ) -> LocalAuditJob:
        now = self._clock()
        job = LocalAuditJob(
            id=audit_id,
            source_path=source_path,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
            status=LocalAuditJobStatus.DRAFT,
            cancel_requested=False,
            failure_code=None,
            source_identity_digest=None,
            snapshot_digest=None,
            manifest_digest=None,
            inventory_digest=None,
            detector_run_digest=None,
            report_digest=None,
            total_files=0,
            scanned_files=0,
            findings=(),
            json_report=None,
            markdown_report=None,
            state_version=1,
            created_at=now,
            updated_at=now,
            queued_at=None,
            started_at=None,
            finished_at=None,
        )
        record = LocalAuditJobRecord(
            id=job.id,
            source_path=job.source_path,
            include_paths_json=list(job.include_paths),
            exclude_paths_json=list(job.exclude_paths),
            status=job.status.value,
            cancel_requested=False,
            failure_code=None,
            source_identity_digest=None,
            snapshot_digest=None,
            manifest_digest=None,
            inventory_digest=None,
            detector_run_digest=None,
            report_digest=None,
            total_files=0,
            scanned_files=0,
            finding_count=0,
            findings_json=[],
            json_report=None,
            markdown_report=None,
            state_version=1,
            created_at=now,
            updated_at=now,
            queued_at=None,
            started_at=None,
            finished_at=None,
        )
        try:
            async with serialized_write(self._session_factory) as session:
                session.add(record)
                await session.flush()
        except IntegrityError:
            raise RepositoryConflictError(
                f"could not create LocalAuditJob {audit_id!r}"
            ) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Local Audit persistence is unavailable"
            ) from None
        return job

    async def get(self, audit_id: str) -> LocalAuditJob | None:
        try:
            async with self._session_factory() as session:
                record = await session.get(LocalAuditJobRecord, audit_id)
        except (JSONDecodeError, TypeError, ValueError):
            raise RepositoryIntegrityError("LocalAuditJob", audit_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Local Audit persistence is unavailable"
            ) from None
        return _from_record(record) if record is not None else None

    async def enqueue(self, audit_id: str) -> LocalAuditJob:
        def mutate(record: LocalAuditJobRecord, now: datetime) -> None:
            if record.status == LocalAuditJobStatus.DRAFT.value:
                record.status = LocalAuditJobStatus.QUEUED.value
                record.queued_at = now
                record.updated_at = now
                record.state_version += 1

        return await self._mutate(audit_id, mutate)

    async def request_cancel(self, audit_id: str) -> LocalAuditJob:
        def mutate(record: LocalAuditJobRecord, now: datetime) -> None:
            if record.status in {
                LocalAuditJobStatus.COMPLETED.value,
                LocalAuditJobStatus.FAILED.value,
                LocalAuditJobStatus.CANCELLED.value,
            }:
                return
            record.cancel_requested = True
            record.updated_at = now
            record.state_version += 1
            if record.status in {
                LocalAuditJobStatus.DRAFT.value,
                LocalAuditJobStatus.QUEUED.value,
            }:
                record.status = LocalAuditJobStatus.CANCELLED.value
                record.finished_at = now

        return await self._mutate(audit_id, mutate)

    async def claim(self, audit_id: str) -> tuple[LocalAuditJob, bool]:
        acquired = False

        def mutate(record: LocalAuditJobRecord, now: datetime) -> None:
            nonlocal acquired
            if (
                record.status == LocalAuditJobStatus.QUEUED.value
                and not record.cancel_requested
            ):
                record.status = LocalAuditJobStatus.SCANNING.value
                record.started_at = now
                record.updated_at = now
                record.state_version += 1
                acquired = True

        job = await self._mutate(audit_id, mutate)
        return job, acquired

    async def complete_or_cancel(
        self,
        audit_id: str,
        result: LocalAuditJobResult,
    ) -> LocalAuditJob:
        if not isinstance(result, LocalAuditJobResult):
            raise TypeError("local Audit result is invalid")

        def mutate(record: LocalAuditJobRecord, now: datetime) -> None:
            if record.status != LocalAuditJobStatus.SCANNING.value:
                if record.status in {
                    LocalAuditJobStatus.COMPLETED.value,
                    LocalAuditJobStatus.FAILED.value,
                    LocalAuditJobStatus.CANCELLED.value,
                }:
                    return
                raise RepositoryConflictError(
                    f"LocalAuditJob {audit_id!r} is not scanning"
                )
            record.updated_at = now
            record.finished_at = now
            record.state_version += 1
            if record.cancel_requested:
                record.status = LocalAuditJobStatus.CANCELLED.value
                record.failure_code = None
                _clear_results(record)
                return
            record.status = LocalAuditJobStatus.COMPLETED.value
            record.failure_code = None
            record.source_identity_digest = result.source_identity_digest
            record.snapshot_digest = result.snapshot_digest
            record.manifest_digest = result.manifest_digest
            record.inventory_digest = result.inventory_digest
            record.detector_run_digest = result.detector_run_digest
            record.report_digest = result.report_digest
            record.total_files = result.total_files
            record.scanned_files = result.scanned_files
            record.finding_count = result.finding_count
            record.findings_json = [value.canonical_payload() for value in result.findings]
            record.json_report = result.json_report
            record.markdown_report = result.markdown_report

        return await self._mutate(audit_id, mutate)

    async def fail_or_cancel(
        self,
        audit_id: str,
        failure: LocalAuditFailure,
    ) -> LocalAuditJob:
        if not isinstance(failure, LocalAuditFailure):
            raise TypeError("local Audit failure is invalid")

        def mutate(record: LocalAuditJobRecord, now: datetime) -> None:
            if record.status in {
                LocalAuditJobStatus.COMPLETED.value,
                LocalAuditJobStatus.FAILED.value,
                LocalAuditJobStatus.CANCELLED.value,
            }:
                return
            if record.status != LocalAuditJobStatus.SCANNING.value:
                raise RepositoryConflictError(
                    f"LocalAuditJob {audit_id!r} is not scanning"
                )
            record.updated_at = now
            record.finished_at = now
            record.state_version += 1
            _clear_results(record)
            if record.cancel_requested:
                record.status = LocalAuditJobStatus.CANCELLED.value
                record.failure_code = None
            else:
                record.status = LocalAuditJobStatus.FAILED.value
                record.failure_code = failure.value

        return await self._mutate(audit_id, mutate)

    async def recover_interrupted(self) -> tuple[int, int]:
        failed = cancelled = 0
        now = self._clock()
        try:
            async with serialized_write(self._session_factory) as session:
                records = (
                    await session.scalars(
                        select(LocalAuditJobRecord)
                        .where(
                            LocalAuditJobRecord.status
                            == LocalAuditJobStatus.SCANNING.value
                        )
                        .order_by(LocalAuditJobRecord.created_at, LocalAuditJobRecord.id)
                        .with_for_update()
                    )
                ).all()
                for record in records:
                    record.updated_at = now
                    record.finished_at = now
                    record.state_version += 1
                    _clear_results(record)
                    if record.cancel_requested:
                        record.status = LocalAuditJobStatus.CANCELLED.value
                        record.failure_code = None
                        cancelled += 1
                    else:
                        record.status = LocalAuditJobStatus.FAILED.value
                        record.failure_code = LocalAuditFailure.INTERRUPTED.value
                        failed += 1
                await session.flush()
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Local Audit persistence is unavailable"
            ) from None
        return failed, cancelled

    async def _mutate(
        self,
        audit_id: str,
        mutation: Callable[[LocalAuditJobRecord, datetime], None],
    ) -> LocalAuditJob:
        try:
            async with serialized_write(self._session_factory) as session:
                record = await session.scalar(
                    select(LocalAuditJobRecord)
                    .where(LocalAuditJobRecord.id == audit_id)
                    .with_for_update()
                )
                if record is None:
                    raise EntityNotFoundError("LocalAuditJob", audit_id)
                mutation(record, self._clock())
                await session.flush()
                return _from_record(record)
        except (EntityNotFoundError, RepositoryConflictError, RepositoryIntegrityError):
            raise
        except IntegrityError:
            raise RepositoryConflictError(
                f"could not update LocalAuditJob {audit_id!r}"
            ) from None
        except (JSONDecodeError, TypeError, ValueError):
            raise RepositoryIntegrityError("LocalAuditJob", audit_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Local Audit persistence is unavailable"
            ) from None


__all__ = ["SQLAlchemyLocalAuditJobRepository"]
