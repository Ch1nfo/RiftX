"""Historical local Code Audit job records and read-only compatibility service."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .paths import validate_posix_absolute_path, validate_repository_filters

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


class LocalAuditJobService:
    """Read and cancel historical local Audit jobs."""

    def __init__(self, store: LocalAuditJobStore) -> None:
        self._jobs = store

    @property
    def runnable(self) -> bool:
        return False

    async def cancel(self, audit_id: str) -> LocalAuditJob:
        return await self._jobs.request_cancel(audit_id)

    async def status(self, audit_id: str) -> LocalAuditJob | None:
        return await self._jobs.get(audit_id)

    async def recover(self) -> tuple[int, int]:
        return await self._jobs.recover_interrupted()

    async def close(self) -> None:
        return None


__all__ = [
    "LOCAL_AUDIT_JOB_SCHEMA_VERSION",
    "LocalAuditFailure",
    "LocalAuditFinding",
    "LocalAuditJob",
    "LocalAuditJobResult",
    "LocalAuditJobService",
    "LocalAuditJobStatus",
    "LocalAuditJobStore",
]
