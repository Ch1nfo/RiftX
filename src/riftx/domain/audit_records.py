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

_SCOPE_TERMINAL_STATUSES = frozenset(
    {
        AuditScopeStatus.ANALYZED,
        AuditScopeStatus.EXCLUDED,
        AuditScopeStatus.DEFERRED,
        AuditScopeStatus.FAILED,
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


__all__ = [
    "AuditProject",
    "AuditRiskTier",
    "AuditScopeKind",
    "AuditScopeStatus",
    "AuditScopeUnit",
    "AuditVcsKind",
    "SOURCE_SNAPSHOT_DIGEST_DOMAIN",
    "SourceSnapshot",
]
