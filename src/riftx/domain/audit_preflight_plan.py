"""Immutable authorization plans derived from completed Code Audit Preflight jobs.

This module deliberately owns no persistence, HTTP, Git, Snapshot, or StartIntent
behavior.  It turns an already terminal and internally consistent Preflight result
into a short-lived plan, defines the opaque bearer-token codec, and validates the
small state machine used by later atomic creation/start units of work.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .audit import AuditMode, SourceTargetKind
from .audit_preflight import (
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST,
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID,
    AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION,
    AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION,
    AuditPreflightCapabilityMatrix,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightMinimumFeasibleBudget,
    AuditPreflightResult,
    PreflightRequest,
)
from .base import DomainModel, new_id, utc_now
from .errors import InvalidStateTransitionError

AUDIT_PREFLIGHT_PLAN_SCHEMA_VERSION: Literal["riftx.audit-preflight-plan/v1"] = (
    "riftx.audit-preflight-plan/v1"
)
AUDIT_PREFLIGHT_PLAN_TARGET_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-plan-target/v1"
] = "riftx.audit-preflight-plan-target/v1"
AUDIT_PREFLIGHT_PLAN_SCOPE_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-plan-scope/v1"
] = "riftx.audit-preflight-plan-scope/v1"
AUDIT_PREFLIGHT_MINIMUM_BUDGET_DIGEST_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-plan-minimum-budget/v1"
] = "riftx.audit-preflight-plan-minimum-budget/v1"
AUDIT_PREFLIGHT_TOKEN_SCHEMA_VERSION: Literal["riftx.audit-preflight-token/v1"] = (
    "riftx.audit-preflight-token/v1"
)
AUDIT_PREFLIGHT_TOKEN_VERIFIER_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-token-verifier/v1"
] = "riftx.audit-preflight-token-verifier/v1"
AUDIT_PREFLIGHT_TOKEN_HASH_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-token-hash/v1"
] = "riftx.audit-preflight-token-hash/v1"

MAX_AUDIT_PREFLIGHT_PLAN_BYTES = 256 * 1_024
MAX_PLAN_RELATIVE_PATHS = 512
MAX_PLAN_RELATIVE_PATH_BYTES = 4_096
MAX_PLAN_RELATIVE_PATH_TOTAL_BYTES = 64 * 1_024
MAX_PLAN_REPOSITORY_PATH_BYTES = 4_096
MAX_PLAN_COUNTER = 2**63 - 1
TOKEN_NONCE_BYTES = 32
TOKEN_MAC_BYTES = hashlib.sha256().digest_size
TOKEN_WIRE_BYTES = TOKEN_NONCE_BYTES + TOKEN_MAC_BYTES
TOKEN_NONCE_WIRE_LENGTH = 43
TOKEN_WIRE_LENGTH = 86

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$"
_SHORT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,63}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_DIGEST_OR_EMPTY_PATTERN = r"^(?:|[0-9a-f]{64})$"
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_UNC_SERVER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")

type PlanId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_ID_PATTERN),
]
type PlanShortId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=_SHORT_ID_PATTERN),
]
type PlanDigest = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=_DIGEST_PATTERN),
]

_JSON_MAPPING_ADAPTER = TypeAdapter(dict[str, Any])


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


def _domain_bytes_digest(domain: str, value: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + value).hexdigest()


def _require_aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _validate_bounded_text(value: str, *, label: str, maximum_bytes: int) -> str:
    if value != value.strip() or not value:
        raise ValueError(f"{label} must be non-empty without surrounding whitespace")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must use NFC normalization")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{label} must not contain control characters")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its byte limit")
    return value


def _validate_repository_path(value: str) -> str:
    _validate_bounded_text(
        value,
        label="repository_path",
        maximum_bytes=MAX_PLAN_REPOSITORY_PATH_BYTES,
    )
    if value.startswith("~") or "\\" in value:
        raise ValueError("repository_path must use a canonical absolute wire path")
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
        raise ValueError("repository_path must be absolute")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("repository_path must not contain empty or dot segments")
    return value


def _validate_relative_path(value: str) -> str:
    _validate_bounded_text(
        value,
        label="repository-relative path",
        maximum_bytes=MAX_PLAN_RELATIVE_PATH_BYTES,
    )
    if value.startswith(("/", "~")) or "\\" in value or _WINDOWS_DRIVE_PREFIX.match(value):
        raise ValueError("repository-relative path must not be absolute")
    if any(component in {"", ".", ".."} for component in value.split("/")):
        raise ValueError("repository-relative path must not contain empty or dot segments")
    return value


def _validate_sorted_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > MAX_PLAN_RELATIVE_PATHS:
        raise ValueError("repository-relative paths exceed their item limit")
    for value in values:
        _validate_relative_path(value)
    if values != tuple(sorted(values)):
        raise ValueError("repository-relative paths must use canonical sorted order")
    if len(values) != len(set(values)):
        raise ValueError("repository-relative paths must not contain duplicates")
    if sum(len(value.encode("utf-8")) for value in values) > MAX_PLAN_RELATIVE_PATH_TOTAL_BYTES:
        raise ValueError("repository-relative paths exceed their total byte limit")
    return values


def _validate_uuid(value: str, *, label: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(f"{label} must be a non-zero canonical UUID")
    return value


def _validate_safe_id(value: str, *, label: str, maximum_bytes: int = 128) -> str:
    _validate_bounded_text(value, label=label, maximum_bytes=maximum_bytes)
    if re.fullmatch(_ID_PATTERN, value) is None:
        raise ValueError(f"{label} contains unsupported characters")
    return value


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str, *, expected_bytes: int, label: str) -> bytes:
    expected_length = (expected_bytes * 8 + 5) // 6
    if (
        len(value) != expected_length
        or _BASE64URL_PATTERN.fullmatch(value) is None
        or "=" in value
    ):
        raise ValueError(f"{label} is not canonical base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} is not canonical base64url") from exc
    if len(decoded) != expected_bytes or _base64url_encode(decoded) != value:
        raise ValueError(f"{label} is not canonical base64url")
    return decoded


class AuditPreflightPlanStrictModel(DomainModel):
    """Fail-closed immutable base for Plan identity and verifier facts."""

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
            raise TypeError("preflight Plan models forbid unvalidated model_copy updates")
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
        raise TypeError("preflight Plan models forbid deprecated copy")


class AuditPreflightPlanStatus(StrEnum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    CONSUMED = "consumed"
    REVOKED = "revoked"


_PLAN_TRANSITIONS: Mapping[AuditPreflightPlanStatus, frozenset[AuditPreflightPlanStatus]] = {
    AuditPreflightPlanStatus.AVAILABLE: frozenset(
        {AuditPreflightPlanStatus.RESERVED, AuditPreflightPlanStatus.REVOKED}
    ),
    AuditPreflightPlanStatus.RESERVED: frozenset(
        {AuditPreflightPlanStatus.CONSUMED, AuditPreflightPlanStatus.REVOKED}
    ),
    AuditPreflightPlanStatus.CONSUMED: frozenset(),
    AuditPreflightPlanStatus.REVOKED: frozenset(),
}


def audit_preflight_plan_can_transition(
    current: AuditPreflightPlanStatus,
    target: AuditPreflightPlanStatus,
) -> bool:
    return (
        isinstance(current, AuditPreflightPlanStatus)
        and isinstance(target, AuditPreflightPlanStatus)
        and target in _PLAN_TRANSITIONS[current]
    )


def validate_audit_preflight_plan_transition(
    current: AuditPreflightPlanStatus,
    target: AuditPreflightPlanStatus,
) -> None:
    if not audit_preflight_plan_can_transition(current, target):
        raise InvalidStateTransitionError("AuditPreflightPlan", current, target)


class AuditPreflightPlanTarget(AuditPreflightPlanStrictModel):
    schema_version: Literal["riftx.audit-preflight-plan-target/v1"] = (
        AUDIT_PREFLIGHT_PLAN_TARGET_SCHEMA_VERSION
    )
    repository_path: str = Field(
        min_length=1,
        max_length=MAX_PLAN_REPOSITORY_PATH_BYTES,
        repr=False,
    )
    source_node_id: Literal["local"] = "local"
    source_ingest_backend_id: PlanId
    kind: SourceTargetKind
    revision: str = Field(min_length=1, max_length=1_024)
    base_revision: str | None = Field(default=None, min_length=1, max_length=1_024)
    mode: AuditMode
    include_untracked: bool = False
    head_revision: str = Field(min_length=40, max_length=64)
    resolved_revision: str = Field(min_length=40, max_length=64)
    resolved_base_revision: str | None = Field(default=None, min_length=40, max_length=64)
    merge_base_revision: str | None = Field(default=None, min_length=40, max_length=64)
    target_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @field_validator("repository_path")
    @classmethod
    def validate_repository_path(cls, value: str) -> str:
        return _validate_repository_path(value)

    @field_validator("revision", "base_revision")
    @classmethod
    def validate_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validate_bounded_text(value, label="revision", maximum_bytes=1_024)
        if value.startswith("-"):
            raise ValueError("revision must not be option-shaped")
        return value

    @field_validator(
        "head_revision",
        "resolved_revision",
        "resolved_base_revision",
        "merge_base_revision",
    )
    @classmethod
    def validate_object_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) not in {40, 64} or re.fullmatch(r"[0-9a-f]+", value) is None:
            raise ValueError("resolved Git object identity must be lowercase hexadecimal")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> AuditPreflightPlanTarget:
        if self.kind is SourceTargetKind.REVISION and self.include_untracked:
            raise ValueError("revision target cannot include untracked files")
        if self.mode is AuditMode.DIFF:
            if (
                self.base_revision is None
                or self.base_revision == self.revision
                or self.resolved_base_revision is None
                or self.merge_base_revision is None
            ):
                raise ValueError("Diff Plan target requires distinct resolved base/head facts")
        elif any(
            value is not None
            for value in (
                self.base_revision,
                self.resolved_base_revision,
                self.merge_base_revision,
            )
        ):
            raise ValueError("non-Diff Plan target cannot carry base or merge-base facts")
        expected = audit_preflight_plan_target_digest(self)
        if self.target_digest:
            if not hmac.compare_digest(self.target_digest, expected):
                raise ValueError("preflight Plan target digest does not match")
        else:
            object.__setattr__(self, "target_digest", expected)
        return self


def audit_preflight_plan_target_digest(target: AuditPreflightPlanTarget) -> str:
    payload = target.model_dump(mode="json", exclude={"target_digest"})
    return _domain_digest(AUDIT_PREFLIGHT_PLAN_TARGET_SCHEMA_VERSION, payload)


class AuditPreflightPlanScope(AuditPreflightPlanStrictModel):
    schema_version: Literal["riftx.audit-preflight-plan-scope/v1"] = (
        AUDIT_PREFLIGHT_PLAN_SCOPE_SCHEMA_VERSION
    )
    include_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PLAN_RELATIVE_PATHS,
    )
    exclude_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PLAN_RELATIVE_PATHS,
    )
    scope_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @field_validator("include_paths", "exclude_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_sorted_paths(values)

    @model_validator(mode="after")
    def validate_scope(self) -> AuditPreflightPlanScope:
        if set(self.include_paths).intersection(self.exclude_paths):
            raise ValueError("Plan include/exclude paths must not overlap")
        expected = audit_preflight_plan_scope_digest(self)
        if self.scope_digest:
            if not hmac.compare_digest(self.scope_digest, expected):
                raise ValueError("preflight Plan scope digest does not match")
        else:
            object.__setattr__(self, "scope_digest", expected)
        return self


def audit_preflight_plan_scope_digest(scope: AuditPreflightPlanScope) -> str:
    payload = scope.model_dump(mode="json", exclude={"scope_digest"})
    return _domain_digest(AUDIT_PREFLIGHT_PLAN_SCOPE_SCHEMA_VERSION, payload)


def audit_preflight_minimum_budget_digest(
    budget: AuditPreflightMinimumFeasibleBudget,
) -> str:
    return _domain_digest(
        AUDIT_PREFLIGHT_MINIMUM_BUDGET_DIGEST_SCHEMA_VERSION,
        budget.model_dump(mode="json"),
    )


class AuditPreflightTokenVerifier(AuditPreflightPlanStrictModel):
    schema_version: Literal["riftx.audit-preflight-token-verifier/v1"] = (
        AUDIT_PREFLIGHT_TOKEN_VERIFIER_SCHEMA_VERSION
    )
    key_id: PlanShortId
    nonce: str = Field(min_length=TOKEN_NONCE_WIRE_LENGTH, max_length=TOKEN_NONCE_WIRE_LENGTH)
    token_hash: PlanDigest

    @field_validator("nonce")
    @classmethod
    def validate_nonce(cls, value: str) -> str:
        _base64url_decode(value, expected_bytes=TOKEN_NONCE_BYTES, label="token nonce")
        return value


@dataclass(frozen=True, slots=True)
class AuditPreflightTokenIssue:
    token: str = field(repr=False)
    verifier: AuditPreflightTokenVerifier


class AuditPreflightTokenCodec:
    """Issue and verify fixed-width opaque Plan bearer tokens."""

    __slots__ = ("_key", "_nonce_factory", "key_id")

    def __init__(
        self,
        *,
        key_id: str,
        key: bytes,
        nonce_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.key_id = _validate_safe_id(key_id, label="token key_id", maximum_bytes=64)
        if not isinstance(key, bytes) or not 32 <= len(key) <= 4_096:
            raise ValueError("preflight token HMAC key must contain at least 32 bytes")
        if not callable(nonce_factory):
            raise TypeError("nonce_factory must be callable")
        self._key = key
        self._nonce_factory = nonce_factory

    def issue(self, *, plan_id: str, plan_digest: str) -> AuditPreflightTokenIssue:
        _validate_safe_id(plan_id, label="plan_id")
        _require_digest(plan_digest, label="plan_digest")
        nonce = self._nonce_factory(TOKEN_NONCE_BYTES)
        if not isinstance(nonce, bytes) or len(nonce) != TOKEN_NONCE_BYTES:
            raise ValueError("preflight token nonce factory must return exactly 32 bytes")
        nonce_wire = _base64url_encode(nonce)
        token = self._derive_token(
            plan_id=plan_id,
            plan_digest=plan_digest,
            nonce=nonce,
            nonce_wire=nonce_wire,
        )
        return AuditPreflightTokenIssue(
            token=token,
            verifier=AuditPreflightTokenVerifier(
                key_id=self.key_id,
                nonce=nonce_wire,
                token_hash=audit_preflight_token_hash(token),
            ),
        )

    def token_for(
        self,
        *,
        plan_id: str,
        plan_digest: str,
        verifier: AuditPreflightTokenVerifier,
    ) -> str:
        if not hmac.compare_digest(verifier.key_id, self.key_id):
            raise ValueError("preflight token verifier uses a different key")
        nonce = _base64url_decode(
            verifier.nonce,
            expected_bytes=TOKEN_NONCE_BYTES,
            label="token nonce",
        )
        token = self._derive_token(
            plan_id=plan_id,
            plan_digest=plan_digest,
            nonce=nonce,
            nonce_wire=verifier.nonce,
        )
        if not hmac.compare_digest(audit_preflight_token_hash(token), verifier.token_hash):
            raise ValueError("preflight token verifier hash does not match")
        return token

    def verify(
        self,
        token: str,
        *,
        plan_id: str,
        plan_digest: str,
        verifier: AuditPreflightTokenVerifier,
    ) -> bool:
        try:
            supplied_bytes = _base64url_decode(
                token,
                expected_bytes=TOKEN_WIRE_BYTES,
                label="preflight token",
            )
            persisted_nonce = _base64url_decode(
                verifier.nonce,
                expected_bytes=TOKEN_NONCE_BYTES,
                label="token nonce",
            )
            if not hmac.compare_digest(supplied_bytes[:TOKEN_NONCE_BYTES], persisted_nonce):
                return False
            if not hmac.compare_digest(audit_preflight_token_hash(token), verifier.token_hash):
                return False
            expected = self.token_for(
                plan_id=plan_id,
                plan_digest=plan_digest,
                verifier=verifier,
            )
        except (TypeError, ValueError):
            return False
        return hmac.compare_digest(token, expected)

    def _derive_token(
        self,
        *,
        plan_id: str,
        plan_digest: str,
        nonce: bytes,
        nonce_wire: str,
    ) -> str:
        payload = _canonical_json(
            {
                "schema_version": AUDIT_PREFLIGHT_TOKEN_SCHEMA_VERSION,
                "key_id": self.key_id,
                "nonce": nonce_wire,
                "plan_digest": plan_digest,
                "plan_id": plan_id,
            }
        ).encode("utf-8")
        mac = hmac.new(
            self._key,
            AUDIT_PREFLIGHT_TOKEN_SCHEMA_VERSION.encode("ascii") + b"\0" + payload,
            hashlib.sha256,
        ).digest()
        token = _base64url_encode(nonce + mac)
        if len(token) != TOKEN_WIRE_LENGTH:
            raise AssertionError("preflight token encoder produced a non-canonical width")
        return token


def audit_preflight_token_hash(token: str) -> str:
    _base64url_decode(token, expected_bytes=TOKEN_WIRE_BYTES, label="preflight token")
    return _domain_bytes_digest(AUDIT_PREFLIGHT_TOKEN_HASH_SCHEMA_VERSION, token.encode("ascii"))


def _require_digest(value: str, *, label: str) -> str:
    if re.fullmatch(_DIGEST_PATTERN, value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


class AuditPreflightPlan(AuditPreflightPlanStrictModel):
    schema_version: Literal["riftx.audit-preflight-plan/v1"] = (
        AUDIT_PREFLIGHT_PLAN_SCHEMA_VERSION
    )
    plan_id: PlanId = Field(default_factory=new_id)
    plan_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )
    preflight_job_id: PlanId
    preflight_client_request_id: str = Field(min_length=36, max_length=36)
    operator_principal_id: PlanId
    authorization_scope_digest: PlanDigest
    request_schema_version: Literal["riftx.audit-preflight-request/v1"] = (
        AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION
    )
    request_digest: PlanDigest
    result_schema_version: Literal["riftx.audit-preflight-result/v1"] = (
        AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION
    )
    result_digest: PlanDigest
    effect_owner_digest: PlanDigest
    source_node_id: Literal["local"] = "local"
    source_root_identity_digest: PlanDigest
    repository_identity_digest: PlanDigest
    content_identity_digest: PlanDigest
    backend_id: PlanId
    image_digest: PlanDigest
    policy_digest: PlanDigest
    capsule_prepare_proof_digest: PlanDigest
    target: AuditPreflightPlanTarget = Field(repr=False)
    target_digest: PlanDigest
    scope: AuditPreflightPlanScope = Field(repr=False)
    scope_digest: PlanDigest
    capability_matrix: AuditPreflightCapabilityMatrix = Field(repr=False)
    capability_matrix_digest: PlanDigest
    minimum_feasible_budget: AuditPreflightMinimumFeasibleBudget = Field(repr=False)
    minimum_feasible_budget_digest: PlanDigest
    security_context_id: Literal["riftx.audit-empty-security-context/v1"] = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    )
    security_context_digest: PlanDigest = AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    preflight_completed_at: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime
    token_verifier: AuditPreflightTokenVerifier = Field(repr=False)
    status: AuditPreflightPlanStatus = AuditPreflightPlanStatus.AVAILABLE
    state_version: int = Field(default=1, strict=True, ge=1, le=MAX_PLAN_COUNTER)
    reserved_audit_id: PlanId | None = None
    reserved_client_request_id: str | None = Field(default=None, min_length=36, max_length=36)
    reserved_at: AwareDatetime | None = None
    consumed_audit_id: PlanId | None = None
    consumed_start_request_id: str | None = Field(default=None, min_length=36, max_length=36)
    consumed_at: AwareDatetime | None = None
    revocation_reason: PlanId | None = None
    revoked_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    @field_validator(
        "preflight_client_request_id",
        "reserved_client_request_id",
        "consumed_start_request_id",
    )
    @classmethod
    def validate_request_ids(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _validate_uuid(value, label=info.field_name)

    @field_validator("revocation_reason")
    @classmethod
    def validate_revocation_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_safe_id(value, label="revocation_reason")

    @model_validator(mode="after")
    def validate_plan(self) -> AuditPreflightPlan:
        for value, label in (
            (self.preflight_completed_at, "preflight_completed_at"),
            (self.created_at, "created_at"),
            (self.expires_at, "expires_at"),
            (self.updated_at, "updated_at"),
        ):
            _require_aware(value, label=label)
        if not self.preflight_completed_at <= self.created_at < self.expires_at:
            raise ValueError("preflight Plan timestamps are not ordered")
        if self.updated_at < self.created_at:
            raise ValueError("preflight Plan updated_at cannot precede creation")
        if (
            self.target.source_node_id != self.source_node_id
            or self.target.source_ingest_backend_id != self.backend_id
        ):
            raise ValueError("preflight Plan target execution binding does not match")
        if not hmac.compare_digest(self.target_digest, self.target.target_digest):
            raise ValueError("preflight Plan target digest binding does not match")
        if not hmac.compare_digest(self.scope_digest, self.scope.scope_digest):
            raise ValueError("preflight Plan scope digest binding does not match")
        if not hmac.compare_digest(
            self.capability_matrix_digest,
            self.capability_matrix.matrix_digest,
        ):
            raise ValueError("preflight Plan capability digest binding does not match")
        expected_budget_digest = audit_preflight_minimum_budget_digest(
            self.minimum_feasible_budget
        )
        if not hmac.compare_digest(
            self.minimum_feasible_budget_digest,
            expected_budget_digest,
        ):
            raise ValueError("preflight Plan budget digest binding does not match")
        if not hmac.compare_digest(
            self.security_context_digest,
            AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST,
        ):
            raise ValueError("preflight Plan security context digest does not match")

        reservation = (
            self.reserved_audit_id,
            self.reserved_client_request_id,
            self.reserved_at,
        )
        consumption = (
            self.consumed_audit_id,
            self.consumed_start_request_id,
            self.consumed_at,
        )
        revocation = (self.revocation_reason, self.revoked_at)
        has_reservation = all(value is not None for value in reservation)
        has_consumption = all(value is not None for value in consumption)
        has_revocation = all(value is not None for value in revocation)
        if any(value is not None for value in reservation) and not has_reservation:
            raise ValueError("preflight Plan reservation facts must appear together")
        if any(value is not None for value in consumption) and not has_consumption:
            raise ValueError("preflight Plan consumption facts must appear together")
        if any(value is not None for value in revocation) and not has_revocation:
            raise ValueError("preflight Plan revocation facts must appear together")

        if self.status is AuditPreflightPlanStatus.AVAILABLE:
            if has_reservation or has_consumption or has_revocation or self.state_version != 1:
                raise ValueError("available preflight Plan cannot carry lifecycle facts")
            if self.updated_at != self.created_at:
                raise ValueError("available preflight Plan must be unchanged since creation")
        elif self.status is AuditPreflightPlanStatus.RESERVED:
            if not has_reservation or has_consumption or has_revocation or self.state_version != 2:
                raise ValueError("reserved preflight Plan requires exact reservation facts")
            if self.reserved_at != self.updated_at:
                raise ValueError("reserved preflight Plan updated_at must equal reserved_at")
        elif self.status is AuditPreflightPlanStatus.CONSUMED:
            if (
                not has_reservation
                or not has_consumption
                or has_revocation
                or self.state_version != 3
            ):
                raise ValueError("consumed preflight Plan requires exact consumption facts")
            if self.reserved_audit_id != self.consumed_audit_id:
                raise ValueError("consumed preflight Plan must preserve its reserved Audit")
            assert self.reserved_at is not None
            assert self.consumed_at is not None
            if self.consumed_at < self.reserved_at or self.updated_at != self.consumed_at:
                raise ValueError("preflight Plan consumption timestamps are not ordered")
        else:
            if has_consumption or not has_revocation:
                raise ValueError("revoked preflight Plan cannot carry consumption facts")
            expected_version = 3 if has_reservation else 2
            if self.state_version != expected_version:
                raise ValueError("revoked preflight Plan has an invalid state version")
            assert self.revoked_at is not None
            if has_reservation:
                assert self.reserved_at is not None
                if self.revoked_at < self.reserved_at:
                    raise ValueError("preflight Plan revocation cannot precede reservation")
            if self.updated_at != self.revoked_at:
                raise ValueError("revoked preflight Plan updated_at must equal revoked_at")

        expected = audit_preflight_plan_digest(self)
        if self.plan_digest:
            if not hmac.compare_digest(self.plan_digest, expected):
                raise ValueError("preflight Plan digest does not match")
        else:
            object.__setattr__(self, "plan_digest", expected)
        canonical = self.canonical_json()
        if len(canonical.encode("utf-8")) > MAX_AUDIT_PREFLIGHT_PLAN_BYTES:
            raise ValueError("preflight Plan exceeds its canonical byte limit")
        return self

    @classmethod
    def from_succeeded(
        cls,
        *,
        job: AuditPreflightJob,
        result: AuditPreflightResult,
        restricted_request: PreflightRequest,
        token_codec: AuditPreflightTokenCodec,
        plan_id: str | None = None,
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> AuditPreflightPlanIssue:
        if job.status is not AuditPreflightJobStatus.SUCCEEDED:
            raise ValueError("preflight Plan requires a succeeded Job")
        if job.result_json != result.canonical_json() or job.result_digest != result.result_digest:
            raise ValueError("preflight Plan Result does not match the succeeded Job")
        if job.restricted_request_json != restricted_request.canonical_json():
            raise ValueError("preflight Plan restricted request does not match the Job")
        if result.blocking_errors:
            raise ValueError("preflight Plan cannot be created from a blocked Result")
        bindings: tuple[tuple[object, object, str], ...] = (
            (job.client_request_id, restricted_request.client_request_id, "client_request_id"),
            (job.request_digest, restricted_request.request_digest, "request_digest"),
            (result.preflight_job_id, job.job_id, "preflight_job_id"),
            (result.request_digest, job.request_digest, "Result request_digest"),
            (result.effect_owner_digest, job.effect_owner_digest, "effect_owner_digest"),
            (result.source_node_id, job.source_node_id, "source_node_id"),
            (
                result.source_root_identity_digest,
                job.source_root_identity_digest,
                "source_root_identity_digest",
            ),
            (result.backend_id, job.backend_id, "backend_id"),
            (result.image_digest, job.image_digest, "image_digest"),
            (result.policy_digest, job.policy_digest, "policy_digest"),
            (
                result.capsule_prepare_proof_digest,
                job.capsule_prepare_proof_digest,
                "capsule_prepare_proof_digest",
            ),
            (result.target_kind, restricted_request.target.kind, "target kind"),
            (result.revision, restricted_request.target.revision, "target revision"),
            (result.base_revision, restricted_request.target.base_revision, "base revision"),
            (
                result.include_untracked,
                restricted_request.target.include_untracked,
                "untracked policy",
            ),
            (result.mode, restricted_request.mode, "mode"),
            (
                restricted_request.source_execution_target.node_id,
                job.source_node_id,
                "request source node",
            ),
            (
                restricted_request.source_execution_target.source_ingest_backend,
                job.backend_id,
                "request backend",
            ),
            (
                result.canonical_empty_context_id,
                job.canonical_empty_context_id,
                "security context ID",
            ),
            (
                result.canonical_empty_context_digest,
                job.canonical_empty_context_digest,
                "security context digest",
            ),
        )
        for actual, expected, label in bindings:
            if actual != expected:
                raise ValueError(f"preflight Plan {label} binding does not match")
        if result.head_revision is None or result.resolved_revision is None:
            raise ValueError("preflight Plan requires resolved target facts")

        issued_at = created_at or utc_now()
        _require_aware(issued_at, label="created_at")
        owner_expiry = min(job.expires_at, result.expires_at)
        plan_expiry = expires_at or owner_expiry
        _require_aware(plan_expiry, label="expires_at")
        if not result.completed_at <= issued_at < plan_expiry <= owner_expiry:
            raise ValueError("preflight Plan lifetime is outside its Result owner lifetime")

        target = AuditPreflightPlanTarget(
            repository_path=restricted_request.repository_path,
            source_node_id=job.source_node_id,
            source_ingest_backend_id=job.backend_id,
            kind=result.target_kind,
            revision=result.revision,
            base_revision=result.base_revision,
            mode=result.mode,
            include_untracked=result.include_untracked,
            head_revision=result.head_revision,
            resolved_revision=result.resolved_revision,
            resolved_base_revision=result.resolved_base_revision,
            merge_base_revision=result.merge_base_revision,
        )
        scope = AuditPreflightPlanScope(
            include_paths=restricted_request.include_paths,
            exclude_paths=restricted_request.exclude_paths,
        )
        budget_digest = audit_preflight_minimum_budget_digest(result.minimum_feasible_budget)
        identity: dict[str, object] = {
            "schema_version": AUDIT_PREFLIGHT_PLAN_SCHEMA_VERSION,
            "plan_id": plan_id or new_id(),
            "preflight_job_id": job.job_id,
            "preflight_client_request_id": job.client_request_id,
            "operator_principal_id": job.operator_principal_id,
            "authorization_scope_digest": job.authorization_scope_digest,
            "request_schema_version": job.request_schema_version,
            "request_digest": job.request_digest,
            "result_schema_version": result.schema_version,
            "result_digest": result.result_digest,
            "effect_owner_digest": job.effect_owner_digest,
            "source_node_id": job.source_node_id,
            "source_root_identity_digest": job.source_root_identity_digest,
            "repository_identity_digest": result.repository_identity_digest,
            "content_identity_digest": result.content_identity_digest,
            "backend_id": job.backend_id,
            "image_digest": job.image_digest,
            "policy_digest": job.policy_digest,
            "capsule_prepare_proof_digest": result.capsule_prepare_proof_digest,
            "target": target,
            "target_digest": target.target_digest,
            "scope": scope,
            "scope_digest": scope.scope_digest,
            "capability_matrix": result.capability_matrix,
            "capability_matrix_digest": result.capability_matrix.matrix_digest,
            "minimum_feasible_budget": result.minimum_feasible_budget,
            "minimum_feasible_budget_digest": budget_digest,
            "security_context_id": result.canonical_empty_context_id,
            "security_context_digest": result.canonical_empty_context_digest,
            "preflight_completed_at": result.completed_at,
            "created_at": issued_at,
            "expires_at": plan_expiry,
        }
        identity_payload = _plan_identity_payload(identity)
        digest = _domain_digest(AUDIT_PREFLIGHT_PLAN_SCHEMA_VERSION, identity_payload)
        token_issue = token_codec.issue(
            plan_id=str(identity["plan_id"]),
            plan_digest=digest,
        )
        plan = cls.model_validate(
            {
                **identity,
                "plan_digest": digest,
                "token_verifier": token_issue.verifier,
                "status": AuditPreflightPlanStatus.AVAILABLE,
                "state_version": 1,
                "updated_at": issued_at,
            }
        )
        return AuditPreflightPlanIssue(plan=plan, token=token_issue.token)

    def reserve(
        self,
        *,
        audit_id: str,
        client_request_id: str,
        at: datetime,
    ) -> AuditPreflightPlan:
        validate_audit_preflight_plan_transition(self.status, AuditPreflightPlanStatus.RESERVED)
        _validate_safe_id(audit_id, label="audit_id")
        _validate_uuid(client_request_id, label="client_request_id")
        _require_aware(at, label="reserved_at")
        if not self.created_at <= at < self.expires_at:
            raise ValueError("preflight Plan reservation is outside its lifetime")
        return self._validated_replace(
            status=AuditPreflightPlanStatus.RESERVED,
            state_version=self.state_version + 1,
            reserved_audit_id=audit_id,
            reserved_client_request_id=client_request_id,
            reserved_at=at,
            updated_at=at,
        )

    def consume(
        self,
        *,
        audit_id: str,
        start_request_id: str,
        at: datetime,
    ) -> AuditPreflightPlan:
        validate_audit_preflight_plan_transition(self.status, AuditPreflightPlanStatus.CONSUMED)
        _validate_safe_id(audit_id, label="audit_id")
        _validate_uuid(start_request_id, label="start_request_id")
        _require_aware(at, label="consumed_at")
        if self.reserved_audit_id is None or not hmac.compare_digest(
            self.reserved_audit_id,
            audit_id,
        ):
            raise ValueError("preflight Plan is reserved for a different Audit")
        if self.reserved_at is None or not self.reserved_at <= at < self.expires_at:
            raise ValueError("preflight Plan consumption is outside its reserved lifetime")
        return self._validated_replace(
            status=AuditPreflightPlanStatus.CONSUMED,
            state_version=self.state_version + 1,
            consumed_audit_id=audit_id,
            consumed_start_request_id=start_request_id,
            consumed_at=at,
            updated_at=at,
        )

    def revoke(self, *, reason_code: str, at: datetime) -> AuditPreflightPlan:
        validate_audit_preflight_plan_transition(self.status, AuditPreflightPlanStatus.REVOKED)
        _validate_safe_id(reason_code, label="reason_code")
        _require_aware(at, label="revoked_at")
        if at < self.updated_at:
            raise ValueError("preflight Plan revocation cannot precede its current state")
        return self._validated_replace(
            status=AuditPreflightPlanStatus.REVOKED,
            state_version=self.state_version + 1,
            revocation_reason=reason_code,
            revoked_at=at,
            updated_at=at,
        )

    def verify_token(self, token: str, *, codec: AuditPreflightTokenCodec) -> bool:
        return codec.verify(
            token,
            plan_id=self.plan_id,
            plan_digest=self.plan_digest,
            verifier=self.token_verifier,
        )

    def identity_json(self) -> str:
        return _canonical_json(_plan_identity_payload(self))

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))

    def _validated_replace(self, **updates: object) -> AuditPreflightPlan:
        payload = self.model_dump(mode="python")
        payload.update(updates)
        return type(self).model_validate(payload)


@dataclass(frozen=True, slots=True)
class AuditPreflightPlanIssue:
    plan: AuditPreflightPlan
    token: str = field(repr=False)


def _plan_identity_payload(plan: AuditPreflightPlan | Mapping[str, object]) -> dict[str, object]:
    if isinstance(plan, AuditPreflightPlan):
        encoded = plan.model_dump(mode="json")
    else:
        encoded = _JSON_MAPPING_ADAPTER.dump_python(dict(plan), mode="json")
    target = encoded["target"]
    scope = encoded["scope"]
    capability_matrix = encoded["capability_matrix"]
    minimum_budget = encoded["minimum_feasible_budget"]
    assert isinstance(target, dict)
    assert isinstance(scope, dict)
    assert isinstance(capability_matrix, dict)
    assert isinstance(minimum_budget, dict)
    return {
        "schema_version": encoded["schema_version"],
        "plan_id": encoded["plan_id"],
        "preflight_job_id": encoded["preflight_job_id"],
        "preflight_client_request_id": encoded["preflight_client_request_id"],
        "operator_principal_id": encoded["operator_principal_id"],
        "authorization_scope_digest": encoded["authorization_scope_digest"],
        "request_schema_version": encoded["request_schema_version"],
        "request_digest": encoded["request_digest"],
        "result_schema_version": encoded["result_schema_version"],
        "result_digest": encoded["result_digest"],
        "effect_owner_digest": encoded["effect_owner_digest"],
        "source_node_id": encoded["source_node_id"],
        "source_root_identity_digest": encoded["source_root_identity_digest"],
        "repository_identity_digest": encoded["repository_identity_digest"],
        "content_identity_digest": encoded["content_identity_digest"],
        "backend_id": encoded["backend_id"],
        "image_digest": encoded["image_digest"],
        "policy_digest": encoded["policy_digest"],
        "capsule_prepare_proof_digest": encoded["capsule_prepare_proof_digest"],
        "target_schema_version": target["schema_version"],
        "target_digest": encoded["target_digest"],
        "scope_schema_version": scope["schema_version"],
        "scope_digest": encoded["scope_digest"],
        "capability_matrix_schema_version": capability_matrix["schema_version"],
        "capability_matrix_digest": encoded["capability_matrix_digest"],
        "minimum_feasible_budget_schema_version": minimum_budget["schema_version"],
        "minimum_feasible_budget_digest": encoded["minimum_feasible_budget_digest"],
        "security_context_id": encoded["security_context_id"],
        "security_context_digest": encoded["security_context_digest"],
        "preflight_completed_at": encoded["preflight_completed_at"],
        "created_at": encoded["created_at"],
        "expires_at": encoded["expires_at"],
    }


def audit_preflight_plan_digest(plan: AuditPreflightPlan) -> str:
    return _domain_digest(AUDIT_PREFLIGHT_PLAN_SCHEMA_VERSION, _plan_identity_payload(plan))


__all__ = [
    "AUDIT_PREFLIGHT_PLAN_SCHEMA_VERSION",
    "AUDIT_PREFLIGHT_PLAN_SCOPE_SCHEMA_VERSION",
    "AUDIT_PREFLIGHT_PLAN_TARGET_SCHEMA_VERSION",
    "AUDIT_PREFLIGHT_TOKEN_HASH_SCHEMA_VERSION",
    "AUDIT_PREFLIGHT_TOKEN_SCHEMA_VERSION",
    "AUDIT_PREFLIGHT_TOKEN_VERIFIER_SCHEMA_VERSION",
    "AuditPreflightPlan",
    "AuditPreflightPlanIssue",
    "AuditPreflightPlanScope",
    "AuditPreflightPlanStatus",
    "AuditPreflightPlanTarget",
    "AuditPreflightTokenCodec",
    "AuditPreflightTokenIssue",
    "AuditPreflightTokenVerifier",
    "TOKEN_NONCE_BYTES",
    "TOKEN_NONCE_WIRE_LENGTH",
    "TOKEN_WIRE_LENGTH",
    "audit_preflight_minimum_budget_digest",
    "audit_preflight_plan_can_transition",
    "audit_preflight_plan_digest",
    "audit_preflight_plan_scope_digest",
    "audit_preflight_plan_target_digest",
    "audit_preflight_token_hash",
    "validate_audit_preflight_plan_transition",
]
