"""Strict persistence-facing domain records for RiftX Code Audit.

These records describe durable Audit ledgers without importing SQLAlchemy or any
other infrastructure concern.  They deliberately keep lifecycle validation in the
domain so repositories cannot reconstruct an impossible or partially corrupted row.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .audit import (
    AuditId,
    AuditPhase,
    AuditStrictModel,
    AuditToken,
    AuditVersion,
    Sha256Digest,
    SourceTargetKind,
)
from .base import new_id, utc_now
from .errors import InvalidStateTransitionError

SOURCE_SNAPSHOT_DIGEST_DOMAIN = "riftx.source-snapshot/v1"
AUDIT_CLIENT_REQUEST_SCHEMA_VERSION = "riftx.audit-create-draft-request/v1"
AUDIT_CLIENT_REQUEST_V2_SCHEMA_VERSION = "riftx.audit-create-draft-request/v2"
_MAX_COUNTER = 2**63 - 1
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"

type GitObjectId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=40,
        max_length=64,
        pattern=_GIT_OBJECT_ID_PATTERN,
    ),
]


class AuditVcsKind(StrEnum):
    DIRECTORY = "directory"
    GIT = "git"


class AuditClientRequestOperation(StrEnum):
    CREATE_DRAFT = "create_draft"


class AuditStartIntentStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    STARTED = "started"
    RETRYABLE = "retryable"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED = "cancelled"


class AuditPhaseRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"
    NOT_APPLICABLE = "not_applicable"


class AuditScopeKind(StrEnum):
    FILE = "file"
    SYMBOL = "symbol"
    DIFF_HUNK = "diff_hunk"
    DEPENDENCY = "dependency"
    ENDPOINT = "endpoint"
    CONFIGURATION = "configuration"
    TRUST_BOUNDARY = "trust_boundary"


class AuditRiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AuditScopeStatus(StrEnum):
    INCLUDED = "included"
    ANALYZED = "analyzed"
    EXCLUDED = "excluded"
    DEFERRED = "deferred"
    FAILED = "failed"


class AuditWorkStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


_START_INTENT_TRANSITIONS: dict[AuditStartIntentStatus, frozenset[AuditStartIntentStatus]] = {
    AuditStartIntentStatus.PENDING: frozenset(
        {AuditStartIntentStatus.CLAIMED, AuditStartIntentStatus.CANCELLED}
    ),
    AuditStartIntentStatus.CLAIMED: frozenset(
        {
            AuditStartIntentStatus.STARTED,
            AuditStartIntentStatus.RETRYABLE,
            AuditStartIntentStatus.OUTCOME_UNKNOWN,
            AuditStartIntentStatus.CANCELLED,
        }
    ),
    AuditStartIntentStatus.STARTED: frozenset(),
    AuditStartIntentStatus.RETRYABLE: frozenset(
        {AuditStartIntentStatus.CLAIMED, AuditStartIntentStatus.CANCELLED}
    ),
    AuditStartIntentStatus.OUTCOME_UNKNOWN: frozenset(
        {
            AuditStartIntentStatus.STARTED,
            AuditStartIntentStatus.RETRYABLE,
            AuditStartIntentStatus.CANCELLED,
        }
    ),
    AuditStartIntentStatus.CANCELLED: frozenset(),
}

_PHASE_RUN_TRANSITIONS: dict[AuditPhaseRunStatus, frozenset[AuditPhaseRunStatus]] = {
    AuditPhaseRunStatus.QUEUED: frozenset(
        {
            AuditPhaseRunStatus.RUNNING,
            AuditPhaseRunStatus.DEFERRED,
            AuditPhaseRunStatus.CANCELLED,
            AuditPhaseRunStatus.NOT_APPLICABLE,
        }
    ),
    AuditPhaseRunStatus.RUNNING: frozenset(
        {
            AuditPhaseRunStatus.COMPLETED,
            AuditPhaseRunStatus.FAILED,
            AuditPhaseRunStatus.DEFERRED,
            AuditPhaseRunStatus.CANCELLED,
        }
    ),
    AuditPhaseRunStatus.COMPLETED: frozenset(),
    AuditPhaseRunStatus.FAILED: frozenset(),
    AuditPhaseRunStatus.DEFERRED: frozenset(),
    AuditPhaseRunStatus.CANCELLED: frozenset(),
    AuditPhaseRunStatus.NOT_APPLICABLE: frozenset(),
}

_SCOPE_TRANSITIONS: dict[AuditScopeStatus, frozenset[AuditScopeStatus]] = {
    AuditScopeStatus.INCLUDED: frozenset(
        {
            AuditScopeStatus.ANALYZED,
            AuditScopeStatus.DEFERRED,
            AuditScopeStatus.FAILED,
        }
    ),
    AuditScopeStatus.ANALYZED: frozenset(),
    AuditScopeStatus.EXCLUDED: frozenset(),
    AuditScopeStatus.DEFERRED: frozenset(),
    AuditScopeStatus.FAILED: frozenset(),
}

_WORK_TRANSITIONS: dict[AuditWorkStatus, frozenset[AuditWorkStatus]] = {
    AuditWorkStatus.QUEUED: frozenset(
        {
            AuditWorkStatus.LEASED,
            AuditWorkStatus.DEFERRED,
            AuditWorkStatus.CANCELLED,
        }
    ),
    AuditWorkStatus.LEASED: frozenset(
        {
            AuditWorkStatus.QUEUED,
            AuditWorkStatus.RUNNING,
            AuditWorkStatus.FAILED,
            AuditWorkStatus.CANCELLED,
            AuditWorkStatus.OUTCOME_UNKNOWN,
        }
    ),
    AuditWorkStatus.RUNNING: frozenset(
        {
            AuditWorkStatus.COMPLETED,
            AuditWorkStatus.FAILED,
            AuditWorkStatus.DEFERRED,
            AuditWorkStatus.CANCELLED,
            AuditWorkStatus.OUTCOME_UNKNOWN,
        }
    ),
    AuditWorkStatus.COMPLETED: frozenset(),
    AuditWorkStatus.FAILED: frozenset(),
    AuditWorkStatus.DEFERRED: frozenset(),
    AuditWorkStatus.CANCELLED: frozenset(),
    AuditWorkStatus.OUTCOME_UNKNOWN: frozenset(
        {
            AuditWorkStatus.COMPLETED,
            AuditWorkStatus.FAILED,
            AuditWorkStatus.DEFERRED,
            AuditWorkStatus.CANCELLED,
        }
    ),
}

_PHASE_TERMINAL_STATUSES = frozenset(
    {
        AuditPhaseRunStatus.COMPLETED,
        AuditPhaseRunStatus.FAILED,
        AuditPhaseRunStatus.DEFERRED,
        AuditPhaseRunStatus.CANCELLED,
        AuditPhaseRunStatus.NOT_APPLICABLE,
    }
)
_SCOPE_TERMINAL_STATUSES = frozenset(
    {
        AuditScopeStatus.ANALYZED,
        AuditScopeStatus.EXCLUDED,
        AuditScopeStatus.DEFERRED,
        AuditScopeStatus.FAILED,
    }
)
_WORK_LEASE_STATUSES = frozenset({AuditWorkStatus.LEASED, AuditWorkStatus.RUNNING})
_WORK_TERMINAL_STATUSES = frozenset(
    {
        AuditWorkStatus.COMPLETED,
        AuditWorkStatus.FAILED,
        AuditWorkStatus.DEFERRED,
        AuditWorkStatus.CANCELLED,
    }
)
_RISK_ORDER = {
    AuditRiskTier.LOW: 0,
    AuditRiskTier.MEDIUM: 1,
    AuditRiskTier.HIGH: 2,
    AuditRiskTier.CRITICAL: 3,
}


def _domain_digest(domain: str, payload: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + payload).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _validate_bounded_text(value: str, *, label: str, maximum_bytes: int) -> str:
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    if not value:
        raise ValueError(f"{label} must not be empty")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{label} must not contain control characters")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds the {maximum_bytes}-byte limit")
    return value


def _validate_sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if values != tuple(sorted(values)):
        raise ValueError(f"{label} must use canonical sorted order")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
    return values


def _validate_relative_path(value: str) -> str:
    _validate_bounded_text(value, label="relative_path", maximum_bytes=4096)
    if value.startswith("/") or "\\" in value:
        raise ValueError("relative_path must be a normalized POSIX relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative_path must not contain empty or dot segments")
    return value


class AuditSummaryCount(AuditStrictModel):
    key: AuditToken
    count: int = Field(ge=0, le=_MAX_COUNTER)


class AuditProject(AuditStrictModel):
    """A stable project identity keyed globally by local-source identity digest."""

    id: AuditId = Field(default_factory=new_id)
    engagement_id: AuditId
    display_name: str = Field(min_length=1, max_length=255)
    vcs_kind: AuditVcsKind = AuditVcsKind.GIT
    # This is deliberately not scoped by engagement, node, or local path.  The
    # persistence layer enforces one global unique key for the repository identity.
    repository_identity_digest: Sha256Digest
    default_branch: str | None = Field(default=None, min_length=1, max_length=1024)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return _validate_bounded_text(value, label="display_name", maximum_bytes=1024)

    @field_validator("default_branch")
    @classmethod
    def validate_default_branch(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_bounded_text(value, label="default_branch", maximum_bytes=4096)

    @model_validator(mode="after")
    def validate_timestamps(self) -> AuditProject:
        if self.vcs_kind is AuditVcsKind.DIRECTORY and self.default_branch is not None:
            raise ValueError("directory Audit project cannot carry a default_branch")
        if self.updated_at < self.created_at:
            raise ValueError("Audit project updated_at must not precede created_at")
        return self


class AuditClientRequest(AuditStrictModel):
    """Immutable request-level idempotency fact for one Audit draft.

    Only a domain-separated request digest and authoritative aggregate bindings are
    durable.  The normalized request payload can contain a sensitive source path and
    therefore must never be stored in this record.
    """

    client_request_id: str = Field(min_length=36, max_length=36)
    operation: AuditClientRequestOperation = AuditClientRequestOperation.CREATE_DRAFT
    request_schema_version: str = Field(
        default=AUDIT_CLIENT_REQUEST_SCHEMA_VERSION,
        min_length=1,
        max_length=128,
    )
    request_digest: Sha256Digest
    preflight_plan_id: AuditId | None = None
    preflight_plan_digest: Sha256Digest | None = None
    security_context_id: AuditVersion | None = None
    security_context_digest: Sha256Digest | None = None
    contract_stage: Literal["preflight_bound_draft"] | None = None
    audit_id: AuditId
    run_id: AuditId
    project_id: AuditId
    engagement_id: AuditId
    contract_id: AuditId
    contract_digest: Sha256Digest
    temporal_workflow_id: AuditToken
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("client_request_id")
    @classmethod
    def validate_client_request_id(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ValueError("client_request_id must be a canonical UUID") from exc
        if parsed.int == 0 or str(parsed) != value:
            raise ValueError("client_request_id must be a non-zero canonical UUID")
        return value

    @model_validator(mode="after")
    def validate_binding(self) -> AuditClientRequest:
        preflight_binding = (
            self.preflight_plan_id,
            self.preflight_plan_digest,
            self.security_context_id,
            self.security_context_digest,
            self.contract_stage,
        )
        if self.request_schema_version == AUDIT_CLIENT_REQUEST_SCHEMA_VERSION:
            if any(value is not None for value in preflight_binding):
                raise ValueError("v1 Audit client request cannot carry Preflight binding")
        elif self.request_schema_version == AUDIT_CLIENT_REQUEST_V2_SCHEMA_VERSION:
            if any(value is None for value in preflight_binding):
                raise ValueError("v2 Audit client request requires complete Preflight binding")
        else:
            raise ValueError("Audit client request uses an unsupported schema version")
        if self.temporal_workflow_id != f"riftx-code-audit-{self.audit_id}":
            raise ValueError("Audit client request workflow binding is not deterministic")
        return self


class SourceSnapshot(AuditStrictModel):
    """An immutable, already-sealed source tree stored outside the source repository."""

    id: AuditId = Field(default_factory=new_id)
    project_id: AuditId
    source_kind: SourceTargetKind
    parent_snapshot_id: AuditId | None = None
    base_tree_digest: Sha256Digest | None = None
    patch_digest: Sha256Digest | None = None
    commit_sha: GitObjectId | None = None
    base_commit_sha: GitObjectId | None = None
    working_tree_digest: Sha256Digest | None = None
    tree_digest: Sha256Digest
    capture_policy_digest: Sha256Digest
    materializer_schema_version: AuditVersion
    snapshot_digest: Sha256Digest
    snapshot_store_version: AuditVersion
    content_storage_key: str = Field(min_length=1, max_length=4096, repr=False)
    manifest_storage_key: str = Field(min_length=1, max_length=4096, repr=False)
    manifest_digest: Sha256Digest
    file_count: int = Field(ge=0, le=_MAX_COUNTER)
    total_bytes: int = Field(ge=0, le=_MAX_COUNTER)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    sealed_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("content_storage_key", "manifest_storage_key")
    @classmethod
    def validate_storage_key(cls, value: str) -> str:
        return _validate_bounded_text(value, label="storage key", maximum_bytes=4096)

    @classmethod
    def compute_snapshot_digest(
        cls,
        *,
        tree_digest: str,
        capture_policy_digest: str,
        materializer_schema_version: str,
    ) -> str:
        canonical = _canonical_json(
            {
                "capture_policy_digest": capture_policy_digest,
                "materializer_schema_version": materializer_schema_version,
                "tree_digest": tree_digest,
            }
        )
        return _domain_digest(SOURCE_SNAPSHOT_DIGEST_DOMAIN, canonical.encode("utf-8"))

    @model_validator(mode="after")
    def validate_snapshot(self) -> SourceSnapshot:
        retest_fields = (
            self.parent_snapshot_id,
            self.base_tree_digest,
            self.patch_digest,
        )
        if any(value is not None for value in retest_fields) and not all(
            value is not None for value in retest_fields
        ):
            raise ValueError(
                "parent_snapshot_id, base_tree_digest, and patch_digest must appear together"
            )
        if self.parent_snapshot_id == self.id:
            raise ValueError("Source Snapshot cannot be its own parent")
        expected_digest = self.compute_snapshot_digest(
            tree_digest=self.tree_digest,
            capture_policy_digest=self.capture_policy_digest,
            materializer_schema_version=self.materializer_schema_version,
        )
        if self.snapshot_digest != expected_digest:
            raise ValueError(
                "snapshot_digest does not match the canonical Source Snapshot identity"
            )
        if self.source_kind is SourceTargetKind.DIRECTORY:
            if any(
                value is not None
                for value in (
                    self.commit_sha,
                    self.base_commit_sha,
                    self.working_tree_digest,
                    self.parent_snapshot_id,
                    self.base_tree_digest,
                    self.patch_digest,
                )
            ):
                raise ValueError("directory Source Snapshot cannot carry Git or retest state")
        elif self.source_kind is SourceTargetKind.REVISION:
            if self.commit_sha is None:
                raise ValueError("revision Source Snapshot requires commit_sha")
            if self.working_tree_digest is not None:
                raise ValueError("revision Source Snapshot cannot carry working_tree_digest")
        elif self.working_tree_digest is None:
            raise ValueError("working-tree Source Snapshot requires working_tree_digest")
        elif self.commit_sha is None:
            raise ValueError("working-tree Source Snapshot requires commit_sha")
        if self.sealed_at < self.created_at:
            raise ValueError("Source Snapshot sealed_at must not precede created_at")
        return self


class AuditStartIntent(AuditStrictModel):
    id: AuditId = Field(default_factory=new_id)
    audit_id: AuditId
    run_id: AuditId
    start_request_id: AuditId
    contract_digest: Sha256Digest
    workflow_id: AuditToken
    task_queue: AuditToken
    status: AuditStartIntentStatus = AuditStartIntentStatus.PENDING
    attempt: int = Field(default=0, ge=0, le=2**31 - 1)
    lease_owner: AuditToken | None = None
    lease_expires_at: AwareDatetime | None = None
    next_attempt_at: AwareDatetime | None = None
    last_error_code: AuditToken | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    started_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_intent(self) -> AuditStartIntent:
        if self.workflow_id != f"riftx-code-audit-{self.audit_id}":
            raise ValueError("Start Intent workflow_id must be deterministic for its Audit")
        has_lease_owner = self.lease_owner is not None
        has_lease_expiry = self.lease_expires_at is not None
        if has_lease_owner != has_lease_expiry:
            raise ValueError("Start Intent lease owner and expiry must appear together")
        if self.status is AuditStartIntentStatus.CLAIMED:
            if not has_lease_owner:
                raise ValueError("claimed Start Intent requires a lease")
        elif has_lease_owner:
            raise ValueError("only a claimed Start Intent may carry a lease")
        if self.status is AuditStartIntentStatus.RETRYABLE:
            if self.next_attempt_at is None:
                raise ValueError("retryable Start Intent requires next_attempt_at")
        elif (
            self.status
            not in {
                AuditStartIntentStatus.PENDING,
                AuditStartIntentStatus.OUTCOME_UNKNOWN,
            }
            and self.next_attempt_at is not None
        ):
            raise ValueError("Start Intent status cannot carry next_attempt_at")
        if self.status is AuditStartIntentStatus.STARTED:
            if self.started_at is None:
                raise ValueError("started Start Intent requires started_at")
        elif self.started_at is not None:
            raise ValueError("only a started Start Intent may carry started_at")
        if (
            self.status
            in {
                AuditStartIntentStatus.CLAIMED,
                AuditStartIntentStatus.STARTED,
                AuditStartIntentStatus.RETRYABLE,
                AuditStartIntentStatus.OUTCOME_UNKNOWN,
            }
            and self.attempt < 1
        ):
            raise ValueError("attempted Start Intent status requires a positive attempt")
        if self.updated_at < self.created_at:
            raise ValueError("Start Intent updated_at must not precede created_at")
        if self.lease_expires_at is not None and self.lease_expires_at <= self.updated_at:
            raise ValueError("Start Intent lease must expire after updated_at")
        if self.next_attempt_at is not None and self.next_attempt_at < self.updated_at:
            raise ValueError("Start Intent next_attempt_at must not precede updated_at")
        if self.started_at is not None and not (
            self.created_at <= self.started_at <= self.updated_at
        ):
            raise ValueError("Start Intent started_at must be between created_at and updated_at")
        return self

    def can_transition_to(self, target: AuditStartIntentStatus) -> bool:
        return target in _START_INTENT_TRANSITIONS[self.status]

    def transition_to(
        self,
        target: AuditStartIntentStatus,
        *,
        at: AwareDatetime | None = None,
        lease_owner: str | None = None,
        lease_expires_at: AwareDatetime | None = None,
        next_attempt_at: AwareDatetime | None = None,
        last_error_code: str | None = None,
    ) -> Self:
        if not self.can_transition_to(target):
            raise InvalidStateTransitionError("AuditStartIntent", self.status, target)
        transition_at = at or utc_now()
        if transition_at < self.updated_at:
            raise ValueError("Start Intent transition time must not precede updated_at")
        updates: dict[str, object] = {
            "status": target,
            "updated_at": transition_at,
            "lease_owner": None,
            "lease_expires_at": None,
            "next_attempt_at": None,
            "last_error_code": last_error_code,
        }
        if target is AuditStartIntentStatus.CLAIMED:
            if lease_owner is None or lease_expires_at is None:
                raise ValueError("claiming a Start Intent requires lease owner and expiry")
            if lease_expires_at <= transition_at:
                raise ValueError("Start Intent lease must expire after the claim time")
            updates.update(
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                attempt=self.attempt + 1,
                last_error_code=None,
            )
        elif target is AuditStartIntentStatus.STARTED:
            updates.update(started_at=transition_at, last_error_code=None)
        elif target is AuditStartIntentStatus.RETRYABLE:
            if next_attempt_at is None or next_attempt_at < transition_at:
                raise ValueError("retryable Start Intent requires a future retry time")
            updates["next_attempt_at"] = next_attempt_at
        elif target is AuditStartIntentStatus.OUTCOME_UNKNOWN:
            updates["next_attempt_at"] = next_attempt_at
        return type(self).model_validate({**self.model_dump(mode="python"), **updates})


class AuditPhaseRun(AuditStrictModel):
    id: AuditId = Field(default_factory=new_id)
    audit_id: AuditId
    phase: AuditPhase
    attempt: int = Field(default=1, ge=1, le=2**31 - 1)
    idempotency_key: AuditToken
    input_digest: Sha256Digest
    config_digest: Sha256Digest
    status: AuditPhaseRunStatus = AuditPhaseRunStatus.QUEUED
    output_artifact_ids: tuple[AuditId, ...] = Field(default_factory=tuple, max_length=256)
    summary_counts: tuple[AuditSummaryCount, ...] = Field(default_factory=tuple, max_length=128)
    error_code: AuditToken | None = None
    error_summary: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    @field_validator("output_artifact_ids")
    @classmethod
    def validate_output_artifact_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_sorted_unique(values, label="Phase output Artifact IDs")

    @field_validator("summary_counts")
    @classmethod
    def validate_summary_counts(
        cls, values: tuple[AuditSummaryCount, ...]
    ) -> tuple[AuditSummaryCount, ...]:
        keys = tuple(value.key for value in values)
        _validate_sorted_unique(keys, label="Phase summary count keys")
        return values

    @field_validator("error_summary")
    @classmethod
    def validate_error_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_bounded_text(value, label="error_summary", maximum_bytes=4096)

    @model_validator(mode="after")
    def validate_phase_run(self) -> AuditPhaseRun:
        has_error_code = self.error_code is not None
        has_error_summary = self.error_summary is not None
        if has_error_code != has_error_summary:
            raise ValueError("Phase error code and summary must appear together")
        if self.status in {
            AuditPhaseRunStatus.QUEUED,
            AuditPhaseRunStatus.RUNNING,
        } and (self.output_artifact_ids or self.summary_counts):
            raise ValueError("active Phase Run cannot carry output facts")
        if self.status is AuditPhaseRunStatus.QUEUED:
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError("queued Phase Run cannot carry lifecycle timestamps")
        elif self.status is AuditPhaseRunStatus.RUNNING:
            if self.started_at is None or self.finished_at is not None:
                raise ValueError("running Phase Run requires only started_at")
        elif self.finished_at is None:
            raise ValueError("terminal Phase Run requires finished_at")
        if self.status is AuditPhaseRunStatus.COMPLETED and self.started_at is None:
            raise ValueError("completed Phase Run requires started_at")
        if (
            self.status
            in {
                AuditPhaseRunStatus.FAILED,
                AuditPhaseRunStatus.DEFERRED,
                AuditPhaseRunStatus.NOT_APPLICABLE,
            }
            and not has_error_code
        ):
            raise ValueError("failed, deferred, or not-applicable Phase Run requires a reason")
        if (
            self.status
            in {
                AuditPhaseRunStatus.QUEUED,
                AuditPhaseRunStatus.RUNNING,
                AuditPhaseRunStatus.COMPLETED,
            }
            and has_error_code
        ):
            raise ValueError("non-error Phase Run status cannot carry an error")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("Phase Run finished_at must not precede started_at")
        if self.updated_at < self.created_at:
            raise ValueError("Phase Run updated_at must not precede created_at")
        if self.started_at is not None and not (
            self.created_at <= self.started_at <= self.updated_at
        ):
            raise ValueError("Phase Run started_at must be between created_at and updated_at")
        if self.finished_at is not None and not (
            self.created_at <= self.finished_at <= self.updated_at
        ):
            raise ValueError("Phase Run finished_at must be between created_at and updated_at")
        return self

    def can_transition_to(self, target: AuditPhaseRunStatus) -> bool:
        return target in _PHASE_RUN_TRANSITIONS[self.status]

    def transition_to(
        self,
        target: AuditPhaseRunStatus,
        *,
        at: AwareDatetime | None = None,
        output_artifact_ids: tuple[str, ...] | None = None,
        summary_counts: tuple[AuditSummaryCount, ...] | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> Self:
        if not self.can_transition_to(target):
            raise InvalidStateTransitionError("AuditPhaseRun", self.status, target)
        transition_at = at or utc_now()
        if transition_at < self.updated_at:
            raise ValueError("Phase Run transition time must not precede updated_at")
        updates: dict[str, object] = {"status": target, "updated_at": transition_at}
        if output_artifact_ids is not None:
            updates["output_artifact_ids"] = output_artifact_ids
        if summary_counts is not None:
            updates["summary_counts"] = summary_counts
        if target is AuditPhaseRunStatus.RUNNING:
            updates.update(started_at=transition_at, error_code=None, error_summary=None)
        elif target in _PHASE_TERMINAL_STATUSES:
            updates.update(
                finished_at=transition_at,
                error_code=error_code,
                error_summary=error_summary,
            )
        return type(self).model_validate({**self.model_dump(mode="python"), **updates})


class AuditScopeUnit(AuditStrictModel):
    id: AuditId = Field(default_factory=new_id)
    audit_id: AuditId
    snapshot_id: AuditId
    kind: AuditScopeKind
    relative_path: str | None = Field(default=None, min_length=1, max_length=4096)
    blob_digest: Sha256Digest | None = None
    symbol_anchor: str | None = Field(default=None, min_length=1, max_length=2048)
    risk_tier: AuditRiskTier
    required_analyses: tuple[AuditToken, ...] = Field(default_factory=tuple, max_length=64)
    status: AuditScopeStatus = AuditScopeStatus.INCLUDED
    closure_code: AuditToken | None = None
    closure_reason: str | None = Field(default=None, min_length=1, max_length=4096)
    receipt_count: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    stable_key: Sha256Digest
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        return _validate_relative_path(value) if value is not None else None

    @field_validator("symbol_anchor")
    @classmethod
    def validate_symbol_anchor(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_bounded_text(value, label="symbol_anchor", maximum_bytes=4096)

    @field_validator("required_analyses")
    @classmethod
    def validate_required_analyses(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_sorted_unique(values, label="required analyses")

    @field_validator("closure_reason")
    @classmethod
    def validate_closure_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_bounded_text(value, label="closure_reason", maximum_bytes=4096)

    @model_validator(mode="after")
    def validate_scope(self) -> AuditScopeUnit:
        path_bound_kinds = {
            AuditScopeKind.FILE,
            AuditScopeKind.SYMBOL,
            AuditScopeKind.DIFF_HUNK,
            AuditScopeKind.CONFIGURATION,
        }
        if self.kind in path_bound_kinds and self.relative_path is None:
            raise ValueError("path-bound Scope Unit requires relative_path")
        if self.kind is AuditScopeKind.SYMBOL and self.symbol_anchor is None:
            raise ValueError("symbol Scope Unit requires symbol_anchor")
        has_closure_code = self.closure_code is not None
        has_closure_reason = self.closure_reason is not None
        if has_closure_code != has_closure_reason:
            raise ValueError("Scope closure code and reason must appear together")
        if self.status in _SCOPE_TERMINAL_STATUSES:
            if not has_closure_code:
                raise ValueError("terminal Scope Unit requires closure code and reason")
        elif has_closure_code:
            raise ValueError("included Scope Unit cannot carry terminal closure data")
        if self.updated_at < self.created_at:
            raise ValueError("Scope Unit updated_at must not precede created_at")
        return self

    def can_transition_to(self, target: AuditScopeStatus) -> bool:
        return target in _SCOPE_TRANSITIONS[self.status]

    def transition_to(
        self,
        target: AuditScopeStatus,
        *,
        closure_code: str,
        closure_reason: str,
        receipt_count: int | None = None,
        at: AwareDatetime | None = None,
    ) -> Self:
        if not self.can_transition_to(target):
            raise InvalidStateTransitionError("AuditScopeUnit", self.status, target)
        transition_at = at or utc_now()
        if transition_at < self.updated_at:
            raise ValueError("Scope Unit transition time must not precede updated_at")
        updates: dict[str, object] = {
            "status": target,
            "closure_code": closure_code,
            "closure_reason": closure_reason,
            "updated_at": transition_at,
        }
        if receipt_count is not None:
            updates["receipt_count"] = receipt_count
        return type(self).model_validate({**self.model_dump(mode="python"), **updates})

    def elevate_risk(
        self,
        target: AuditRiskTier,
        *,
        at: AwareDatetime | None = None,
    ) -> Self:
        if self.status is not AuditScopeStatus.INCLUDED:
            raise ValueError("terminal Scope risk cannot be rewritten")
        if _RISK_ORDER[target] <= _RISK_ORDER[self.risk_tier]:
            raise ValueError("Scope risk elevation must be strictly monotonic")
        transition_at = at or utc_now()
        if transition_at < self.updated_at:
            raise ValueError("Scope risk elevation time must not precede updated_at")
        return type(self).model_validate(
            {
                **self.model_dump(mode="python"),
                "risk_tier": target,
                "updated_at": transition_at,
            }
        )


class AuditWorkItem(AuditStrictModel):
    id: AuditId = Field(default_factory=new_id)
    audit_id: AuditId
    phase: AuditPhase
    epoch: int = Field(default=0, ge=0, le=2**31 - 1)
    primary_scope_unit_id: AuditId
    strategy: AuditToken
    stable_key: Sha256Digest
    risk_tier: AuditRiskTier
    status: AuditWorkStatus = AuditWorkStatus.QUEUED
    lease_owner: AuditToken | None = None
    lease_expires_at: AwareDatetime | None = None
    attempt: int = Field(default=0, ge=0, le=2**31 - 1)
    input_digest: Sha256Digest
    required_coverage_plan_artifact_id: AuditId
    required_coverage_plan_digest: Sha256Digest
    receipt_id: AuditId | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_work_item(self) -> AuditWorkItem:
        has_lease_owner = self.lease_owner is not None
        has_lease_expiry = self.lease_expires_at is not None
        if has_lease_owner != has_lease_expiry:
            raise ValueError("Work Item lease owner and expiry must appear together")
        if self.status in _WORK_LEASE_STATUSES:
            if not has_lease_owner:
                raise ValueError("leased or running Work Item requires a lease")
        elif has_lease_owner:
            raise ValueError("Work Item status cannot carry an active lease")
        if (
            self.status
            in {
                AuditWorkStatus.LEASED,
                AuditWorkStatus.RUNNING,
                AuditWorkStatus.COMPLETED,
                AuditWorkStatus.FAILED,
                AuditWorkStatus.OUTCOME_UNKNOWN,
            }
            and self.attempt < 1
        ):
            raise ValueError("attempted Work Item status requires a positive attempt")
        if self.status is AuditWorkStatus.COMPLETED:
            if self.receipt_id is None:
                raise ValueError("completed Work Item requires receipt_id")
        elif self.receipt_id is not None:
            raise ValueError("only a completed Work Item may carry receipt_id")
        if self.updated_at < self.created_at:
            raise ValueError("Work Item updated_at must not precede created_at")
        if self.lease_expires_at is not None and self.lease_expires_at <= self.updated_at:
            raise ValueError("active Work Item lease must expire after updated_at")
        return self

    def can_transition_to(self, target: AuditWorkStatus) -> bool:
        return target in _WORK_TRANSITIONS[self.status]

    def transition_to(
        self,
        target: AuditWorkStatus,
        *,
        at: AwareDatetime | None = None,
        lease_owner: str | None = None,
        lease_expires_at: AwareDatetime | None = None,
        receipt_id: str | None = None,
    ) -> Self:
        if not self.can_transition_to(target):
            raise InvalidStateTransitionError("AuditWorkItem", self.status, target)
        transition_at = at or utc_now()
        if transition_at < self.updated_at:
            raise ValueError("Work Item transition time must not precede updated_at")
        updates: dict[str, object] = {
            "status": target,
            "updated_at": transition_at,
            "lease_owner": None,
            "lease_expires_at": None,
            "receipt_id": None,
        }
        if target is AuditWorkStatus.LEASED:
            if lease_owner is None or lease_expires_at is None:
                raise ValueError("leasing a Work Item requires lease owner and expiry")
            if lease_expires_at <= transition_at:
                raise ValueError("Work Item lease must expire after the lease time")
            updates.update(
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                attempt=self.attempt + 1,
            )
        elif target is AuditWorkStatus.RUNNING:
            updates.update(
                lease_owner=self.lease_owner,
                lease_expires_at=self.lease_expires_at,
            )
        elif target is AuditWorkStatus.COMPLETED:
            if receipt_id is None:
                raise ValueError("completing a Work Item requires receipt_id")
            updates["receipt_id"] = receipt_id
        return type(self).model_validate({**self.model_dump(mode="python"), **updates})


__all__ = [
    "AuditPhaseRun",
    "AuditPhaseRunStatus",
    "AuditProject",
    "AuditRiskTier",
    "AuditScopeKind",
    "AuditScopeStatus",
    "AuditScopeUnit",
    "AuditStartIntent",
    "AuditStartIntentStatus",
    "AuditSummaryCount",
    "AuditVcsKind",
    "AuditWorkItem",
    "AuditWorkStatus",
    "SOURCE_SNAPSHOT_DIGEST_DOMAIN",
    "SourceSnapshot",
]
