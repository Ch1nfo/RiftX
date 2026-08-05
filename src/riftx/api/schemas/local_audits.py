"""Minimal HTTP schemas for same-machine local Code Audit jobs."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from riftx.audit import LocalAuditFinding, LocalAuditJob

LocalAuditId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$",
    ),
]


class _LocalAuditWireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        validate_default=True,
    )


class _LocalAuditResponseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )


class CreateLocalAuditRequest(_LocalAuditWireModel):
    source_path: str = Field(min_length=1, max_length=4096, repr=False)
    include_patterns: tuple[str, ...] = Field(default_factory=tuple, max_length=512)
    exclude_patterns: tuple[str, ...] = Field(default_factory=tuple, max_length=512)


class LocalAuditJobResponse(_LocalAuditResponseModel):
    audit_id: LocalAuditId
    status: Literal["draft", "queued", "scanning", "completed", "failed", "cancelled"]
    cancel_requested: bool
    failure_code: str | None
    source_identity_digest: str | None
    snapshot_digest: str | None
    manifest_digest: str | None
    inventory_digest: str | None
    detector_run_digest: str | None
    report_digest: str | None
    total_files: int = Field(ge=0)
    scanned_files: int = Field(ge=0)
    finding_count: int = Field(ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    queued_at: AwareDatetime | None
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None

    @classmethod
    def from_domain(cls, job: LocalAuditJob) -> LocalAuditJobResponse:
        return cls(
            audit_id=job.id,
            status=job.status.value,
            cancel_requested=job.cancel_requested,
            failure_code=job.failure_code.value if job.failure_code is not None else None,
            source_identity_digest=job.source_identity_digest,
            snapshot_digest=job.snapshot_digest,
            manifest_digest=job.manifest_digest,
            inventory_digest=job.inventory_digest,
            detector_run_digest=job.detector_run_digest,
            report_digest=job.report_digest,
            total_files=job.total_files,
            scanned_files=job.scanned_files,
            finding_count=len(job.findings),
            created_at=job.created_at,
            updated_at=job.updated_at,
            queued_at=job.queued_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


class LocalAuditFindingResponse(_LocalAuditResponseModel):
    finding_id: str
    rule_id: str
    rule_version: str
    category: str
    title: str
    severity: Literal["critical", "high", "medium", "low", "info"]
    confidence: float = Field(ge=0.0, le=1.0)
    relative_path: str
    blob_digest: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    end_line: int | None
    end_column: int | None
    evidence_excerpt: str

    @classmethod
    def from_domain(cls, finding: LocalAuditFinding) -> LocalAuditFindingResponse:
        return cls(
            finding_id=finding.id,
            rule_id=finding.rule_id,
            rule_version=finding.rule_version,
            category=finding.rule_id.partition(".")[0],
            title=finding.title,
            severity=finding.severity,  # type: ignore[arg-type]
            confidence=finding.confidence,
            relative_path=finding.relative_path,
            blob_digest=finding.blob_digest,
            line=finding.line,
            column=finding.column,
            end_line=finding.end_line,
            end_column=finding.end_column,
            evidence_excerpt=finding.evidence_excerpt,
        )


class LocalAuditFindingListResponse(_LocalAuditResponseModel):
    items: tuple[LocalAuditFindingResponse, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=1000)
    offset: int = Field(ge=0)


__all__ = [
    "CreateLocalAuditRequest",
    "LocalAuditFindingListResponse",
    "LocalAuditFindingResponse",
    "LocalAuditId",
    "LocalAuditJobResponse",
]
