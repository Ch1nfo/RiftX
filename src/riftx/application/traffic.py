"""Safe contracts for the Target HTTP metadata-only read model."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from riftx.domain import ApprovalStatus, NodeStatus

_DIGEST = re.compile(r"[0-9a-f]{64}")
_OPAQUE_ARTIFACT_REF = re.compile(r"traffic-artifact:v1:[0-9a-f]{64}")
_MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]{1,64}/[a-z0-9!#$&^_.+-]{1,127}")
TRAFFIC_SAFE_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/octet-stream",
        "application/pdf",
        "application/xml",
        "application/zip",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/svg+xml",
        "image/webp",
        "text/css",
        "text/csv",
        "text/html",
        "text/plain",
        "text/xml",
    }
)


class TrafficMetadataCapability(StrEnum):
    """Typed application capability; adapters map it to a deployment profile."""

    READ = "traffic.metadata.read"


class TrafficStatusClass(StrEnum):
    INFORMATIONAL = "informational"
    SUCCESS = "success"
    REDIRECT = "redirect"
    CLIENT_ERROR = "client_error"
    SERVER_ERROR = "server_error"


class TrafficAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class TrafficProjectionQuality(StrEnum):
    EXACT = "exact"
    PARTIAL = "partial"


class TrafficDigestStability(StrEnum):
    SERVER_INSTANCE = "server_instance"


class TrafficArtifactPresence(StrEnum):
    RECORDED_PRESENT = "recorded_present"
    RECORDED_MISSING = "recorded_missing"
    NOT_RECORDED = "not_recorded"


class TrafficMetadataAccess(StrEnum):
    METADATA_ONLY = "metadata_only"


class TrafficBodyAvailability(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class TrafficTlsAvailability(StrEnum):
    AVAILABLE = "available"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


class TrafficCreatedByKind(StrEnum):
    AGENT_RUNTIME = "agent_runtime"
    UNKNOWN = "unknown"


class TrafficApprovalAvailability(StrEnum):
    AVAILABLE = "available"
    NOT_REQUIRED = "not_required"
    UNAVAILABLE = "unavailable"


class TrafficSensitivityClass(StrEnum):
    RESTRICTED_SENSITIVE = "restricted_sensitive"


class TrafficRetentionState(StrEnum):
    LEGACY_UNMANAGED = "legacy_unmanaged"


class TrafficRevealCapability(StrEnum):
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class TrafficScopeSource:
    run_id: str
    engagement_id: str


@dataclass(frozen=True, slots=True)
class TrafficPageKey:
    created_at: datetime
    exchange_id: str


@dataclass(frozen=True, slots=True)
class TrafficSnapshotSource:
    boundary: TrafficPageKey | None
    total: int


@dataclass(frozen=True, slots=True)
class TrafficExchangeSource:
    """Persistence allowlist; raw URL/JSON/hash/Artifact IDs are impossible here."""

    exchange_id: str
    execution_key: str
    run_id: str
    session_id: str
    tool_call_id: str
    node_id: str
    node_status: str | None
    method: str
    canonical_request_digest: str
    safe_metadata_version: int | None
    url_scheme: str | None
    url_origin: str | None
    url_path_shape: str | None
    url_path_segment_count: int | None
    redirect_count: int | None
    redirect_origins: tuple[str, ...] | None
    request_body_availability: str | None
    status_code: int
    elapsed_ms: int
    content_type: str | None
    content_type_redacted: bool
    content_length: int | None
    response_truncated: bool
    tls_verified: bool | None
    tls_client_certificate_used: bool | None
    request_artifact_ref: str | None
    request_artifact_recorded: bool
    request_artifact_present: bool
    response_artifact_ref: str | None
    response_artifact_recorded: bool
    response_artifact_present: bool
    intent_lineage_exact: bool
    intent_approval_level: str | None
    approval_reference_id: str | None
    approval_status: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TrafficSourcePage:
    snapshot: TrafficSnapshotSource
    items: tuple[TrafficExchangeSource, ...]
    has_more: bool


class _TrafficModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TrafficScope(_TrafficModel):
    run_id: str
    engagement_id: str

    @field_validator("run_id", "engagement_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_id(value)


class TrafficLineage(_TrafficModel):
    run_id: str
    session_id: str
    tool_call_id: str
    node_id: str
    node_status: NodeStatus

    @field_validator("run_id", "session_id", "tool_call_id", "node_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_id(value)


class TrafficUrlSummary(_TrafficModel):
    availability: TrafficAvailability
    scheme: str | None
    origin: str | None
    path_shape: str | None
    path_segment_count: int | None = Field(default=None, ge=0, le=4096)
    redacted: bool = True

    @model_validator(mode="after")
    def validate_availability(self) -> TrafficUrlSummary:
        values = (self.scheme, self.origin, self.path_shape, self.path_segment_count)
        if self.availability is TrafficAvailability.UNAVAILABLE:
            if any(value is not None for value in values):
                raise ValueError("Unavailable Traffic URL summaries cannot carry values")
            return self
        if (
            self.scheme not in {"http", "https"}
            or self.origin is None
            or not _is_safe_origin(self.origin, expected_scheme=self.scheme)
            or self.path_shape not in {"/", "/…"}
            or self.path_segment_count is None
        ):
            raise ValueError("Traffic URL summary is invalid")
        return self


class TrafficResponseMetadata(_TrafficModel):
    status_code: int = Field(ge=100, le=599)
    status_class: TrafficStatusClass
    elapsed_ms: int = Field(ge=0)
    content_type: str | None = Field(default=None, max_length=192)
    content_length: int | None = Field(default=None, ge=0)
    truncated: bool

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, value: str | None) -> str | None:
        if value is not None and (
            _MEDIA_TYPE.fullmatch(value) is None or value not in TRAFFIC_SAFE_MEDIA_TYPES
        ):
            raise ValueError("Traffic media type is invalid")
        return value

    @model_validator(mode="after")
    def validate_status_class(self) -> TrafficResponseMetadata:
        expected = {
            1: TrafficStatusClass.INFORMATIONAL,
            2: TrafficStatusClass.SUCCESS,
            3: TrafficStatusClass.REDIRECT,
            4: TrafficStatusClass.CLIENT_ERROR,
            5: TrafficStatusClass.SERVER_ERROR,
        }[self.status_code // 100]
        if self.status_class is not expected:
            raise ValueError("Traffic response status class is invalid")
        return self


class TrafficTlsSummary(_TrafficModel):
    availability: TrafficTlsAvailability
    verified: bool | None
    client_certificate_used: bool | None

    @model_validator(mode="after")
    def validate_summary(self) -> TrafficTlsSummary:
        if self.availability is TrafficTlsAvailability.AVAILABLE:
            if type(self.verified) is not bool or type(self.client_certificate_used) is not bool:
                raise ValueError("Available Traffic TLS metadata requires booleans")
        elif self.verified is not None or self.client_certificate_used is not None:
            raise ValueError("Unavailable Traffic TLS metadata cannot carry values")
        return self


class TrafficArtifactMetadata(_TrafficModel):
    opaque_ref: str | None
    presence: TrafficArtifactPresence
    access: TrafficMetadataAccess = TrafficMetadataAccess.METADATA_ONLY

    @model_validator(mode="after")
    def validate_ref(self) -> TrafficArtifactMetadata:
        if self.presence is TrafficArtifactPresence.NOT_RECORDED:
            if self.opaque_ref is not None:
                raise ValueError("Unrecorded Traffic Artifact cannot have a reference")
            return self
        if self.opaque_ref is None or _OPAQUE_ARTIFACT_REF.fullmatch(self.opaque_ref) is None:
            raise ValueError("Traffic Artifact reference is invalid")
        return self


class TrafficArtifactSet(_TrafficModel):
    request: TrafficArtifactMetadata
    response: TrafficArtifactMetadata


class TrafficBodyMetadata(_TrafficModel):
    availability: TrafficBodyAvailability
    revealable: bool = False
    truncated: bool

    @field_validator("revealable")
    @classmethod
    def reject_reveal(cls, value: bool) -> bool:
        if value:
            raise ValueError("Traffic body reveal is unavailable")
        return value


class TrafficBodySet(_TrafficModel):
    request: TrafficBodyMetadata
    response: TrafficBodyMetadata


class TrafficRedirectSummary(_TrafficModel):
    availability: TrafficAvailability
    count: int | None = Field(default=None, ge=0, le=10)
    followed: bool | None
    origins: tuple[str, ...]
    partial: bool

    @model_validator(mode="after")
    def validate_summary(self) -> TrafficRedirectSummary:
        if self.availability is TrafficAvailability.UNAVAILABLE:
            if self.count is not None or self.followed is not None or self.origins:
                raise ValueError("Unavailable redirect summary cannot carry values")
            if not self.partial:
                raise ValueError("Unavailable redirect summary must be partial")
            return self
        if self.count is None or self.followed is not (self.count > 0):
            raise ValueError("Traffic redirect count is invalid")
        if len(self.origins) != self.count:
            raise ValueError("Traffic redirect origins do not match redirect count")
        for origin in self.origins:
            if not _is_safe_origin(origin):
                raise ValueError("Traffic redirect origin is invalid")
        if self.partial:
            raise ValueError("Available redirect metadata cannot be partial")
        return self


class TrafficReplayReference(_TrafficModel):
    availability: TrafficAvailability = TrafficAvailability.UNAVAILABLE
    request_id: None = None
    reason: str = "not_persisted"


class TrafficCreatedBy(_TrafficModel):
    availability: TrafficAvailability
    kind: TrafficCreatedByKind

    @model_validator(mode="after")
    def validate_kind(self) -> TrafficCreatedBy:
        if self.availability is TrafficAvailability.AVAILABLE:
            if self.kind is not TrafficCreatedByKind.AGENT_RUNTIME:
                raise ValueError("Available creator must be server-derived")
        elif self.kind is not TrafficCreatedByKind.UNKNOWN:
            raise ValueError("Unavailable creator must be unknown")
        return self


class TrafficScopeDecision(_TrafficModel):
    availability: TrafficAvailability = TrafficAvailability.UNAVAILABLE
    decision: None = None
    reference_kind: str = "run_scope"
    reason: str = "decision_not_persisted"


class TrafficApprovalReference(_TrafficModel):
    availability: TrafficApprovalAvailability
    reference_id: str | None
    status: ApprovalStatus | None

    @model_validator(mode="after")
    def validate_reference(self) -> TrafficApprovalReference:
        if self.availability is TrafficApprovalAvailability.AVAILABLE:
            if self.reference_id is None or self.status is None:
                raise ValueError("Available approval metadata requires identity and status")
            _validate_id(self.reference_id)
        elif self.reference_id is not None or self.status is not None:
            raise ValueError("Unavailable approval metadata cannot carry values")
        return self


class TrafficSafetyGateReference(_TrafficModel):
    availability: TrafficAvailability = TrafficAvailability.UNAVAILABLE
    reference_id: None = None
    reason: str = "not_implemented"


class TrafficGovernance(_TrafficModel):
    sensitivity: TrafficSensitivityClass = TrafficSensitivityClass.RESTRICTED_SENSITIVE
    access: TrafficMetadataAccess = TrafficMetadataAccess.METADATA_ONLY
    retention: TrafficRetentionState = TrafficRetentionState.LEGACY_UNMANAGED
    reveal_capability: TrafficRevealCapability = TrafficRevealCapability.DISABLED


class TrafficExchangeView(_TrafficModel):
    exchange_id: str
    request_id: str
    execution_key: str = Field(min_length=1, max_length=255)
    canonical_request_digest: str
    digest_stability: TrafficDigestStability = TrafficDigestStability.SERVER_INSTANCE
    lineage: TrafficLineage
    method: str
    url_summary: TrafficUrlSummary
    tls: TrafficTlsSummary
    response: TrafficResponseMetadata
    artifacts: TrafficArtifactSet
    body: TrafficBodySet
    redirect: TrafficRedirectSummary
    replay_of: TrafficReplayReference
    created_by: TrafficCreatedBy
    created_at: AwareDatetime
    scope_decision: TrafficScopeDecision
    approval: TrafficApprovalReference
    safety_gate: TrafficSafetyGateReference
    governance: TrafficGovernance
    projection_quality: TrafficProjectionQuality
    partial_reasons: tuple[str, ...]

    @field_validator("exchange_id", "request_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_id(value)

    @field_validator("canonical_request_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("Traffic request digest is invalid")
        return value

    @field_validator("method")
    @classmethod
    def validate_method(cls, value: str) -> str:
        if not 1 <= len(value) <= 32 or not value.isascii():
            raise ValueError("Traffic method is invalid")
        if not value.replace("-", "").isalpha() or value != value.upper():
            raise ValueError("Traffic method is invalid")
        return value

    @field_validator("execution_key")
    @classmethod
    def validate_execution_key(cls, value: str) -> str:
        return _validate_safe_text(value, maximum=255)

    @field_validator("partial_reasons")
    @classmethod
    def validate_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None:
                raise ValueError("Traffic partial reason is invalid")
        if tuple(sorted(set(values))) != values:
            raise ValueError("Traffic partial reasons must be unique and sorted")
        return values

    @model_validator(mode="after")
    def validate_quality(self) -> TrafficExchangeView:
        if self.exchange_id != self.request_id:
            raise ValueError("Traffic request and exchange identity must match")
        expected = (
            TrafficProjectionQuality.PARTIAL
            if self.partial_reasons
            else TrafficProjectionQuality.EXACT
        )
        if self.projection_quality is not expected:
            raise ValueError("Traffic projection quality does not match partial reasons")
        return self


class TrafficSnapshot(_TrafficModel):
    id: str
    created_through: AwareDatetime | None
    stale: bool = False

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("Traffic snapshot ID is invalid")
        return value


class TrafficExchangePage(_TrafficModel):
    scope: TrafficScope
    snapshot: TrafficSnapshot
    items: tuple[TrafficExchangeView, ...]
    truncated: bool
    has_more: bool
    next_cursor: str | None = Field(default=None, max_length=4096)
    partial: bool
    partial_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def validate_page(self) -> TrafficExchangePage:
        reasons = tuple(sorted({reason for item in self.items for reason in item.partial_reasons}))
        if self.partial != bool(reasons) or self.partial_reasons != reasons:
            raise ValueError("Traffic page partial state is invalid")
        if any(item.lineage.run_id != self.scope.run_id for item in self.items):
            raise ValueError("Traffic page contains a foreign Run item")
        if self.has_more is not (self.next_cursor is not None):
            raise ValueError("Traffic page cursor state is invalid")
        if self.truncated:
            raise ValueError("Traffic source truncation is not used by this projection")
        return self


class TrafficExchangeDetail(_TrafficModel):
    scope: TrafficScope
    item: TrafficExchangeView

    @model_validator(mode="after")
    def validate_scope(self) -> TrafficExchangeDetail:
        if self.item.lineage.run_id != self.scope.run_id:
            raise ValueError("Traffic detail contains a foreign Run item")
        return self


class InvalidTrafficCursorError(ValueError):
    """The cursor failed shape, binding, or signature validation."""


class StaleTrafficCursorError(RuntimeError):
    """The immutable snapshot described by a valid cursor changed."""


class TrafficSourceContractError(RuntimeError):
    """Persistence returned data outside the metadata-only contract."""


def _validate_id(value: str) -> str:
    return _validate_safe_text(value, maximum=256)


def _validate_safe_text(value: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("Traffic text is invalid")
    return value


def _is_safe_origin(value: str, *, expected_scheme: str | None = None) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or (expected_scheme is not None and parsed.scheme != expected_scheme)
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    default_port = 443 if parsed.scheme == "https" else 80
    rendered_host = parsed.hostname.lower().rstrip(".")
    if ":" in rendered_host:
        rendered_host = f"[{rendered_host}]"
    rendered = f"{parsed.scheme}://{rendered_host}"
    if port is not None and port != default_port:
        rendered = f"{rendered}:{port}"
    return rendered == value


__all__ = [
    name
    for name in globals()
    if name.startswith("Traffic") or name.startswith("Invalid") or name.startswith("Stale")
]
