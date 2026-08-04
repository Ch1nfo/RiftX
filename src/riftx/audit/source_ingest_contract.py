"""Bounded data contract between the Runner and SourceIngest capsule worker.

The worker receives only the container-local repository mount and immutable
Preflight facts.  Host paths, credentials, sockets, Run/Audit identity, and
post-Preflight authorization objects are intentionally absent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from riftx.domain.audit import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    MAX_PREFLIGHT_COUNTER,
    MAX_PREFLIGHT_LANGUAGE_ESTIMATES,
    MAX_PREFLIGHT_RELATIVE_PATHS,
    MAX_PREFLIGHT_SAFE_CODES,
    AuditPreflightStrictModel,
)

SOURCE_INGEST_WORKER_REQUEST_SCHEMA_VERSION: Literal[
    "riftx.audit-source-ingest-worker-request/v1"
] = "riftx.audit-source-ingest-worker-request/v1"
SOURCE_INGEST_WORKER_RESULT_SCHEMA_VERSION: Literal[
    "riftx.audit-source-ingest-worker-result/v1"
] = "riftx.audit-source-ingest-worker-result/v1"

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$"
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"

Digest = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=_DIGEST_PATTERN),
]
SafeCode = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_SAFE_CODE_PATTERN),
]
GitObjectId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=40,
        max_length=64,
        pattern=_GIT_OBJECT_ID_PATTERN,
    ),
]


class SourceIngestWorkerOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


class SourceIngestWorkerRequest(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-source-ingest-worker-request/v1"] = (
        SOURCE_INGEST_WORKER_REQUEST_SCHEMA_VERSION
    )
    capsule_id: str = Field(min_length=1, max_length=128)
    request_digest: Digest
    source_root_identity_digest: Digest
    repository_descriptor_identity_digest: Digest
    expected_source_mount_identity_digest: Digest
    target_kind: SourceTargetKind
    revision: str = Field(min_length=1, max_length=1_024)
    base_revision: str | None = Field(default=None, min_length=1, max_length=1_024)
    mode: AuditMode
    include_untracked: bool = False
    include_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=512)
    exclude_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=512)
    max_files: int = Field(strict=True, ge=1, le=MAX_PREFLIGHT_COUNTER)
    max_repository_bytes: int = Field(strict=True, ge=1, le=MAX_PREFLIGHT_COUNTER)
    max_file_bytes: int = Field(strict=True, ge=1, le=MAX_PREFLIGHT_COUNTER)
    max_git_output_bytes: int = Field(strict=True, ge=1_024, le=16 * 1024 * 1024)
    command_timeout_seconds: int = Field(strict=True, ge=1, le=300)

    @field_validator("include_paths", "exclude_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > MAX_PREFLIGHT_RELATIVE_PATHS:
            raise ValueError("SourceIngest path filter limit exceeded")
        if values != tuple(sorted(set(values))):
            raise ValueError("SourceIngest path filters must be sorted and unique")
        return values

    @model_validator(mode="after")
    def validate_request(self) -> SourceIngestWorkerRequest:
        if self.max_file_bytes > self.max_repository_bytes:
            raise ValueError("SourceIngest max_file_bytes exceeds repository limit")
        if set(self.include_paths).intersection(self.exclude_paths):
            raise ValueError("SourceIngest include/exclude filters overlap")
        if self.mode is AuditMode.DIFF:
            if self.base_revision is None:
                raise ValueError("SourceIngest diff mode requires base_revision")
        elif self.base_revision is not None:
            raise ValueError("SourceIngest non-diff mode cannot carry base_revision")
        if self.target_kind is SourceTargetKind.REVISION and self.include_untracked:
            raise ValueError("SourceIngest revision target cannot include untracked files")
        return self


class SourceIngestLanguageEstimate(AuditPreflightStrictModel):
    language_id: SafeCode
    file_count: int = Field(strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)
    total_bytes: int = Field(strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)


class SourceIngestWorkerResult(AuditPreflightStrictModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        validate_default=True,
    )

    schema_version: Literal["riftx.audit-source-ingest-worker-result/v1"] = (
        SOURCE_INGEST_WORKER_RESULT_SCHEMA_VERSION
    )
    outcome: SourceIngestWorkerOutcome
    safe_error_code: SafeCode | None = None
    request_digest: Digest
    source_root_identity_digest: Digest
    repository_descriptor_identity_digest: Digest
    source_mount_identity_digest: Digest | None = None
    source_mount_proof_digest: Digest | None = None
    repository_identity_digest: Digest | None = None
    content_identity_digest: Digest | None = None
    git_version: str | None = Field(default=None, min_length=1, max_length=128)
    git_component_digest: Digest | None = None
    git_proof_digest: Digest | None = None
    head_revision: GitObjectId | None = None
    resolved_revision: GitObjectId | None = None
    resolved_base_revision: GitObjectId | None = None
    merge_base_revision: GitObjectId | None = None
    dirty: bool = False
    staged: bool = False
    unstaged: bool = False
    untracked: bool = False
    file_count: int = Field(default=0, strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)
    total_bytes: int = Field(default=0, strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)
    max_file_bytes: int = Field(default=0, strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)
    language_estimates: tuple[SourceIngestLanguageEstimate, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PREFLIGHT_LANGUAGE_ESTIMATES,
    )
    capability_warnings: tuple[SafeCode, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PREFLIGHT_SAFE_CODES,
    )
    blocking_errors: tuple[SafeCode, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PREFLIGHT_SAFE_CODES,
    )

    @field_validator("capability_warnings", "blocking_errors")
    @classmethod
    def validate_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("SourceIngest safe codes must be sorted and unique")
        return values

    @field_validator("language_estimates")
    @classmethod
    def validate_languages(
        cls,
        values: tuple[SourceIngestLanguageEstimate, ...],
    ) -> tuple[SourceIngestLanguageEstimate, ...]:
        identities = tuple(value.language_id for value in values)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("SourceIngest language estimates must be sorted and unique")
        return values

    @model_validator(mode="after")
    def validate_result(self) -> SourceIngestWorkerResult:
        if self.dirty != any((self.staged, self.unstaged, self.untracked)):
            raise ValueError("SourceIngest dirty summary is inconsistent")
        if self.max_file_bytes > self.total_bytes:
            raise ValueError("SourceIngest max_file_bytes exceeds total_bytes")
        if self.file_count == 0 and (self.total_bytes or self.max_file_bytes):
            raise ValueError("SourceIngest empty result cannot report bytes")
        proof_fields = (
            self.source_mount_identity_digest,
            self.source_mount_proof_digest,
            self.repository_identity_digest,
            self.content_identity_digest,
            self.git_version,
            self.git_component_digest,
            self.git_proof_digest,
        )
        if self.outcome is SourceIngestWorkerOutcome.FAILED:
            if self.safe_error_code is None:
                raise ValueError("failed SourceIngest result requires a safe error code")
            if any(value is not None for value in proof_fields):
                raise ValueError("failed SourceIngest result cannot claim repository proof")
            if self.blocking_errors:
                raise ValueError("failed SourceIngest result cannot claim policy rejection")
            return self
        if not all(value is not None for value in proof_fields):
            raise ValueError("completed SourceIngest result requires repository proof")
        if self.outcome is SourceIngestWorkerOutcome.REJECTED:
            if self.safe_error_code is None or not self.blocking_errors:
                raise ValueError("rejected SourceIngest result requires blocking facts")
        elif self.safe_error_code is not None or self.blocking_errors:
            raise ValueError("successful SourceIngest result cannot carry blocking error")
        return self


__all__ = [
    "SOURCE_INGEST_WORKER_REQUEST_SCHEMA_VERSION",
    "SOURCE_INGEST_WORKER_RESULT_SCHEMA_VERSION",
    "SourceIngestLanguageEstimate",
    "SourceIngestWorkerOutcome",
    "SourceIngestWorkerRequest",
    "SourceIngestWorkerResult",
]
