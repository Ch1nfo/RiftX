"""Durable single-machine jobs for the simplified local Code Audit workflow."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import uuid4

from .builtin_detectors import builtin_detector_registry
from .detectors import (
    DetectorCancellation,
    DetectorRegistry,
    DetectorRunLimits,
    LocalDetectorRunner,
)
from .finding_normalizer import normalize_detector_signals
from .inventory import FileInventoryError, build_file_inventory
from .local_materializer import (
    LocalSourceMaterializationError,
    LocalSourceMaterializer,
)
from .paths import (
    SourcePathAuthorizationError,
    open_authorized_local_source,
    validate_posix_absolute_path,
    validate_repository_filters,
)
from .reporting import build_audit_reports
from .snapshot import SnapshotCASBinding, SnapshotStoreError
from .snapshot_store import LocalSnapshotStore
from .snapshot_view import LocalSnapshotViewError, open_local_snapshot_view
from .source_manifest import SourceCapturePolicy, publish_source_manifest

LOCAL_AUDIT_JOB_SCHEMA_VERSION = "riftx.local-audit-job/v1"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class LocalAuditJobStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    SCANNING = "scanning"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LocalAuditFailure(StrEnum):
    SOURCE_REJECTED = "local_audit_source_rejected"
    SNAPSHOT_FAILED = "local_audit_snapshot_failed"
    SCAN_FAILED = "local_audit_scan_failed"
    INTERRUPTED = "local_audit_interrupted"
    INTERNAL_ERROR = "local_audit_internal_error"


@dataclass(frozen=True, slots=True)
class LocalAuditFinding:
    id: str
    rule_id: str
    rule_version: str
    title: str
    severity: str
    confidence: float
    relative_path: str
    blob_digest: str
    line: int
    column: int
    end_line: int | None
    end_column: int | None
    evidence_excerpt: str = field(repr=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("id", self.id),
            ("rule_id", self.rule_id),
            ("rule_version", self.rule_version),
            ("title", self.title),
            ("severity", self.severity),
            ("relative_path", self.relative_path),
            ("evidence_excerpt", self.evidence_excerpt),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"local Audit Finding {label} is invalid")
        if _DIGEST_PATTERN.fullmatch(self.blob_digest) is None:
            raise ValueError("local Audit Finding blob digest is invalid")
        if not isinstance(self.confidence, float) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("local Audit Finding confidence is invalid")
        for value in (self.line, self.column):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("local Audit Finding location is invalid")
        if (self.end_line is None) != (self.end_column is None):
            raise ValueError("local Audit Finding end location is incomplete")
        if self.end_line is not None and (
            self.end_line < self.line
            or self.end_column is None
            or self.end_column < 1
        ):
            raise ValueError("local Audit Finding end location is invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "blob_digest": self.blob_digest,
            "column": self.column,
            "confidence": self.confidence,
            "end_column": self.end_column,
            "end_line": self.end_line,
            "evidence_excerpt": self.evidence_excerpt,
            "id": self.id,
            "line": self.line,
            "relative_path": self.relative_path,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "severity": self.severity,
            "title": self.title,
        }

    @classmethod
    def from_payload(cls, value: object) -> LocalAuditFinding:
        if not isinstance(value, dict):
            raise ValueError("local Audit Finding must be an object")
        expected = {
            "blob_digest",
            "column",
            "confidence",
            "end_column",
            "end_line",
            "evidence_excerpt",
            "id",
            "line",
            "relative_path",
            "rule_id",
            "rule_version",
            "severity",
            "title",
        }
        if set(value) != expected:
            raise ValueError("local Audit Finding shape is invalid")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class LocalAuditJobResult:
    source_identity_digest: str
    snapshot_digest: str
    manifest_digest: str
    inventory_digest: str
    detector_run_digest: str
    report_digest: str
    total_files: int
    scanned_files: int
    findings: tuple[LocalAuditFinding, ...]
    json_report: str = field(repr=False)
    markdown_report: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (
            self.source_identity_digest,
            self.snapshot_digest,
            self.manifest_digest,
            self.inventory_digest,
            self.detector_run_digest,
            self.report_digest,
        ):
            if _DIGEST_PATTERN.fullmatch(value) is None:
                raise ValueError("local Audit result digest is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.total_files, self.scanned_files)
        ) or self.scanned_files != self.total_files:
            raise ValueError("local Audit result counters are invalid")
        if not isinstance(self.findings, tuple) or any(
            not isinstance(value, LocalAuditFinding) for value in self.findings
        ):
            raise ValueError("local Audit result Findings are invalid")
        if not self.json_report.endswith("\n") or not self.markdown_report.endswith("\n"):
            raise ValueError("local Audit reports are invalid")

    @property
    def finding_count(self) -> int:
        return len(self.findings)


@dataclass(frozen=True, slots=True)
class LocalAuditJob:
    id: str
    source_path: str = field(repr=False)
    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]
    status: LocalAuditJobStatus
    cancel_requested: bool
    failure_code: LocalAuditFailure | None
    source_identity_digest: str | None
    snapshot_digest: str | None
    manifest_digest: str | None
    inventory_digest: str | None
    detector_run_digest: str | None
    report_digest: str | None
    total_files: int
    scanned_files: int
    findings: tuple[LocalAuditFinding, ...]
    json_report: str | None = field(repr=False)
    markdown_report: str | None = field(repr=False)
    state_version: int
    created_at: datetime
    updated_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    schema_version: str = field(default=LOCAL_AUDIT_JOB_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        if _ID_PATTERN.fullmatch(self.id) is None:
            raise ValueError("local Audit id is invalid")
        validate_posix_absolute_path(self.source_path)
        filters = validate_repository_filters(
            include_paths=self.include_paths,
            exclude_paths=self.exclude_paths,
        )
        if (
            filters.include_paths != self.include_paths
            or filters.exclude_paths != self.exclude_paths
        ):
            raise ValueError("local Audit filters are not canonical")
        if not isinstance(self.status, LocalAuditJobStatus):
            raise ValueError("local Audit status is invalid")
        if type(self.cancel_requested) is not bool:
            raise ValueError("local Audit cancel flag is invalid")
        if self.failure_code is not None and not isinstance(
            self.failure_code, LocalAuditFailure
        ):
            raise ValueError("local Audit failure code is invalid")
        for value in (
            self.source_identity_digest,
            self.snapshot_digest,
            self.manifest_digest,
            self.inventory_digest,
            self.detector_run_digest,
            self.report_digest,
        ):
            if value is not None and _DIGEST_PATTERN.fullmatch(value) is None:
                raise ValueError("local Audit digest is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.total_files, self.scanned_files)
        ):
            raise ValueError("local Audit counters are invalid")
        if self.scanned_files > self.total_files:
            raise ValueError("local Audit scanned count exceeds total files")
        if self.state_version < 1:
            raise ValueError("local Audit state version is invalid")
        if any(value.utcoffset() is None for value in (self.created_at, self.updated_at)):
            raise ValueError("local Audit timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("local Audit timestamps are out of order")
        for value in (self.queued_at, self.started_at, self.finished_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("local Audit timestamps must be timezone-aware")
        if self.status is LocalAuditJobStatus.COMPLETED:
            required = (
                self.source_identity_digest,
                self.snapshot_digest,
                self.manifest_digest,
                self.inventory_digest,
                self.detector_run_digest,
                self.report_digest,
                self.json_report,
                self.markdown_report,
                self.finished_at,
            )
            if any(value is None for value in required) or self.failure_code is not None:
                raise ValueError("completed local Audit result is incomplete")
            if self.scanned_files != self.total_files:
                raise ValueError("completed local Audit did not process every included file")
        elif self.findings or self.json_report is not None or self.markdown_report is not None:
            raise ValueError("non-completed local Audit cannot publish results")
        if self.status is not LocalAuditJobStatus.FAILED and self.failure_code is not None:
            raise ValueError("non-failed local Audit carries a failure code")
        if self.status is LocalAuditJobStatus.FAILED and (
            self.failure_code is None or self.finished_at is None
        ):
            raise ValueError("failed local Audit has no terminal failure")
        if self.status is LocalAuditJobStatus.CANCELLED and self.finished_at is None:
            raise ValueError("cancelled local Audit has no finish time")
        if self.status is LocalAuditJobStatus.DRAFT and any(
            value is not None for value in (self.queued_at, self.started_at, self.finished_at)
        ):
            raise ValueError("draft local Audit carries execution timestamps")
        if self.status is LocalAuditJobStatus.QUEUED and (
            self.queued_at is None
            or self.started_at is not None
            or self.finished_at is not None
        ):
            raise ValueError("queued local Audit timestamps are invalid")
        if self.status is LocalAuditJobStatus.SCANNING and (
            self.queued_at is None
            or self.started_at is None
            or self.finished_at is not None
        ):
            raise ValueError("scanning local Audit timestamps are invalid")


class LocalAuditJobStore(Protocol):
    async def create(
        self,
        *,
        audit_id: str,
        source_path: str,
        include_paths: tuple[str, ...],
        exclude_paths: tuple[str, ...],
    ) -> LocalAuditJob: ...

    async def get(self, audit_id: str) -> LocalAuditJob | None: ...

    async def enqueue(self, audit_id: str) -> LocalAuditJob: ...

    async def request_cancel(self, audit_id: str) -> LocalAuditJob: ...

    async def claim(self, audit_id: str) -> tuple[LocalAuditJob, bool]: ...

    async def complete_or_cancel(
        self, audit_id: str, result: LocalAuditJobResult
    ) -> LocalAuditJob: ...

    async def fail_or_cancel(
        self, audit_id: str, failure: LocalAuditFailure
    ) -> LocalAuditJob: ...

    async def recover_interrupted(self) -> tuple[int, int]: ...


@dataclass(frozen=True, slots=True)
class LocalAuditWorkerConfig:
    allowed_roots: tuple[Path, ...]
    protected_paths: tuple[Path, ...]
    staging_root: Path
    snapshot_root: Path
    max_file_bytes: int = 5 * 1024 * 1024
    max_repository_bytes: int = 2 * 1024 * 1024 * 1024
    max_manifest_entries: int = 200_000
    max_text_characters: int = 5 * 1024 * 1024
    max_total_matches: int = 50_000
    max_matches_per_rule_file: int = 1_000

    def __post_init__(self) -> None:
        if (
            not isinstance(self.staging_root, Path)
            or not self.staging_root.is_absolute()
            or not isinstance(self.snapshot_root, Path)
            or not self.snapshot_root.is_absolute()
        ):
            raise ValueError("local Audit state paths are invalid")
        for paths in (self.allowed_roots, self.protected_paths):
            if not isinstance(paths, tuple) or any(
                not isinstance(path, Path) or not path.is_absolute() for path in paths
            ):
                raise ValueError("local Audit path configuration is invalid")
        if not self.allowed_roots:
            raise ValueError("local Audit allowed roots are empty")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (
                self.max_file_bytes,
                self.max_repository_bytes,
                self.max_manifest_entries,
                self.max_text_characters,
                self.max_total_matches,
                self.max_matches_per_rule_file,
            )
        ):
            raise ValueError("local Audit limits are invalid")
        if self.max_file_bytes > self.max_repository_bytes:
            raise ValueError("local Audit file limit exceeds repository limit")


class LocalAuditWorker:
    """Claim and execute one durable local Audit without target-code execution."""

    def __init__(
        self,
        store: LocalAuditJobStore,
        config: LocalAuditWorkerConfig,
        *,
        registry: DetectorRegistry | None = None,
    ) -> None:
        self._jobs = store
        self._config = config
        self._registry = registry or builtin_detector_registry()
        self._tokens: dict[str, DetectorCancellation] = {}
        self._tokens_lock = Lock()

    async def run(self, audit_id: str) -> LocalAuditJob:
        claimed, acquired = await self._jobs.claim(audit_id)
        if not acquired:
            return claimed
        token = DetectorCancellation()
        with self._tokens_lock:
            self._tokens[audit_id] = token
        try:
            result = await asyncio.to_thread(self._execute, claimed, token)
            return await self._jobs.complete_or_cancel(audit_id, result)
        except _Cancelled:
            return await self._jobs.fail_or_cancel(
                audit_id, LocalAuditFailure.INTERNAL_ERROR
            )
        except SourcePathAuthorizationError:
            return await self._jobs.fail_or_cancel(
                audit_id, LocalAuditFailure.SOURCE_REJECTED
            )
        except (LocalSourceMaterializationError, SnapshotStoreError):
            return await self._jobs.fail_or_cancel(
                audit_id, LocalAuditFailure.SNAPSHOT_FAILED
            )
        except (LocalSnapshotViewError, FileInventoryError):
            return await self._jobs.fail_or_cancel(
                audit_id, LocalAuditFailure.SCAN_FAILED
            )
        except Exception:
            return await self._jobs.fail_or_cancel(
                audit_id, LocalAuditFailure.INTERNAL_ERROR
            )
        finally:
            with self._tokens_lock:
                self._tokens.pop(audit_id, None)

    def cancel(self, audit_id: str) -> None:
        with self._tokens_lock:
            token = self._tokens.get(audit_id)
        if token is not None:
            token.cancel()

    def _execute(
        self,
        job: LocalAuditJob,
        cancellation: DetectorCancellation,
    ) -> LocalAuditJobResult:
        config = self._config
        filters = validate_repository_filters(
            include_paths=job.include_paths,
            exclude_paths=job.exclude_paths,
        )
        policy = SourceCapturePolicy(
            include_paths=filters.include_paths,
            exclude_paths=filters.exclude_paths,
            max_file_bytes=config.max_file_bytes,
            max_repository_bytes=config.max_repository_bytes,
            max_manifest_entries=config.max_manifest_entries,
        )
        materializer = LocalSourceMaterializer(config.staging_root)
        snapshot_store = LocalSnapshotStore(
            config.snapshot_root,
            max_blob_bytes=config.max_file_bytes,
            max_tree_bytes=config.max_repository_bytes,
        )
        materialized = None
        with open_authorized_local_source(
            job.source_path,
            allowed_roots=config.allowed_roots,
            protected_paths=(
                *config.protected_paths,
                config.staging_root,
                config.snapshot_root,
            ),
            include_paths=filters.include_paths,
            exclude_paths=filters.exclude_paths,
        ) as source:
            if cancellation.cancelled:
                raise _Cancelled
            materialized = materializer.materialize(source, policy=policy)
            source_identity_digest = source.source_identity_digest
        try:
            if cancellation.cancelled:
                raise _Cancelled
            published = publish_source_manifest(
                project_id=job.id,
                manifest=materialized.manifest,
                staging_root=materialized.root,
                snapshot_store=snapshot_store,
                temporary_root=config.staging_root,
            )
            inventory = build_file_inventory(materialized.manifest)
            binding = SnapshotCASBinding(
                project_id=job.id,
                snapshot_digest=published.snapshot_digest,
                manifest_digest=published.manifest_digest,
            )
            descriptor_digest = materialized.manifest.content_descriptor(
                project_id=job.id
            ).descriptor_digest
            limits = DetectorRunLimits(
                max_file_bytes=config.max_file_bytes,
                max_text_characters=config.max_text_characters,
                max_matches_per_rule_file=config.max_matches_per_rule_file,
                max_total_matches=config.max_total_matches,
            )
            with open_local_snapshot_view(
                snapshot_store,
                binding=binding,
                content_storage_key=published.content_storage_key,
                expected_descriptor_digest=descriptor_digest,
                max_file_read_bytes=config.max_file_bytes,
                max_total_read_bytes=config.max_repository_bytes,
                max_text_characters=config.max_text_characters,
            ) as view:
                receipt = LocalDetectorRunner(self._registry, limits).run(
                    view=view,
                    inventory=inventory,
                    cancellation=cancellation,
                )
            if receipt.cancelled or cancellation.cancelled:
                raise _Cancelled
            findings = normalize_detector_signals(receipt.signals)
            reports = build_audit_reports(
                inventory=inventory,
                detector_receipt=receipt,
                findings=findings,
                rules=self._registry.metadata(),
            )
            included_files = inventory.statistics.included_files
            if len(receipt.files) != included_files:
                raise RuntimeError("local Audit did not process every included file")
            return LocalAuditJobResult(
                source_identity_digest=source_identity_digest,
                snapshot_digest=published.snapshot_digest,
                manifest_digest=published.manifest_digest,
                inventory_digest=inventory.inventory_digest,
                detector_run_digest=receipt.run_digest,
                report_digest=reports.report_digest,
                total_files=included_files,
                scanned_files=len(receipt.files),
                findings=tuple(
                    LocalAuditFinding(
                        id=value.id,
                        rule_id=value.rule_id,
                        rule_version=value.rule_version,
                        title=value.title,
                        severity=value.severity.value,
                        confidence=value.confidence,
                        relative_path=value.relative_path,
                        blob_digest=value.blob_digest,
                        line=value.line,
                        column=value.column,
                        end_line=value.end_line,
                        end_column=value.end_column,
                        evidence_excerpt=value.evidence_excerpt,
                    )
                    for value in findings
                ),
                json_report=reports.json_text,
                markdown_report=reports.markdown_text,
            )
        finally:
            if materialized is not None:
                materializer.discard(materialized)


class LocalAuditJobService:
    """Minimal draft/start/cancel/status application facade."""

    def __init__(
        self,
        store: LocalAuditJobStore,
        worker: LocalAuditWorker,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._jobs = store
        self._worker = worker
        self._id_factory = id_factory or (lambda: f"audit-{uuid4().hex}")

    async def create(
        self,
        source_path: str,
        *,
        include_paths: tuple[str, ...] = (),
        exclude_paths: tuple[str, ...] = (),
    ) -> LocalAuditJob:
        canonical_source = validate_posix_absolute_path(source_path)
        filters = validate_repository_filters(
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )
        return await self._jobs.create(
            audit_id=self._id_factory(),
            source_path=canonical_source,
            include_paths=filters.include_paths,
            exclude_paths=filters.exclude_paths,
        )

    async def start(self, audit_id: str) -> LocalAuditJob:
        return await self._jobs.enqueue(audit_id)

    async def cancel(self, audit_id: str) -> LocalAuditJob:
        job = await self._jobs.request_cancel(audit_id)
        self._worker.cancel(audit_id)
        return job

    async def status(self, audit_id: str) -> LocalAuditJob | None:
        return await self._jobs.get(audit_id)

    async def recover(self) -> tuple[int, int]:
        return await self._jobs.recover_interrupted()


class _Cancelled(RuntimeError):
    pass


__all__ = [
    "LOCAL_AUDIT_JOB_SCHEMA_VERSION",
    "LocalAuditFailure",
    "LocalAuditFinding",
    "LocalAuditJob",
    "LocalAuditJobResult",
    "LocalAuditJobService",
    "LocalAuditJobStatus",
    "LocalAuditJobStore",
    "LocalAuditWorker",
    "LocalAuditWorkerConfig",
]
