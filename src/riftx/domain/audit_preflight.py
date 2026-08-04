"""Strict, infrastructure-independent contracts for Code Audit preflight jobs.

The preflight owner is deliberately independent from every Run-scoped protocol.
This module contains only bounded facts and canonical digests; filesystem admission,
database compare-and-swap, and capsule execution remain infrastructure concerns.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .audit import AuditMode, SourceTargetKind
from .base import DomainModel, new_id, utc_now
from .errors import InvalidStateTransitionError
from .runner import RunnerPrincipal

AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION: Literal["riftx.audit-preflight-request/v1"] = (
    "riftx.audit-preflight-request/v1"
)
AUDIT_PREFLIGHT_JOB_SCHEMA_VERSION: Literal["riftx.audit-preflight-job/v1"] = (
    "riftx.audit-preflight-job/v1"
)
AUDIT_PREFLIGHT_EFFECT_OWNER_SCHEMA_VERSION: Literal["riftx.audit-preflight-effect-owner/v1"] = (
    "riftx.audit-preflight-effect-owner/v1"
)
AUDIT_PREFLIGHT_LEASE_ENVELOPE_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-lease-envelope/v1"
] = "riftx.audit-preflight-lease-envelope/v1"
AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION: Literal["riftx.audit-preflight-result/v1"] = (
    "riftx.audit-preflight-result/v1"
)
AUDIT_PREFLIGHT_CAPABILITY_MATRIX_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-capability-matrix/v1"
] = "riftx.audit-preflight-capability-matrix/v1"
AUDIT_PREFLIGHT_MINIMUM_BUDGET_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-minimum-feasible-budget/v1"
] = "riftx.audit-preflight-minimum-feasible-budget/v1"
AUDIT_PREFLIGHT_STOP_RECEIPT_SCHEMA_VERSION: Literal["riftx.audit-preflight-stop-receipt/v1"] = (
    "riftx.audit-preflight-stop-receipt/v1"
)
AUDIT_PREFLIGHT_EXIT_RECEIPT_SCHEMA_VERSION: Literal["riftx.audit-preflight-exit-receipt/v1"] = (
    "riftx.audit-preflight-exit-receipt/v1"
)
AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY: Literal["preflight_job_owner_v1"] = "preflight_job_owner_v1"
AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID: Literal["riftx.audit-empty-security-context/v1"] = (
    "riftx.audit-empty-security-context/v1"
)

MAX_PREFLIGHT_REPOSITORY_PATH_BYTES = 4_096
MAX_PREFLIGHT_RELATIVE_PATH_BYTES = 4_096
MAX_PREFLIGHT_RELATIVE_PATHS = 512
MAX_PREFLIGHT_RELATIVE_PATH_TOTAL_BYTES = 64 * 1_024
MAX_PREFLIGHT_RESTRICTED_REQUEST_BYTES = 128 * 1_024
MAX_PREFLIGHT_RESULT_BYTES = 256 * 1_024
MAX_PREFLIGHT_CAPABILITY_FACTS = 128
MAX_PREFLIGHT_LANGUAGE_ESTIMATES = 256
MAX_PREFLIGHT_SAFE_CODES = 128
MAX_PREFLIGHT_COUNTER = 2**63 - 1

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$"
_SHORT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,63}$"
_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+:/\-]{0,127}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_DIGEST_OR_EMPTY_PATTERN = r"^(?:|[0-9a-f]{64})$"
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_UNC_SERVER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")

type PreflightId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_ID_PATTERN),
]
type PreflightShortId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=_SHORT_ID_PATTERN),
]
type PreflightVersion = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_VERSION_PATTERN),
]
type PreflightDigest = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=_DIGEST_PATTERN),
]
type PreflightSafeCode = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_ID_PATTERN),
]
type GitObjectId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=40,
        max_length=64,
        pattern=_GIT_OBJECT_ID_PATTERN,
    ),
]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _domain_digest(domain: str, value: object) -> str:
    canonical = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + canonical).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key is not canonical")
        result[key] = value
    return result


def _parse_canonical_json_object(value: str, *, maximum_bytes: int, label: str) -> object:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{label} exceeds the {maximum_bytes}-byte limit")
    try:
        parsed = json.loads(value, object_pairs_hook=_reject_duplicate_json_keys)
        canonical = _canonical_json(parsed)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc
    if not isinstance(parsed, dict) or canonical != value:
        raise ValueError(f"{label} is not a canonical JSON object")
    return parsed


def _validate_bounded_text(value: str, *, label: str, maximum_bytes: int) -> str:
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    if not value:
        raise ValueError(f"{label} must not be empty")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must use NFC normalization")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{label} must not contain control characters")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds the {maximum_bytes}-byte limit")
    return value


def _validate_repository_path(value: str) -> str:
    _validate_bounded_text(
        value,
        label="repository_path",
        maximum_bytes=MAX_PREFLIGHT_REPOSITORY_PATH_BYTES,
    )
    if value.startswith("~"):
        raise ValueError("repository_path must not use home-directory expansion")
    if "\\" in value:
        raise ValueError("repository_path must use canonical forward slashes")

    if _WINDOWS_DRIVE_PREFIX.match(value) is not None:
        if not value[0].isupper() or not value.startswith(f"{value[0]}:/"):
            raise ValueError("repository_path must use an uppercase canonical Windows drive")
        if len(value) > 3 and value.endswith("/"):
            raise ValueError("repository_path must not have a trailing separator")
        if "//" in value[2:]:
            raise ValueError("repository_path must not contain duplicate separators")
        components = value[3:].split("/") if len(value) > 3 else []
    elif value.startswith("//"):
        if value.endswith("/") or "//" in value[2:]:
            raise ValueError("repository_path must use canonical UNC separators")
        components = value[2:].split("/")
        if (
            len(components) < 2
            or not all(components)
            or _UNC_SERVER_PATTERN.fullmatch(components[0]) is None
        ):
            raise ValueError("repository_path must use a lowercase canonical UNC root")
    elif value.startswith("/"):
        if value != "/" and (value.endswith("/") or "//" in value):
            raise ValueError("repository_path must use canonical POSIX separators")
        components = value[1:].split("/") if value != "/" else []
    else:
        raise ValueError("repository_path must be an absolute node-local path")

    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("repository_path must not contain empty or dot segments")
    return value


def _validate_relative_path(value: str) -> str:
    _validate_bounded_text(
        value,
        label="repository-relative path",
        maximum_bytes=MAX_PREFLIGHT_RELATIVE_PATH_BYTES,
    )
    if value.startswith(("/", "~")) or "\\" in value or _WINDOWS_DRIVE_PREFIX.match(value):
        raise ValueError("repository-relative path must not be absolute or platform-relative")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("repository-relative path must not contain empty or dot segments")
    return value


def _validate_sorted_unique_strings(
    values: tuple[str, ...],
    *,
    label: str,
    maximum_items: int,
) -> tuple[str, ...]:
    if len(values) > maximum_items:
        raise ValueError(f"{label} exceeds the {maximum_items}-item limit")
    if values != tuple(sorted(values)):
        raise ValueError(f"{label} must use canonical sorted order")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
    return values


def _validate_client_request_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("client_request_id must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError("client_request_id must be a non-zero canonical UUID")
    return value


class AuditPreflightStrictModel(DomainModel):
    """Fail-closed immutable base for every preflight fact."""

    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
        strict=True,
        validate_default=True,
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update:
            raise TypeError("preflight models forbid unvalidated model_copy updates")
        return super().model_copy(deep=deep)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        del include, exclude, update, deep
        raise TypeError("preflight models forbid deprecated copy")


class AuditPreflightSourceExecutionTarget(AuditPreflightStrictModel):
    node_id: Literal["local"] = "local"
    source_ingest_backend: PreflightId


class AuditPreflightTarget(AuditPreflightStrictModel):
    kind: SourceTargetKind
    revision: str = Field(min_length=1, max_length=1_024)
    base_revision: str | None = Field(default=None, min_length=1, max_length=1_024)
    include_untracked: bool = False

    @field_validator("revision", "base_revision")
    @classmethod
    def validate_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validate_bounded_text(value, label="revision", maximum_bytes=1_024)
        if value.startswith("-"):
            raise ValueError("revision must not be option-shaped")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> AuditPreflightTarget:
        if self.kind is SourceTargetKind.REVISION and self.include_untracked:
            raise ValueError("revision target cannot include untracked files")
        if self.base_revision is not None and self.base_revision == self.revision:
            raise ValueError("base_revision and revision must identify different targets")
        return self


class AuditPreflightSecurityContext(AuditPreflightStrictModel):
    """The only context accepted before the bounded context milestone."""

    input_id: None = None
    repository_paths: tuple[()] = ()
    discover_defaults: Literal[False] = False

    @property
    def context_id(self) -> str:
        return AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID

    @property
    def context_digest(self) -> str:
        return AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST


class PreflightRequest(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-request/v1"] = (
        AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION
    )
    client_request_id: str = Field(min_length=36, max_length=36)
    repository_path: str = Field(
        min_length=1,
        max_length=MAX_PREFLIGHT_REPOSITORY_PATH_BYTES,
        repr=False,
    )
    source_execution_target: AuditPreflightSourceExecutionTarget
    target: AuditPreflightTarget
    include_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PREFLIGHT_RELATIVE_PATHS,
    )
    exclude_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PREFLIGHT_RELATIVE_PATHS,
    )
    security_context: AuditPreflightSecurityContext = Field(
        default_factory=AuditPreflightSecurityContext
    )
    mode: AuditMode = AuditMode.STANDARD

    @field_validator("client_request_id")
    @classmethod
    def validate_client_request_id(cls, value: str) -> str:
        return _validate_client_request_id(value)

    @field_validator("repository_path")
    @classmethod
    def validate_repository_path(cls, value: str) -> str:
        return _validate_repository_path(value)

    @field_validator("include_paths", "exclude_paths")
    @classmethod
    def validate_relative_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_relative_path(value)
        _validate_sorted_unique_strings(
            values,
            label="repository-relative paths",
            maximum_items=MAX_PREFLIGHT_RELATIVE_PATHS,
        )
        total_bytes = sum(len(value.encode("utf-8")) for value in values)
        if total_bytes > MAX_PREFLIGHT_RELATIVE_PATH_TOTAL_BYTES:
            raise ValueError("repository-relative paths exceed their total byte limit")
        return values

    @model_validator(mode="after")
    def validate_request(self) -> PreflightRequest:
        if self.mode is AuditMode.DIFF:
            if self.target.base_revision is None:
                raise ValueError("diff mode requires base_revision")
        elif self.target.base_revision is not None:
            raise ValueError("only diff mode may carry base_revision")
        overlap = set(self.include_paths).intersection(self.exclude_paths)
        if overlap:
            raise ValueError("include_paths and exclude_paths must not contain the same path")
        return self

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))

    def request_identity_json(self) -> str:
        payload = self.model_dump(mode="json", exclude={"client_request_id"})
        return _canonical_json(payload)

    @property
    def request_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"client_request_id"})
        return _domain_digest(AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION, payload)


def audit_preflight_request_digest(request: PreflightRequest) -> str:
    return request.request_digest


_EMPTY_CONTEXT_PAYLOAD: dict[str, object] = {
    "discover_defaults": False,
    "input_id": None,
    "repository_paths": [],
    "schema_version": AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID,
}
AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST: PreflightDigest = _domain_digest(
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID,
    _EMPTY_CONTEXT_PAYLOAD,
)


def _validate_canonical_empty_context_digest(value: str) -> str:
    if not hmac.compare_digest(
        value,
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST,
    ):
        raise ValueError("canonical empty-context digest does not match")
    return value


class AuditPreflightJobStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class AuditPreflightCapabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    BLOCKING = "blocking"


class AuditPreflightBudgetStatus(StrEnum):
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"
    BLOCKING = "blocking"


class AuditPreflightStopDisposition(StrEnum):
    STOPPED = "stopped"
    NEVER_CREATED = "never_created"


class AuditPreflightObservedTerminalState(StrEnum):
    EXITED = "exited"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_CREATED = "not_created"


class AuditPreflightExitTerminalState(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


class AuditPreflightLanguageEstimate(AuditPreflightStrictModel):
    language_id: PreflightSafeCode
    file_count: int = Field(strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)
    total_bytes: int = Field(strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)

    @property
    def identity(self) -> str:
        return self.language_id


class AuditPreflightCapabilityFact(AuditPreflightStrictModel):
    capability_id: PreflightSafeCode
    status: AuditPreflightCapabilityStatus
    component_version: PreflightVersion | None = None
    component_digest: PreflightDigest | None = None
    proof_digest: PreflightDigest | None = None
    reason_code: PreflightSafeCode | None = None

    @model_validator(mode="after")
    def validate_fact(self) -> AuditPreflightCapabilityFact:
        implementation = (
            self.component_version,
            self.component_digest,
            self.proof_digest,
        )
        if self.status is AuditPreflightCapabilityStatus.AVAILABLE:
            if not all(value is not None for value in implementation):
                raise ValueError("available capability requires version, component, and proof")
            if self.reason_code is not None:
                raise ValueError("available capability cannot carry an unavailable reason")
        else:
            if any(value is not None for value in implementation):
                raise ValueError("unavailable capability cannot claim implementation proof")
            if self.reason_code is None:
                raise ValueError("unavailable capability requires a safe reason_code")
        return self

    @property
    def identity(self) -> str:
        return self.capability_id


class AuditPreflightCapabilityMatrix(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-capability-matrix/v1"] = (
        AUDIT_PREFLIGHT_CAPABILITY_MATRIX_SCHEMA_VERSION
    )
    entries: tuple[AuditPreflightCapabilityFact, ...] = Field(
        min_length=1,
        max_length=MAX_PREFLIGHT_CAPABILITY_FACTS,
    )
    matrix_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @field_validator("entries")
    @classmethod
    def validate_entries(
        cls,
        values: tuple[AuditPreflightCapabilityFact, ...],
    ) -> tuple[AuditPreflightCapabilityFact, ...]:
        identities = tuple(value.identity for value in values)
        _validate_sorted_unique_strings(
            identities,
            label="preflight capability facts",
            maximum_items=MAX_PREFLIGHT_CAPABILITY_FACTS,
        )
        return values

    @model_validator(mode="after")
    def validate_digest(self) -> AuditPreflightCapabilityMatrix:
        expected = audit_preflight_capability_matrix_digest(self)
        if self.matrix_digest:
            if not hmac.compare_digest(self.matrix_digest, expected):
                raise ValueError("preflight capability matrix digest does not match")
        else:
            object.__setattr__(self, "matrix_digest", expected)
        return self


def audit_preflight_capability_matrix_digest(
    matrix: AuditPreflightCapabilityMatrix,
) -> str:
    payload = matrix.model_dump(mode="json", exclude={"matrix_digest"})
    return _domain_digest(AUDIT_PREFLIGHT_CAPABILITY_MATRIX_SCHEMA_VERSION, payload)


class AuditPreflightMinimumFeasibleBudget(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-minimum-feasible-budget/v1"] = (
        AUDIT_PREFLIGHT_MINIMUM_BUDGET_SCHEMA_VERSION
    )
    status: AuditPreflightBudgetStatus
    minimum_wall_seconds: int | None = Field(default=None, strict=True, ge=1, le=86_400)
    maximum_wall_seconds: int | None = Field(default=None, strict=True, ge=1, le=86_400)
    minimum_read_bytes: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=MAX_PREFLIGHT_COUNTER,
    )
    maximum_read_bytes: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=MAX_PREFLIGHT_COUNTER,
    )
    minimum_work_items: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=MAX_PREFLIGHT_COUNTER,
    )
    maximum_work_items: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=MAX_PREFLIGHT_COUNTER,
    )
    provenance_digest: PreflightDigest
    reason_code: PreflightSafeCode | None = None

    @model_validator(mode="after")
    def validate_budget(self) -> AuditPreflightMinimumFeasibleBudget:
        estimates = (
            self.minimum_wall_seconds,
            self.maximum_wall_seconds,
            self.minimum_read_bytes,
            self.maximum_read_bytes,
            self.minimum_work_items,
            self.maximum_work_items,
        )
        if self.status is AuditPreflightBudgetStatus.ESTIMATED:
            if not all(value is not None for value in estimates):
                raise ValueError("estimated minimum budget requires every bounded interval")
            if self.reason_code is not None:
                raise ValueError("estimated minimum budget cannot carry an unavailable reason")
            assert self.minimum_wall_seconds is not None
            assert self.maximum_wall_seconds is not None
            assert self.minimum_read_bytes is not None
            assert self.maximum_read_bytes is not None
            assert self.minimum_work_items is not None
            assert self.maximum_work_items is not None
            if self.minimum_wall_seconds > self.maximum_wall_seconds:
                raise ValueError("minimum wall estimate must not exceed maximum")
            if self.minimum_read_bytes > self.maximum_read_bytes:
                raise ValueError("minimum read estimate must not exceed maximum")
            if self.minimum_work_items > self.maximum_work_items:
                raise ValueError("minimum work estimate must not exceed maximum")
        else:
            if any(value is not None for value in estimates):
                raise ValueError("unavailable minimum budget cannot contain invented estimates")
            if self.reason_code is None:
                raise ValueError("unavailable minimum budget requires a safe reason_code")
        return self


class AuditPreflightEffectOwner(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-effect-owner/v1"] = (
        AUDIT_PREFLIGHT_EFFECT_OWNER_SCHEMA_VERSION
    )
    job_id: PreflightId
    operator_principal_id: PreflightId
    authorization_scope_digest: PreflightDigest
    source_node_id: Literal["local"] = "local"
    source_root_identity_digest: PreflightDigest
    request_schema_version: Literal["riftx.audit-preflight-request/v1"] = (
        AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION
    )
    request_digest: PreflightDigest
    backend_id: PreflightId
    image_digest: PreflightDigest
    policy_digest: PreflightDigest
    created_at: AwareDatetime
    expires_at: AwareDatetime
    effect_owner_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @model_validator(mode="after")
    def validate_owner(self) -> AuditPreflightEffectOwner:
        if self.expires_at <= self.created_at:
            raise ValueError("preflight effect owner must expire after creation")
        expected = audit_preflight_effect_owner_digest(self)
        if self.effect_owner_digest:
            if not hmac.compare_digest(self.effect_owner_digest, expected):
                raise ValueError("preflight effect owner digest does not match")
        else:
            object.__setattr__(self, "effect_owner_digest", expected)
        return self

    @classmethod
    def from_request(
        cls,
        *,
        job_id: str,
        operator_principal_id: str,
        authorization_scope_digest: str,
        source_root_identity_digest: str,
        request: PreflightRequest,
        backend_id: str,
        image_digest: str,
        policy_digest: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> AuditPreflightEffectOwner:
        if request.source_execution_target.source_ingest_backend != backend_id:
            raise ValueError("preflight backend does not match the frozen request")
        return cls(
            job_id=job_id,
            operator_principal_id=operator_principal_id,
            authorization_scope_digest=authorization_scope_digest,
            source_node_id=request.source_execution_target.node_id,
            source_root_identity_digest=source_root_identity_digest,
            request_schema_version=request.schema_version,
            request_digest=request.request_digest,
            backend_id=backend_id,
            image_digest=image_digest,
            policy_digest=policy_digest,
            created_at=created_at,
            expires_at=expires_at,
        )


def audit_preflight_effect_owner_digest(owner: AuditPreflightEffectOwner) -> str:
    payload = owner.model_dump(mode="json", exclude={"effect_owner_digest"})
    return _domain_digest(AUDIT_PREFLIGHT_EFFECT_OWNER_SCHEMA_VERSION, payload)


class AuditPreflightLeaseEnvelope(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-lease-envelope/v1"] = (
        AUDIT_PREFLIGHT_LEASE_ENVELOPE_SCHEMA_VERSION
    )
    owner: AuditPreflightEffectOwner
    runner_principal: RunnerPrincipal
    lease_id: PreflightId
    lease_expires_at: AwareDatetime
    expected_state_version: int = Field(strict=True, ge=1, le=MAX_PREFLIGHT_COUNTER)
    output_contract_digest: PreflightDigest
    lease_envelope_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @model_validator(mode="after")
    def validate_lease(self) -> AuditPreflightLeaseEnvelope:
        if not self.owner.created_at < self.lease_expires_at <= self.owner.expires_at:
            raise ValueError("preflight lease expiry must be inside the owner lifetime")
        expected = audit_preflight_lease_envelope_digest(self)
        if self.lease_envelope_digest:
            if not hmac.compare_digest(self.lease_envelope_digest, expected):
                raise ValueError("preflight lease envelope digest does not match")
        else:
            object.__setattr__(self, "lease_envelope_digest", expected)
        return self


def audit_preflight_lease_envelope_digest(envelope: AuditPreflightLeaseEnvelope) -> str:
    encoded = envelope.model_dump(mode="json")
    payload = {
        "schema_version": envelope.schema_version,
        "effect_owner_digest": envelope.owner.effect_owner_digest,
        "runner_principal": envelope.runner_principal.model_dump(mode="json"),
        "lease_id": envelope.lease_id,
        "lease_expires_at": encoded["lease_expires_at"],
        "expected_state_version": envelope.expected_state_version,
        "output_contract_digest": envelope.output_contract_digest,
    }
    return _domain_digest(AUDIT_PREFLIGHT_LEASE_ENVELOPE_SCHEMA_VERSION, payload)


class AuditPreflightResult(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-result/v1"] = (
        AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION
    )
    result_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )
    preflight_job_id: PreflightId
    request_digest: PreflightDigest
    effect_owner_digest: PreflightDigest
    source_node_id: Literal["local"] = "local"
    source_root_identity_digest: PreflightDigest
    repository_identity_digest: PreflightDigest
    content_identity_digest: PreflightDigest
    backend_id: PreflightId
    image_digest: PreflightDigest
    policy_digest: PreflightDigest
    capsule_prepare_proof_digest: PreflightDigest
    target_kind: SourceTargetKind
    revision: str = Field(min_length=1, max_length=1_024)
    base_revision: str | None = Field(default=None, min_length=1, max_length=1_024)
    mode: AuditMode
    include_untracked: bool
    head_revision: GitObjectId | None = None
    resolved_revision: GitObjectId | None = None
    resolved_base_revision: GitObjectId | None = None
    merge_base_revision: GitObjectId | None = None
    dirty: bool
    staged: bool
    unstaged: bool
    untracked: bool
    file_count: int = Field(strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)
    total_bytes: int = Field(strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)
    max_file_bytes: int = Field(strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)
    language_estimates: tuple[AuditPreflightLanguageEstimate, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PREFLIGHT_LANGUAGE_ESTIMATES,
    )
    capability_matrix: AuditPreflightCapabilityMatrix
    capability_warnings: tuple[PreflightSafeCode, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PREFLIGHT_SAFE_CODES,
    )
    blocking_errors: tuple[PreflightSafeCode, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PREFLIGHT_SAFE_CODES,
    )
    minimum_feasible_budget: AuditPreflightMinimumFeasibleBudget
    canonical_empty_context_id: Literal["riftx.audit-empty-security-context/v1"] = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    )
    canonical_empty_context_digest: PreflightDigest = AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    completed_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("revision", "base_revision")
    @classmethod
    def validate_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validate_bounded_text(value, label="revision", maximum_bytes=1_024)
        if value.startswith("-"):
            raise ValueError("revision must not be option-shaped")
        return value

    @field_validator("language_estimates")
    @classmethod
    def validate_language_estimates(
        cls,
        values: tuple[AuditPreflightLanguageEstimate, ...],
    ) -> tuple[AuditPreflightLanguageEstimate, ...]:
        identities = tuple(value.identity for value in values)
        _validate_sorted_unique_strings(
            identities,
            label="preflight language estimates",
            maximum_items=MAX_PREFLIGHT_LANGUAGE_ESTIMATES,
        )
        return values

    @field_validator("capability_warnings", "blocking_errors")
    @classmethod
    def validate_safe_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_sorted_unique_strings(
            values,
            label="preflight safe codes",
            maximum_items=MAX_PREFLIGHT_SAFE_CODES,
        )

    @field_validator("canonical_empty_context_digest")
    @classmethod
    def validate_empty_context_digest(cls, value: str) -> str:
        return _validate_canonical_empty_context_digest(value)

    @model_validator(mode="after")
    def validate_result(self) -> AuditPreflightResult:
        if self.expires_at <= self.completed_at:
            raise ValueError("preflight result must expire after completion")
        if self.target_kind is SourceTargetKind.REVISION and self.include_untracked:
            raise ValueError("revision result cannot include untracked files")
        if self.mode is AuditMode.DIFF:
            if self.base_revision is None or self.base_revision == self.revision:
                raise ValueError("diff result requires distinct base and head revisions")
        elif self.base_revision is not None:
            raise ValueError("non-diff result cannot carry base_revision")
        if self.dirty != any((self.staged, self.unstaged, self.untracked)):
            raise ValueError("dirty must exactly summarize staged, unstaged, and untracked")
        if self.max_file_bytes > self.total_bytes:
            raise ValueError("max_file_bytes must not exceed total_bytes")
        if self.file_count == 0 and (self.total_bytes != 0 or self.max_file_bytes != 0):
            raise ValueError("empty preflight result cannot report repository bytes")
        if sum(item.file_count for item in self.language_estimates) > self.file_count:
            raise ValueError("language file estimates must not exceed file_count")
        if sum(item.total_bytes for item in self.language_estimates) > self.total_bytes:
            raise ValueError("language byte estimates must not exceed total_bytes")

        has_blocking_capability = any(
            entry.status is AuditPreflightCapabilityStatus.BLOCKING
            for entry in self.capability_matrix.entries
        )
        budget_blocking = self.minimum_feasible_budget.status is AuditPreflightBudgetStatus.BLOCKING
        if bool(self.blocking_errors) != budget_blocking:
            raise ValueError("blocking_errors and minimum budget blocking status must agree")
        if has_blocking_capability and not self.blocking_errors:
            raise ValueError("blocking capability requires a bounded blocking error")

        if not self.blocking_errors:
            if self.resolved_revision is None:
                raise ValueError("feasible preflight result requires resolved_revision")
            if self.head_revision is None:
                raise ValueError("feasible preflight result requires head_revision")
            if self.mode is AuditMode.DIFF and (
                self.resolved_base_revision is None or self.merge_base_revision is None
            ):
                raise ValueError("feasible diff result requires resolved base and merge-base")
        if self.mode is not AuditMode.DIFF and any(
            value is not None for value in (self.resolved_base_revision, self.merge_base_revision)
        ):
            raise ValueError("non-diff result cannot carry resolved base or merge-base")

        expected = audit_preflight_result_digest(self)
        if self.result_digest:
            if not hmac.compare_digest(self.result_digest, expected):
                raise ValueError("preflight result digest does not match")
        else:
            object.__setattr__(self, "result_digest", expected)
        canonical = _canonical_json(self.model_dump(mode="json"))
        if len(canonical.encode("utf-8")) > MAX_PREFLIGHT_RESULT_BYTES:
            raise ValueError("preflight result exceeds its canonical byte limit")
        return self

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))


def audit_preflight_result_digest(result: AuditPreflightResult) -> str:
    payload = result.model_dump(mode="json", exclude={"result_digest"})
    return _domain_digest(AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION, payload)


class AuditPreflightExitReceipt(AuditPreflightStrictModel):
    """Immutable proof that a created capsule reached an observed terminal state."""

    schema_version: Literal["riftx.audit-preflight-exit-receipt/v1"] = (
        AUDIT_PREFLIGHT_EXIT_RECEIPT_SCHEMA_VERSION
    )
    receipt_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )
    job_id: PreflightId
    effect_owner_digest: PreflightDigest
    lease_envelope_digest: PreflightDigest
    capsule_id: PreflightId
    source_node_id: Literal["local"] = "local"
    runner_principal: RunnerPrincipal
    backend_id: PreflightId
    image_digest: PreflightDigest
    policy_digest: PreflightDigest
    process_identity_digest: PreflightDigest
    result_digest: PreflightDigest | None = None
    terminal_state: AuditPreflightExitTerminalState
    received_at: AwareDatetime

    @model_validator(mode="after")
    def validate_receipt(self) -> AuditPreflightExitReceipt:
        has_result = self.result_digest is not None
        result_terminal = self.terminal_state in {
            AuditPreflightExitTerminalState.SUCCEEDED,
            AuditPreflightExitTerminalState.REJECTED,
        }
        if has_result != result_terminal:
            raise ValueError(
                "succeeded or rejected exit receipt requires exactly one result digest"
            )
        expected = audit_preflight_exit_receipt_digest(self)
        if self.receipt_digest:
            if not hmac.compare_digest(self.receipt_digest, expected):
                raise ValueError("preflight exit receipt digest does not match")
        else:
            object.__setattr__(self, "receipt_digest", expected)
        return self

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))


def audit_preflight_exit_receipt_digest(receipt: AuditPreflightExitReceipt) -> str:
    payload = receipt.model_dump(mode="json", exclude={"receipt_digest"})
    return _domain_digest(AUDIT_PREFLIGHT_EXIT_RECEIPT_SCHEMA_VERSION, payload)


class AuditPreflightStopReceipt(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-stop-receipt/v1"] = (
        AUDIT_PREFLIGHT_STOP_RECEIPT_SCHEMA_VERSION
    )
    receipt_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )
    job_id: PreflightId
    effect_owner_digest: PreflightDigest
    lease_envelope_digest: PreflightDigest
    capsule_id: PreflightId | None = None
    source_node_id: Literal["local"] = "local"
    runner_principal: RunnerPrincipal
    backend_id: PreflightId
    image_digest: PreflightDigest
    policy_digest: PreflightDigest
    disposition: AuditPreflightStopDisposition
    process_identity_digest: PreflightDigest | None = None
    never_created_proof_digest: PreflightDigest | None = None
    observed_terminal_state: AuditPreflightObservedTerminalState
    received_at: AwareDatetime

    @model_validator(mode="after")
    def validate_receipt(self) -> AuditPreflightStopReceipt:
        if self.disposition is AuditPreflightStopDisposition.STOPPED:
            if self.capsule_id is None or self.process_identity_digest is None:
                raise ValueError("stopped receipt requires capsule and process identity")
            if self.never_created_proof_digest is not None:
                raise ValueError("stopped receipt cannot carry never-created proof")
            if self.observed_terminal_state is AuditPreflightObservedTerminalState.NOT_CREATED:
                raise ValueError("stopped receipt must observe an actual terminal state")
        else:
            if self.capsule_id is not None or self.process_identity_digest is not None:
                raise ValueError("never-created receipt cannot claim a capsule or process")
            if self.never_created_proof_digest is None:
                raise ValueError("never-created receipt requires affirmative proof")
            if self.observed_terminal_state is not AuditPreflightObservedTerminalState.NOT_CREATED:
                raise ValueError("never-created receipt must observe not_created")
        expected = audit_preflight_stop_receipt_digest(self)
        if self.receipt_digest:
            if not hmac.compare_digest(self.receipt_digest, expected):
                raise ValueError("preflight stop receipt digest does not match")
        else:
            object.__setattr__(self, "receipt_digest", expected)
        return self

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))


def audit_preflight_stop_receipt_digest(receipt: AuditPreflightStopReceipt) -> str:
    payload = receipt.model_dump(mode="json", exclude={"receipt_digest"})
    return _domain_digest(AUDIT_PREFLIGHT_STOP_RECEIPT_SCHEMA_VERSION, payload)


_TERMINAL_JOB_STATUSES = frozenset(
    {
        AuditPreflightJobStatus.SUCCEEDED,
        AuditPreflightJobStatus.REJECTED,
        AuditPreflightJobStatus.FAILED,
        AuditPreflightJobStatus.CANCELLED,
    }
)

_AUDIT_PREFLIGHT_TRANSITIONS: Mapping[
    AuditPreflightJobStatus,
    frozenset[AuditPreflightJobStatus],
] = {
    AuditPreflightJobStatus.PENDING: frozenset(
        {
            AuditPreflightJobStatus.CLAIMED,
            AuditPreflightJobStatus.CANCELLING,
            # A single locked-CAS may fence and project a never-created proof.
            AuditPreflightJobStatus.CANCELLED,
        }
    ),
    AuditPreflightJobStatus.CLAIMED: frozenset(
        {
            AuditPreflightJobStatus.RUNNING,
            AuditPreflightJobStatus.CANCELLING,
            AuditPreflightJobStatus.CANCELLED,
            AuditPreflightJobStatus.OUTCOME_UNKNOWN,
        }
    ),
    AuditPreflightJobStatus.RUNNING: frozenset(
        {
            AuditPreflightJobStatus.SUCCEEDED,
            AuditPreflightJobStatus.REJECTED,
            AuditPreflightJobStatus.FAILED,
            AuditPreflightJobStatus.CANCELLING,
            AuditPreflightJobStatus.OUTCOME_UNKNOWN,
        }
    ),
    AuditPreflightJobStatus.OUTCOME_UNKNOWN: frozenset(
        {
            AuditPreflightJobStatus.RUNNING,
            AuditPreflightJobStatus.SUCCEEDED,
            AuditPreflightJobStatus.REJECTED,
            AuditPreflightJobStatus.FAILED,
            AuditPreflightJobStatus.CANCELLING,
        }
    ),
    AuditPreflightJobStatus.CANCELLING: frozenset(
        {
            AuditPreflightJobStatus.CANCELLED,
            AuditPreflightJobStatus.OUTCOME_UNKNOWN,
        }
    ),
    AuditPreflightJobStatus.SUCCEEDED: frozenset(),
    AuditPreflightJobStatus.REJECTED: frozenset(),
    AuditPreflightJobStatus.FAILED: frozenset(),
    AuditPreflightJobStatus.CANCELLED: frozenset(),
}


def audit_preflight_can_transition(
    current: AuditPreflightJobStatus,
    target: AuditPreflightJobStatus,
) -> bool:
    return (
        isinstance(current, AuditPreflightJobStatus)
        and isinstance(target, AuditPreflightJobStatus)
        and target in _AUDIT_PREFLIGHT_TRANSITIONS[current]
    )


def validate_audit_preflight_transition(
    current: AuditPreflightJobStatus,
    target: AuditPreflightJobStatus,
) -> None:
    if not audit_preflight_can_transition(current, target):
        raise InvalidStateTransitionError("AuditPreflightJob", current, target)


class AuditPreflightJob(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-job/v1"] = AUDIT_PREFLIGHT_JOB_SCHEMA_VERSION
    job_id: PreflightId = Field(default_factory=new_id)
    client_request_id: str = Field(min_length=36, max_length=36)
    operator_principal_id: PreflightId
    authorization_scope_digest: PreflightDigest
    request_schema_version: Literal["riftx.audit-preflight-request/v1"] = (
        AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION
    )
    request_digest: PreflightDigest
    restricted_request_json: str = Field(
        min_length=2,
        max_length=MAX_PREFLIGHT_RESTRICTED_REQUEST_BYTES,
        repr=False,
    )
    source_node_id: Literal["local"] = "local"
    source_root_identity_digest: PreflightDigest
    backend_id: PreflightId
    image_digest: PreflightDigest
    policy_digest: PreflightDigest
    canonical_empty_context_id: Literal["riftx.audit-empty-security-context/v1"] = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    )
    canonical_empty_context_digest: PreflightDigest = AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    status: AuditPreflightJobStatus = AuditPreflightJobStatus.PENDING
    state_version: int = Field(default=1, strict=True, ge=1, le=MAX_PREFLIGHT_COUNTER)
    attempt: int = Field(default=0, strict=True, ge=0, le=MAX_PREFLIGHT_COUNTER)
    effect_owner_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )
    lease_id: PreflightId | None = None
    lease_owner_instance_id: PreflightShortId | None = None
    lease_owner_epoch: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_PREFLIGHT_COUNTER,
    )
    lease_expires_at: AwareDatetime | None = None
    lease_expected_state_version: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_PREFLIGHT_COUNTER,
    )
    lease_output_contract_digest: PreflightDigest | None = None
    lease_envelope_digest: PreflightDigest | None = None
    capsule_id: PreflightId | None = None
    capsule_prepare_proof_digest: PreflightDigest | None = None
    result_schema_version: Literal["riftx.audit-preflight-result/v1"] | None = None
    result_json: str | None = Field(
        default=None,
        min_length=2,
        max_length=MAX_PREFLIGHT_RESULT_BYTES,
        repr=False,
    )
    result_digest: PreflightDigest | None = None
    safe_error_code: PreflightSafeCode | None = None
    never_created_proof_digest: PreflightDigest | None = None
    exit_receipt_digest: PreflightDigest | None = None
    stop_receipt_digest: PreflightDigest | None = None
    expires_at: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    @field_validator("client_request_id")
    @classmethod
    def validate_client_request_id(cls, value: str) -> str:
        return _validate_client_request_id(value)

    @field_validator("canonical_empty_context_digest")
    @classmethod
    def validate_empty_context_digest(cls, value: str) -> str:
        return _validate_canonical_empty_context_digest(value)

    @model_validator(mode="after")
    def validate_job(self) -> AuditPreflightJob:
        _parse_canonical_json_object(
            self.restricted_request_json,
            maximum_bytes=MAX_PREFLIGHT_RESTRICTED_REQUEST_BYTES,
            label="restricted preflight request",
        )
        request = PreflightRequest.model_validate_json(self.restricted_request_json)
        if request.canonical_json() != self.restricted_request_json:
            raise ValueError("restricted preflight request does not round-trip canonically")
        request_bindings = (
            (self.client_request_id, request.client_request_id, "client_request_id"),
            (self.request_schema_version, request.schema_version, "request_schema_version"),
            (self.request_digest, request.request_digest, "request_digest"),
            (self.source_node_id, request.source_execution_target.node_id, "source_node_id"),
            (
                self.backend_id,
                request.source_execution_target.source_ingest_backend,
                "backend_id",
            ),
        )
        for actual, expected, label in request_bindings:
            if actual != expected:
                raise ValueError(f"preflight Job {label} does not match restricted request")

        if self.expires_at <= self.created_at:
            raise ValueError("preflight Job must expire after creation")
        if self.updated_at < self.created_at:
            raise ValueError("preflight Job updated_at must not precede created_at")
        if self.started_at is not None and not (
            self.created_at <= self.started_at <= self.updated_at
        ):
            raise ValueError("preflight Job started_at is outside its lifecycle")
        if self.finished_at is not None and not (
            self.created_at <= self.finished_at <= self.updated_at
        ):
            raise ValueError("preflight Job finished_at is outside its lifecycle")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("preflight Job finished_at must not precede started_at")

        owner = self.effect_owner()
        if self.effect_owner_digest:
            if not hmac.compare_digest(self.effect_owner_digest, owner.effect_owner_digest):
                raise ValueError("preflight Job effect owner digest does not match")
        else:
            object.__setattr__(self, "effect_owner_digest", owner.effect_owner_digest)

        lease_fields = (
            self.lease_id,
            self.lease_owner_instance_id,
            self.lease_owner_epoch,
            self.lease_expires_at,
            self.lease_expected_state_version,
            self.lease_output_contract_digest,
            self.lease_envelope_digest,
        )
        has_lease = all(value is not None for value in lease_fields)
        if any(value is not None for value in lease_fields) and not has_lease:
            raise ValueError("preflight Job lease facts must appear together")
        if has_lease:
            envelope = self.lease_envelope()
            assert envelope is not None
            assert self.lease_envelope_digest is not None
            if not hmac.compare_digest(
                self.lease_envelope_digest,
                envelope.lease_envelope_digest,
            ):
                raise ValueError("preflight Job lease envelope digest does not match")
        attempted_statuses = {
            AuditPreflightJobStatus.CLAIMED,
            AuditPreflightJobStatus.RUNNING,
            AuditPreflightJobStatus.OUTCOME_UNKNOWN,
        }
        if self.status in attempted_statuses and not has_lease:
            raise ValueError(f"{self.status.value} preflight Job requires a complete lease")
        if self.status is AuditPreflightJobStatus.PENDING:
            if self.attempt != 0 or has_lease:
                raise ValueError("pending preflight Job cannot carry attempt or lease facts")
        elif self.status in attempted_statuses and self.attempt < 1:
            raise ValueError(f"{self.status.value} preflight Job requires a positive attempt")
        if self.status is AuditPreflightJobStatus.CANCELLING:
            if self.attempt >= 1 and not has_lease:
                raise ValueError("attempted cancelling preflight Job must preserve its lease")
            if self.attempt == 0 and has_lease:
                raise ValueError("unattempted cancelling preflight Job cannot carry a lease")
        if self.status in _TERMINAL_JOB_STATUSES:
            if self.attempt >= 1 and not has_lease:
                raise ValueError("attempted terminal preflight Job must preserve its lease")
            if self.attempt == 0 and has_lease:
                raise ValueError("unattempted terminal preflight Job cannot carry a lease")
        if (
            self.status in {AuditPreflightJobStatus.CLAIMED, AuditPreflightJobStatus.RUNNING}
            and self.lease_expires_at is not None
            and self.lease_expires_at <= self.updated_at
        ):
            raise ValueError("active preflight Job requires a non-expired lease")

        has_capsule = self.capsule_id is not None
        has_prepare_proof = self.capsule_prepare_proof_digest is not None
        if has_prepare_proof and not has_capsule:
            raise ValueError("preflight Job prepare proof requires a capsule identity")
        if self.started_at is not None and not has_capsule:
            raise ValueError("started preflight Job requires a capsule identity")
        if self.status in attempted_statuses and not has_capsule:
            raise ValueError(f"{self.status.value} preflight Job requires a capsule identity")
        if self.status is AuditPreflightJobStatus.RUNNING and self.started_at is None:
            raise ValueError("running preflight Job requires a started capsule")
        if self.status is AuditPreflightJobStatus.CANCELLING:
            if self.attempt >= 1 and not has_capsule:
                raise ValueError(
                    "attempted cancelling preflight Job must preserve capsule identity"
                )
            if self.attempt == 0 and has_capsule:
                raise ValueError("unattempted cancelling preflight Job cannot carry a capsule")
        if self.status in _TERMINAL_JOB_STATUSES:
            if self.attempt >= 1 and not has_capsule:
                raise ValueError("attempted terminal preflight Job must preserve capsule identity")
            if self.attempt == 0 and (has_capsule or self.started_at is not None):
                raise ValueError("unattempted terminal preflight Job cannot carry capsule facts")

        result_fields = (
            self.result_schema_version,
            self.result_json,
            self.result_digest,
        )
        has_result = all(value is not None for value in result_fields)
        if any(value is not None for value in result_fields) and not has_result:
            raise ValueError("preflight Job result facts must appear together")
        result: AuditPreflightResult | None = None
        if has_result:
            assert self.result_json is not None
            _parse_canonical_json_object(
                self.result_json,
                maximum_bytes=MAX_PREFLIGHT_RESULT_BYTES,
                label="preflight result",
            )
            result = AuditPreflightResult.model_validate_json(self.result_json)
            if result.canonical_json() != self.result_json:
                raise ValueError("preflight result does not round-trip canonically")
            result_bindings: tuple[tuple[object, object, str], ...] = (
                (result.preflight_job_id, self.job_id, "job_id"),
                (result.request_digest, self.request_digest, "request_digest"),
                (result.effect_owner_digest, self.effect_owner_digest, "effect_owner_digest"),
                (result.source_node_id, self.source_node_id, "source_node_id"),
                (
                    result.source_root_identity_digest,
                    self.source_root_identity_digest,
                    "source_root_identity_digest",
                ),
                (result.backend_id, self.backend_id, "backend_id"),
                (result.image_digest, self.image_digest, "image_digest"),
                (result.policy_digest, self.policy_digest, "policy_digest"),
                (result.result_digest, self.result_digest, "result_digest"),
                (result.completed_at, self.finished_at, "completed_at"),
            )
            for result_actual, result_expected, result_label in result_bindings:
                if result_actual != result_expected:
                    raise ValueError(f"preflight Job result {result_label} binding does not match")
            if result.expires_at > self.expires_at:
                raise ValueError("preflight result cannot outlive its Job owner")

        has_stop_proof = any(
            value is not None
            for value in (self.never_created_proof_digest, self.stop_receipt_digest)
        )
        has_exit_proof = self.exit_receipt_digest is not None
        if has_exit_proof and has_stop_proof:
            raise ValueError(
                "preflight Job cannot combine exit proof with stop or never-created proof"
            )
        if self.status in {
            AuditPreflightJobStatus.PENDING,
            AuditPreflightJobStatus.CLAIMED,
            AuditPreflightJobStatus.RUNNING,
        } and (has_exit_proof or has_stop_proof):
            raise ValueError("active preflight Job cannot carry terminal proof")
        if self.status is AuditPreflightJobStatus.SUCCEEDED:
            if self.attempt < 1:
                raise ValueError("succeeded preflight Job requires a positive attempt")
            if not has_result or not has_exit_proof or self.safe_error_code is not None:
                raise ValueError(
                    "succeeded preflight Job requires immutable result and exit receipt"
                )
            if not has_prepare_proof or self.started_at is None:
                raise ValueError("succeeded preflight Job requires prepared started capsule")
            if has_stop_proof:
                raise ValueError("succeeded preflight Job cannot carry stop or never-created proof")
        elif self.status is AuditPreflightJobStatus.REJECTED:
            if self.attempt < 1:
                raise ValueError("rejected preflight Job requires a positive attempt")
            if not has_result or not has_exit_proof or self.safe_error_code is None:
                raise ValueError(
                    "rejected preflight Job requires result, safe error, and exit receipt"
                )
            assert result is not None
            if not result.blocking_errors:
                raise ValueError("rejected preflight Job result requires blocking_errors")
            if not has_prepare_proof or self.started_at is None:
                raise ValueError("rejected preflight Job requires prepared started capsule")
            if has_stop_proof:
                raise ValueError("rejected preflight Job cannot carry stop proof")
        elif self.status is AuditPreflightJobStatus.FAILED:
            if has_result or self.safe_error_code is None or not (has_exit_proof or has_stop_proof):
                raise ValueError("failed preflight Job requires safe error and terminal proof")
            if has_exit_proof and (not has_prepare_proof or self.started_at is None):
                raise ValueError("exit-proven failed preflight Job requires prepared capsule")
        elif self.status is AuditPreflightJobStatus.CANCELLED:
            if has_result or has_exit_proof or not has_stop_proof:
                raise ValueError("cancelled preflight Job requires affirmative stop proof")
        elif has_result:
            raise ValueError("nonterminal preflight Job cannot carry a result")

        if self.status in _TERMINAL_JOB_STATUSES:
            if self.finished_at is None:
                raise ValueError("terminal preflight Job requires finished_at")
        elif self.finished_at is not None:
            raise ValueError("nonterminal preflight Job cannot carry finished_at")
        if (
            self.status
            in {
                AuditPreflightJobStatus.PENDING,
                AuditPreflightJobStatus.CLAIMED,
                AuditPreflightJobStatus.RUNNING,
            }
            and self.safe_error_code is not None
        ):
            raise ValueError("active preflight Job cannot carry safe_error_code")
        return self

    def effect_owner(self) -> AuditPreflightEffectOwner:
        return AuditPreflightEffectOwner(
            job_id=self.job_id,
            operator_principal_id=self.operator_principal_id,
            authorization_scope_digest=self.authorization_scope_digest,
            source_node_id=self.source_node_id,
            source_root_identity_digest=self.source_root_identity_digest,
            request_schema_version=self.request_schema_version,
            request_digest=self.request_digest,
            backend_id=self.backend_id,
            image_digest=self.image_digest,
            policy_digest=self.policy_digest,
            created_at=self.created_at,
            expires_at=self.expires_at,
        )

    def lease_envelope(self) -> AuditPreflightLeaseEnvelope | None:
        lease_fields = (
            self.lease_id,
            self.lease_owner_instance_id,
            self.lease_owner_epoch,
            self.lease_expires_at,
            self.lease_expected_state_version,
            self.lease_output_contract_digest,
        )
        if not all(value is not None for value in lease_fields):
            return None
        assert self.lease_id is not None
        assert self.lease_owner_instance_id is not None
        assert self.lease_owner_epoch is not None
        assert self.lease_expires_at is not None
        assert self.lease_expected_state_version is not None
        assert self.lease_output_contract_digest is not None
        return AuditPreflightLeaseEnvelope(
            owner=self.effect_owner(),
            runner_principal=RunnerPrincipal(
                instance_id=self.lease_owner_instance_id,
                epoch=self.lease_owner_epoch,
            ),
            lease_id=self.lease_id,
            lease_expires_at=self.lease_expires_at,
            expected_state_version=self.lease_expected_state_version,
            output_contract_digest=self.lease_output_contract_digest,
            lease_envelope_digest=self.lease_envelope_digest or "",
        )

    def can_transition_to(self, target: AuditPreflightJobStatus) -> bool:
        return audit_preflight_can_transition(self.status, target)

    def validate_transition_to(self, target: AuditPreflightJobStatus) -> None:
        validate_audit_preflight_transition(self.status, target)


def audit_preflight_idempotency_drift_fields(
    job: AuditPreflightJob,
    *,
    operator_principal_id: str,
    client_request_id: str,
    authorization_scope_digest: str,
    request_schema_version: str,
    request_digest: str,
) -> tuple[str, ...]:
    candidates = (
        ("operator_principal_id", job.operator_principal_id, operator_principal_id),
        ("client_request_id", job.client_request_id, client_request_id),
        (
            "authorization_scope_digest",
            job.authorization_scope_digest,
            authorization_scope_digest,
        ),
        ("request_schema_version", job.request_schema_version, request_schema_version),
        ("request_digest", job.request_digest, request_digest),
    )
    return tuple(label for label, expected, actual in candidates if expected != actual)


def audit_preflight_is_exact_replay(
    job: AuditPreflightJob,
    *,
    operator_principal_id: str,
    client_request_id: str,
    authorization_scope_digest: str,
    request_schema_version: str,
    request_digest: str,
) -> bool:
    return not audit_preflight_idempotency_drift_fields(
        job,
        operator_principal_id=operator_principal_id,
        client_request_id=client_request_id,
        authorization_scope_digest=authorization_scope_digest,
        request_schema_version=request_schema_version,
        request_digest=request_digest,
    )


def audit_preflight_terminal_replay_drift_fields(
    job: AuditPreflightJob,
    *,
    status: AuditPreflightJobStatus,
    result_schema_version: str | None,
    result_digest: str | None,
    safe_error_code: str | None,
    never_created_proof_digest: str | None,
    exit_receipt_digest: str | None,
    stop_receipt_digest: str | None,
) -> tuple[str, ...]:
    candidates = (
        ("status", job.status, status),
        ("result_schema_version", job.result_schema_version, result_schema_version),
        ("result_digest", job.result_digest, result_digest),
        ("safe_error_code", job.safe_error_code, safe_error_code),
        (
            "never_created_proof_digest",
            job.never_created_proof_digest,
            never_created_proof_digest,
        ),
        ("exit_receipt_digest", job.exit_receipt_digest, exit_receipt_digest),
        ("stop_receipt_digest", job.stop_receipt_digest, stop_receipt_digest),
    )
    return tuple(label for label, expected, actual in candidates if expected != actual)


def audit_preflight_is_exact_terminal_replay(
    job: AuditPreflightJob,
    *,
    status: AuditPreflightJobStatus,
    result_schema_version: str | None,
    result_digest: str | None,
    safe_error_code: str | None,
    never_created_proof_digest: str | None,
    exit_receipt_digest: str | None,
    stop_receipt_digest: str | None,
) -> bool:
    if job.status not in _TERMINAL_JOB_STATUSES:
        return False
    return not audit_preflight_terminal_replay_drift_fields(
        job,
        status=status,
        result_schema_version=result_schema_version,
        result_digest=result_digest,
        safe_error_code=safe_error_code,
        never_created_proof_digest=never_created_proof_digest,
        exit_receipt_digest=exit_receipt_digest,
        stop_receipt_digest=stop_receipt_digest,
    )
