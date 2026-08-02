"""Strict, infrastructure-independent domain contracts for RiftX Code Audit."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from .base import DomainModel, new_id, utc_now
from .enums import RunStatus
from .errors import InvalidStateTransitionError

AUDIT_CONTRACT_SCHEMA_VERSION: Literal["riftx.audit-contract/v1"] = (
    "riftx.audit-contract/v1"
)
AUDIT_CAPABILITY_MATRIX_SCHEMA_VERSION: Literal["riftx.audit-capability-matrix/v1"] = (
    "riftx.audit-capability-matrix/v1"
)
AUDIT_BUDGET_SCHEMA_VERSION: Literal["riftx.audit-budget/v1"] = "riftx.audit-budget/v1"
AUDIT_EXECUTION_SELECTION_SCHEMA_VERSION: Literal[
    "riftx.audit-execution-selection/v1"
] = "riftx.audit-execution-selection/v1"
AUDIT_MODEL_EGRESS_SCHEMA_VERSION: Literal["riftx.model-data-egress/v1"] = (
    "riftx.model-data-egress/v1"
)
AUDIT_MODEL_DISCLOSURE_SCHEMA_VERSION: Literal[
    "riftx.model-retention-training-disclosure/v1"
] = "riftx.model-retention-training-disclosure/v1"
AUDIT_POLICY_DOCUMENT_SCHEMA_VERSION: Literal["riftx.versioned-policy-document/v1"] = (
    "riftx.versioned-policy-document/v1"
)
AUDIT_VALIDATION_POLICY_SCHEMA_VERSION: Literal["riftx.validation-policy/v1"] = (
    "riftx.validation-policy/v1"
)
MAX_AUDIT_CONTRACT_BYTES = 256 * 1024
MAX_AUDIT_POLICY_BYTES = 64 * 1024
MAX_AUDIT_CAPABILITY_ENTRIES = 512
MAX_CANONICAL_JSON_DEPTH = 64
MAX_CANONICAL_JSON_NODES = 10_000
MAX_CANONICAL_JSON_KEY_BYTES = 1_024
MAX_CANONICAL_JSON_STRING_BYTES = 64 * 1024

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$"
_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,255}$"
_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+:/\-]{0,127}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_WINDOWS_DRIVE_PREFIX = re.compile(r"[A-Za-z]:[\\/]")
_WINDOWS_UNC_PREFIX = re.compile(r"(?:\\\\|//)[^\\/]+[\\/][^\\/]+")
_DNS_ORIGIN_LABEL_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)

type AuditId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_ID_PATTERN),
]
type AuditNodeId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=_ID_PATTERN),
]
type AuditToken = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256, pattern=_TOKEN_PATTERN),
]
type AuditVersion = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_VERSION_PATTERN),
]
type Sha256Digest = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=_DIGEST_PATTERN),
]


def _domain_digest(domain: str, payload: bytes) -> str:
    """Hash canonical bytes with an explicit RiftX-owned domain separator."""

    return hashlib.sha256(domain.encode("ascii") + b"\0" + payload).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key is not canonical")
        result[key] = value
    return result


def _parse_canonical_json(value: str, *, maximum_bytes: int, label: str) -> object:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{label} exceeds the {maximum_bytes}-byte limit")
    try:
        parsed = json.loads(value, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc
    _validate_json_shape(parsed, label=label)
    try:
        canonical = _canonical_json(parsed)
    except (RecursionError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc
    if canonical != value:
        raise ValueError(f"{label} is not in canonical JSON form")
    return parsed


def _validate_json_shape(value: object, *, label: str) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_CANONICAL_JSON_NODES:
            raise ValueError(f"{label} exceeds the JSON node limit")
        if depth > MAX_CANONICAL_JSON_DEPTH:
            raise ValueError(f"{label} exceeds the JSON depth limit")
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} JSON object keys must be strings")
                if len(key.encode("utf-8")) > MAX_CANONICAL_JSON_KEY_BYTES:
                    raise ValueError(f"{label} exceeds the JSON key byte limit")
                stack.append((child, depth + 1))
        elif isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            if len(current.encode("utf-8")) > MAX_CANONICAL_JSON_STRING_BYTES:
                raise ValueError(f"{label} exceeds the JSON string byte limit")
        elif current is not None and not isinstance(current, (bool, int, float)):
            raise ValueError(f"{label} contains a non-JSON value")


def _policy_document_digest(policy_schema_version: str, canonical_json: str) -> str:
    payload = policy_schema_version.encode("ascii") + b"\0" + canonical_json.encode("utf-8")
    return _domain_digest(AUDIT_POLICY_DOCUMENT_SCHEMA_VERSION, payload)


def _ensure_sorted_unique(
    values: tuple[Any, ...],
    *,
    key: Any,
    label: str,
) -> tuple[Any, ...]:
    keys = tuple(key(value) for value in values)
    if keys != tuple(sorted(keys)):
        raise ValueError(f"{label} must use canonical sorted order")
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must not contain duplicate identities")
    return values


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


def _canonical_https_origin(value: str) -> str:
    if "*" in value or "\\" in value:
        raise ValueError("remote origins must be explicit HTTPS origins")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("remote origin is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise ValueError("remote origins must be origin-only HTTPS URLs")
    raw_host = parsed.hostname.rstrip(".")
    if not raw_host or "%" in raw_host:
        raise ValueError("remote origin hostname is invalid")
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("remote origin hostname is invalid") from exc
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or any(_DNS_ORIGIN_LABEL_PATTERN.fullmatch(label) is None for label in labels)
            or host.replace(".", "").isdigit()
        ):
            raise ValueError("remote origin hostname is invalid") from None
    else:
        host = address.compressed
        if address.version == 6:
            host = f"[{host}]"
    normalized_port = None if port == 443 else port
    return f"https://{host}{f':{normalized_port}' if normalized_port is not None else ''}"


class AuditStrictModel(DomainModel):
    """Fail-closed base for Code Audit contracts and immutable aggregates."""

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
        """Disallow Pydantic's unvalidated update escape hatch."""

        if update:
            raise TypeError("Code Audit models forbid unvalidated model_copy updates")
        return super().model_copy(deep=deep)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Disable Pydantic's deprecated, wholly unvalidated copy implementation."""

        del include, exclude, update, deep
        raise TypeError(
            "Code Audit models forbid deprecated copy; use model_copy without update"
        )


class SourceTargetKind(StrEnum):
    REVISION = "revision"
    WORKING_TREE = "working_tree"


class AuditMode(StrEnum):
    STANDARD = "standard"
    DEEP = "deep"
    DIFF = "diff"


class AnalysisProfile(StrEnum):
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"


class AuditPurpose(StrEnum):
    PRIMARY = "primary"
    VALIDATION_FOLLOWUP = "validation_followup"
    RETEST = "retest"


class ValidationPolicy(StrEnum):
    STATIC_ONLY = "static_only"
    ISOLATED_BUILD = "isolated_build"
    ISOLATED_TEST = "isolated_test"
    ISOLATED_POC = "isolated_poc"
    ISOLATED_FIX_AND_RETEST = "isolated_fix_and_retest"


class ModelDataEgressMode(StrEnum):
    LOCAL_ONLY = "local_only"
    REMOTE_REDACTED = "remote_redacted"


class ModelExecutionLocality(StrEnum):
    LOCAL_CONTROLLED = "local_controlled"
    REMOTE_PROVIDER = "remote_provider"


class ModelTrainingUsage(StrEnum):
    NOT_USED_FOR_TRAINING = "not_used_for_training"
    MAY_BE_USED_FOR_TRAINING = "may_be_used_for_training"


class AuditLifecycleStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    PREFLIGHTING = "preflighting"
    SNAPSHOTTING = "snapshotting"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSING = "pausing"
    PAUSED = "paused"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    FAILING = "failing"
    CLEANING = "cleaning"
    SEALING_CORE = "sealing_core"
    REPORTING = "reporting"
    PACKAGING = "packaging"
    COMPLETED = "completed"
    COMPLETED_PARTIAL = "completed_partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AuditPhase(StrEnum):
    AUTHORIZE_AND_FREEZE = "authorize_and_freeze"
    MAP_SCOPE = "map_scope"
    DETERMINISTIC_PROBE = "deterministic_probe"
    THREAT_MODEL = "threat_model"
    AGENT_HUNT = "agent_hunt"
    RECONCILE = "reconcile"
    PROVE = "prove"
    COMPOSE_RISK = "compose_risk"
    COMPARE_BASELINE = "compare_baseline"
    VALIDATE_CLOSURE = "validate_closure"
    CLEANUP = "cleanup"
    SEAL_CORE = "seal_core"
    GENERATE_REPORTS = "generate_reports"
    PACKAGE_AND_PUBLISH = "package_and_publish"


class AuditClosureStatus(StrEnum):
    COMPLETE_UNDER_DECLARED_SCOPE = "complete_under_declared_scope"
    COMPLETE_WITH_POLICY_EXCLUSIONS = "complete_with_policy_exclusions"
    PARTIAL_CAPABILITY = "partial_capability"
    PARTIAL_BUDGET = "partial_budget"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AuditTerminalOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AuditPublicationStatus(StrEnum):
    NOT_STARTED = "not_started"
    SEALING_CORE = "sealing_core"
    REPORT_PENDING = "report_pending"
    REPORTING = "reporting"
    PACKAGING = "packaging"
    PUBLISHED = "published"
    SEAL_FAILED = "seal_failed"
    REPORT_FAILED = "report_failed"
    PACKAGE_FAILED = "package_failed"


class AuditPhaseRequirement(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "not_applicable"


class AuditStartMissingOutcome(StrEnum):
    REJECT_START = "reject_start"
    CONTINUE_WITHOUT_CLAIM = "continue_without_claim"
    NOT_APPLICABLE = "not_applicable"


class AuditRuntimeMissingOutcome(StrEnum):
    PARTIAL_CAPABILITY = "partial_capability"
    FAILED = "failed"
    CONTINUE_WITHOUT_CLAIM = "continue_without_claim"
    NOT_APPLICABLE = "not_applicable"


class AuditLanguageTier(StrEnum):
    TIER_A = "tier_a"
    TIER_B = "tier_b"
    TIER_C = "tier_c"
    UNSUPPORTED = "unsupported"


class SourceTarget(AuditStrictModel):
    repository_path: str = Field(min_length=1, max_length=4096, repr=False)
    kind: SourceTargetKind
    revision: str = Field(min_length=1, max_length=1024)
    base_revision: str | None = Field(default=None, min_length=1, max_length=1024)
    include_untracked: bool = False

    @field_validator("repository_path")
    @classmethod
    def validate_repository_path(cls, value: str) -> str:
        _validate_bounded_text(value, label="repository_path", maximum_bytes=4096)
        if value.startswith("~"):
            raise ValueError("repository_path must not use home-directory expansion")
        windows_drive = _WINDOWS_DRIVE_PREFIX.match(value) is not None
        windows_unc = _WINDOWS_UNC_PREFIX.match(value) is not None
        if not (value.startswith("/") or windows_drive or windows_unc):
            raise ValueError("repository_path must be an absolute node-local path")
        if windows_drive:
            if (
                "\\" in value
                or not value[0].isupper()
                or (len(value) > 3 and value.endswith("/"))
                or "//" in value[2:]
            ):
                raise ValueError("repository_path must use canonical Windows drive syntax")
        elif windows_unc:
            components = value[2:].split("/") if value.startswith("//") else []
            if (
                "\\" in value
                or len(components) < 2
                or any(not component for component in components)
                or components[0] != components[0].lower()
            ):
                raise ValueError("repository_path must use canonical UNC syntax")
        elif value != "/" and (value.endswith("/") or "//" in value):
            raise ValueError("repository_path must use canonical separators")
        if any(segment in {".", ".."} for segment in value.replace("\\", "/").split("/")):
            raise ValueError("repository_path must not contain dot path segments")
        return value

    @field_validator("revision", "base_revision")
    @classmethod
    def validate_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validate_bounded_text(value, label="revision", maximum_bytes=1024)
        if value.startswith("-"):
            raise ValueError("revision must not be option-shaped")
        return value

    @model_validator(mode="after")
    def validate_target_kind(self) -> SourceTarget:
        if self.kind is SourceTargetKind.REVISION and self.include_untracked:
            raise ValueError("revision targets cannot include untracked working-tree files")
        return self


class AuditBudget(AuditStrictModel):
    schema_version: Literal["riftx.audit-budget/v1"] = AUDIT_BUDGET_SCHEMA_VERSION
    max_wall_seconds: int = Field(strict=True, ge=1, le=7_200)
    max_detector_jobs: int = Field(strict=True, ge=1, le=4_096)
    max_worker_jobs: int = Field(strict=True, ge=1, le=64)
    max_epochs: int = Field(strict=True, ge=1, le=8)
    max_model_calls: int = Field(strict=True, ge=0, le=100)
    max_input_tokens: int = Field(strict=True, ge=0, le=2_000_000)
    max_output_tokens: int = Field(strict=True, ge=0, le=200_000)
    max_read_bytes: int = Field(strict=True, ge=1, le=2_147_483_648)
    max_candidates: int = Field(strict=True, ge=1, le=1_000)
    max_signals: int = Field(strict=True, ge=1, le=16_000)
    max_dynamic_validations: int = Field(strict=True, ge=0, le=1_000)
    max_artifact_output_bytes: int = Field(strict=True, ge=1, le=268_435_456)

    @property
    def digest(self) -> str:
        payload = _canonical_json(self.model_dump(mode="json")).encode("utf-8")
        return _domain_digest(AUDIT_BUDGET_SCHEMA_VERSION, payload)


class CapabilityMissingOutcome(AuditStrictModel):
    start: AuditStartMissingOutcome
    runtime: AuditRuntimeMissingOutcome


class AuditCapabilityVersion(AuditStrictModel):
    minimum_version: AuditVersion
    component_digest: Sha256Digest


class AuditCapabilityRequirement(AuditStrictModel):
    matrix_schema_version: Literal["riftx.audit-capability-matrix/v1"] = (
        AUDIT_CAPABILITY_MATRIX_SCHEMA_VERSION
    )
    phase: AuditPhase
    capability_id: AuditToken
    requirement: AuditPhaseRequirement
    scope_classes: tuple[AuditToken, ...] = Field(default_factory=tuple, max_length=64)
    language_tiers: tuple[AuditLanguageTier, ...] = Field(default_factory=tuple, max_length=4)
    provider_id: AuditToken | None = None
    node_id: AuditId | None = None
    backend_id: AuditToken | None = None
    min_version_and_digest: AuditCapabilityVersion | None = None
    proof_kind: AuditToken | None = None
    proof_digest: Sha256Digest | None = None
    missing_outcome: CapabilityMissingOutcome
    reason_code: AuditToken | None = None

    @field_validator("scope_classes")
    @classmethod
    def validate_scope_classes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _ensure_sorted_unique(values, key=lambda value: value, label="scope_classes")

    @field_validator("language_tiers")
    @classmethod
    def validate_language_tiers(
        cls, values: tuple[AuditLanguageTier, ...]
    ) -> tuple[AuditLanguageTier, ...]:
        return _ensure_sorted_unique(
            values,
            key=lambda value: value.value,
            label="language_tiers",
        )

    @model_validator(mode="after")
    def validate_requirement_contract(self) -> AuditCapabilityRequirement:
        has_target = any((self.provider_id, self.node_id, self.backend_id))
        has_version = self.min_version_and_digest is not None
        has_proof = self.proof_kind is not None and self.proof_digest is not None
        partial_proof = (self.proof_kind is None) != (self.proof_digest is None)
        if partial_proof:
            raise ValueError("capability proof_kind and proof_digest must be supplied together")

        if self.requirement is AuditPhaseRequirement.REQUIRED:
            if self.missing_outcome.start is not AuditStartMissingOutcome.REJECT_START:
                raise ValueError("required capability must reject Start when missing")
            if self.missing_outcome.runtime not in {
                AuditRuntimeMissingOutcome.PARTIAL_CAPABILITY,
                AuditRuntimeMissingOutcome.FAILED,
            }:
                raise ValueError("required capability needs a fail-closed runtime outcome")
            if not (has_target and has_version and has_proof):
                raise ValueError("required capability must freeze target, version, and proof")
        elif self.requirement is AuditPhaseRequirement.OPTIONAL:
            if self.missing_outcome != CapabilityMissingOutcome(
                start=AuditStartMissingOutcome.CONTINUE_WITHOUT_CLAIM,
                runtime=AuditRuntimeMissingOutcome.CONTINUE_WITHOUT_CLAIM,
            ):
                raise ValueError("optional capability must continue without a capability claim")
            if not (has_target and has_version and has_proof):
                raise ValueError("optional enhancement must identify its frozen implementation")
            if self.reason_code is None:
                raise ValueError("optional enhancement requires a reason_code")
        else:
            if self.missing_outcome != CapabilityMissingOutcome(
                start=AuditStartMissingOutcome.NOT_APPLICABLE,
                runtime=AuditRuntimeMissingOutcome.NOT_APPLICABLE,
            ):
                raise ValueError("not-applicable capability needs not-applicable outcomes")
            if any((has_target, has_version, has_proof)):
                raise ValueError("not-applicable capability cannot carry implementation proof")
            if self.reason_code is None:
                raise ValueError("not-applicable capability requires a reason_code")
        return self

    @property
    def identity(self) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
        return (
            self.phase.value,
            self.capability_id,
            self.scope_classes,
            tuple(tier.value for tier in self.language_tiers),
        )


class AuditCapabilityMatrix(AuditStrictModel):
    schema_version: Literal["riftx.audit-capability-matrix/v1"] = (
        AUDIT_CAPABILITY_MATRIX_SCHEMA_VERSION
    )
    entries: tuple[AuditCapabilityRequirement, ...] = Field(
        min_length=1,
        max_length=MAX_AUDIT_CAPABILITY_ENTRIES,
    )

    @field_validator("entries")
    @classmethod
    def validate_entries(
        cls, values: tuple[AuditCapabilityRequirement, ...]
    ) -> tuple[AuditCapabilityRequirement, ...]:
        return _ensure_sorted_unique(values, key=lambda value: value.identity, label="capabilities")

    @property
    def digest(self) -> str:
        payload = _canonical_json(self.model_dump(mode="json")).encode("utf-8")
        return _domain_digest(AUDIT_CAPABILITY_MATRIX_SCHEMA_VERSION, payload)

    def global_requirement(self, capability_id: str) -> AuditCapabilityRequirement | None:
        matches = [
            entry
            for entry in self.entries
            if entry.capability_id == capability_id
            and not entry.scope_classes
            and not entry.language_tiers
        ]
        if len(matches) > 1:
            raise ValueError(f"capability {capability_id} has multiple global requirements")
        return matches[0] if matches else None


class VersionedComponentRef(AuditStrictModel):
    component_id: AuditToken
    version: AuditVersion
    digest: Sha256Digest


class SchemaVersionRef(AuditStrictModel):
    schema_id: AuditToken
    version: AuditVersion


class VersionedCanonicalPolicy(AuditStrictModel):
    document_schema_version: Literal["riftx.versioned-policy-document/v1"] = (
        AUDIT_POLICY_DOCUMENT_SCHEMA_VERSION
    )
    policy_schema_version: AuditVersion
    canonical_json: str = Field(min_length=2, max_length=MAX_AUDIT_POLICY_BYTES, repr=False)
    digest: Sha256Digest

    @model_validator(mode="after")
    def validate_canonical_policy(self) -> VersionedCanonicalPolicy:
        parsed = _parse_canonical_json(
            self.canonical_json,
            maximum_bytes=MAX_AUDIT_POLICY_BYTES,
            label="canonical policy JSON",
        )
        if not isinstance(parsed, dict):
            raise ValueError("canonical policy JSON must contain an object")
        expected = _policy_document_digest(
            self.policy_schema_version,
            self.canonical_json,
        )
        if not hmac.compare_digest(self.digest, expected):
            raise ValueError("canonical policy digest does not match its document")
        return self

    @classmethod
    def from_value(
        cls,
        *,
        policy_schema_version: str,
        value: Mapping[str, JsonValue],
    ) -> VersionedCanonicalPolicy:
        _validate_json_shape(value, label="canonical policy JSON")
        canonical = _canonical_json(dict(value))
        return cls(
            policy_schema_version=policy_schema_version,
            canonical_json=canonical,
            digest=_policy_document_digest(policy_schema_version, canonical),
        )


class ModelRetentionTrainingDisclosure(AuditStrictModel):
    schema_version: Literal["riftx.model-retention-training-disclosure/v1"] = (
        AUDIT_MODEL_DISCLOSURE_SCHEMA_VERSION
    )
    data_residency_regions: tuple[AuditToken, ...] = Field(min_length=1, max_length=32)
    retention_days: int = Field(strict=True, ge=0, le=3_650)
    training_usage: ModelTrainingUsage
    provider_terms_version: AuditVersion
    provider_terms_digest: Sha256Digest

    @field_validator("data_residency_regions")
    @classmethod
    def validate_data_residency_regions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        canonical = _ensure_sorted_unique(
            values,
            key=lambda value: value,
            label="data_residency_regions",
        )
        forbidden = {"unknown", "undisclosed", "unspecified"}
        if any(value.casefold() in forbidden for value in canonical):
            raise ValueError("data residency disclosure cannot be unknown")
        return canonical


class ModelDataEgressPolicy(AuditStrictModel):
    schema_version: Literal["riftx.model-data-egress/v1"] = AUDIT_MODEL_EGRESS_SCHEMA_VERSION
    mode: ModelDataEgressMode
    model_profile_digest: Sha256Digest | None = None
    endpoint_origin_digest: Sha256Digest | None = None
    provider_display_name: str | None = Field(default=None, min_length=1, max_length=256)
    execution_locality: ModelExecutionLocality | None = None
    retention_training_disclosure: ModelRetentionTrainingDisclosure | None = None
    allowed_scope_classes: tuple[AuditToken, ...] = Field(default_factory=tuple, max_length=64)
    allowed_remote_origins: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    max_bytes_per_call: int = Field(strict=True, ge=1, le=131_072)
    max_bytes_per_audit: int = Field(strict=True, ge=1, le=16_777_216)
    redaction_policy_version: AuditVersion | None = None
    redaction_policy_digest: Sha256Digest | None = None
    operator_consent_requirement_digest: Sha256Digest | None = None
    operator_consent_at: AwareDatetime | None = None
    policy_digest: Sha256Digest | None = None

    @field_validator("provider_display_name")
    @classmethod
    def validate_provider_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_bounded_text(
            value,
            label="provider_display_name",
            maximum_bytes=256,
        )

    @field_validator("allowed_scope_classes")
    @classmethod
    def validate_allowed_scope_classes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _ensure_sorted_unique(
            values,
            key=lambda value: value,
            label="allowed_scope_classes",
        )

    @field_validator("allowed_remote_origins")
    @classmethod
    def validate_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _ensure_sorted_unique(values, key=lambda value: value, label="remote origins")
        for value in values:
            _validate_bounded_text(value, label="remote origin", maximum_bytes=2048)
            if not value.isascii():
                raise ValueError("remote origins must use canonical ASCII HTTPS origins")
            if _canonical_https_origin(value) != value:
                raise ValueError("remote origins must be canonical HTTPS origins")
        return values

    @model_validator(mode="after")
    def validate_egress_policy(self) -> ModelDataEgressPolicy:
        if self.max_bytes_per_call > self.max_bytes_per_audit:
            raise ValueError("max_bytes_per_call must not exceed max_bytes_per_audit")
        has_redaction_version = self.redaction_policy_version is not None
        has_redaction_digest = self.redaction_policy_digest is not None
        if has_redaction_version != has_redaction_digest:
            raise ValueError("redaction policy version and digest must be supplied together")
        active_profile = self.model_profile_digest is not None
        if self.mode is ModelDataEgressMode.REMOTE_REDACTED:
            if not self.allowed_remote_origins:
                raise ValueError("remote_redacted requires at least one approved origin")
            if not has_redaction_digest:
                raise ValueError("remote_redacted requires a redaction policy")
            expected_origin_digest = _domain_digest(
                "riftx.model-endpoint-origins/v1",
                _canonical_json(self.allowed_remote_origins).encode("utf-8"),
            )
            if self.endpoint_origin_digest is None or not hmac.compare_digest(
                self.endpoint_origin_digest,
                expected_origin_digest,
            ):
                raise ValueError("remote_redacted endpoint_origin_digest does not match origins")
            if self.execution_locality is not ModelExecutionLocality.REMOTE_PROVIDER:
                raise ValueError("remote_redacted requires remote_provider execution locality")
        elif self.allowed_remote_origins or has_redaction_digest:
            raise ValueError("local_only cannot carry remote origins or redaction policy")
        elif (
            active_profile
            and self.execution_locality is not ModelExecutionLocality.LOCAL_CONTROLLED
        ):
            raise ValueError(
                "local_only model profile requires local_controlled execution locality"
            )

        if active_profile:
            if (
                self.endpoint_origin_digest is None
                or self.provider_display_name is None
                or self.execution_locality is None
                or self.retention_training_disclosure is None
                or not self.allowed_scope_classes
                or self.operator_consent_at is None
            ):
                raise ValueError(
                    "active model profile requires complete egress disclosure and consent"
                )
        elif any(
            (
                self.endpoint_origin_digest,
                self.provider_display_name,
                self.execution_locality,
                self.retention_training_disclosure,
                self.allowed_scope_classes,
                self.operator_consent_requirement_digest,
                self.operator_consent_at,
            )
        ):
            raise ValueError("inactive model egress policy cannot carry active profile metadata")

        if active_profile:
            consent_payload = _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={
                        "operator_consent_requirement_digest",
                        "operator_consent_at",
                        "policy_digest",
                    },
                )
            ).encode("utf-8")
            expected_consent_requirement = _domain_digest(
                "riftx.model-egress-consent-requirement/v1",
                consent_payload,
            )
            if self.operator_consent_requirement_digest is None:
                object.__setattr__(
                    self,
                    "operator_consent_requirement_digest",
                    expected_consent_requirement,
                )
            elif not hmac.compare_digest(
                self.operator_consent_requirement_digest,
                expected_consent_requirement,
            ):
                raise ValueError(
                    "operator consent requirement does not match model egress disclosure"
                )

        digest_payload = _canonical_json(
            self.model_dump(mode="json", exclude={"policy_digest"})
        ).encode("utf-8")
        expected_policy_digest = _domain_digest(
            AUDIT_MODEL_EGRESS_SCHEMA_VERSION,
            digest_payload,
        )
        if self.policy_digest is None:
            object.__setattr__(self, "policy_digest", expected_policy_digest)
        elif not hmac.compare_digest(self.policy_digest, expected_policy_digest):
            raise ValueError("model egress policy_digest does not match its frozen fields")
        return self


class AuditExecutionSelection(AuditStrictModel):
    schema_version: Literal["riftx.audit-execution-selection/v1"] = (
        AUDIT_EXECUTION_SELECTION_SCHEMA_VERSION
    )
    source_node_id: AuditNodeId
    source_ingest_backend_id: AuditToken
    source_ingest_backend_digest: Sha256Digest
    source_prepare_proof_digest: Sha256Digest
    selected_node_id: AuditNodeId
    required_backend_id: AuditToken
    analysis_backend_digest: Sha256Digest
    analysis_prepare_proof_digest: Sha256Digest
    analysis_image_digest: Sha256Digest
    analysis_policy_digest: Sha256Digest
    snapshot_hydration_policy_digest: Sha256Digest
    selection_policy_version: AuditVersion
    eligible_candidates_digest: Sha256Digest


_GLOBAL_REQUIRED_CAPABILITIES = frozenset(
    {"analysis_backend", "closure", "core_seal", "snapshot_store", "source_ingest"}
)
_HYBRID_REQUIRED_CAPABILITIES = frozenset(
    {
        "agent_hunt",
        "model_adapter",
        "model_transport",
        "proof",
        "skeptic",
        "threat_model",
        "typed_output",
    }
)
_DEEP_REQUIRED_CAPABILITIES = frozenset(
    {"child_workflow", "epoch_budget", "minimum_visits"}
)
_DIFF_REQUIRED_CAPABILITIES = frozenset(
    {"diff_mapper", "paired_closure", "risk_comparator", "sealed_base_head"}
)
_DYNAMIC_REQUIRED_CAPABILITIES = frozenset(
    {"dynamic_approval", "dynamic_egress", "dynamic_sandbox"}
)
_STATIC_NOT_APPLICABLE_CAPABILITIES = frozenset(
    {"isolated_build", "isolated_fix_and_retest", "isolated_poc", "isolated_test"}
)
_VALIDATION_POLICY_CAPABILITY: Mapping[ValidationPolicy, str] = {
    ValidationPolicy.ISOLATED_BUILD: "isolated_build",
    ValidationPolicy.ISOLATED_TEST: "isolated_test",
    ValidationPolicy.ISOLATED_POC: "isolated_poc",
    ValidationPolicy.ISOLATED_FIX_AND_RETEST: "isolated_fix_and_retest",
}
_KNOWN_CAPABILITY_PHASES: Mapping[str, AuditPhase] = {
    "agent_hunt": AuditPhase.AGENT_HUNT,
    "analysis_backend": AuditPhase.AUTHORIZE_AND_FREEZE,
    "child_workflow": AuditPhase.AGENT_HUNT,
    "closure": AuditPhase.VALIDATE_CLOSURE,
    "core_seal": AuditPhase.SEAL_CORE,
    "diff_mapper": AuditPhase.MAP_SCOPE,
    "dynamic_approval": AuditPhase.PROVE,
    "dynamic_egress": AuditPhase.PROVE,
    "dynamic_sandbox": AuditPhase.PROVE,
    "epoch_budget": AuditPhase.AGENT_HUNT,
    "isolated_build": AuditPhase.PROVE,
    "isolated_fix_and_retest": AuditPhase.PROVE,
    "isolated_poc": AuditPhase.PROVE,
    "isolated_test": AuditPhase.PROVE,
    "minimum_visits": AuditPhase.AGENT_HUNT,
    "model_adapter": AuditPhase.THREAT_MODEL,
    "model_transport": AuditPhase.THREAT_MODEL,
    "paired_closure": AuditPhase.COMPARE_BASELINE,
    "proof": AuditPhase.PROVE,
    "risk_comparator": AuditPhase.COMPARE_BASELINE,
    "sealed_base_head": AuditPhase.AUTHORIZE_AND_FREEZE,
    "skeptic": AuditPhase.RECONCILE,
    "snapshot_store": AuditPhase.AUTHORIZE_AND_FREEZE,
    "source_ingest": AuditPhase.AUTHORIZE_AND_FREEZE,
    "threat_model": AuditPhase.THREAT_MODEL,
    "typed_output": AuditPhase.THREAT_MODEL,
}


class AuditContract(AuditStrictModel):
    schema_version: Literal["riftx.audit-contract/v1"] = AUDIT_CONTRACT_SCHEMA_VERSION
    audit_id: AuditId
    project_id: AuditId
    source_target: SourceTarget
    mode: AuditMode
    analysis_profile: AnalysisProfile
    baseline_audit_id: AuditId | None = None
    scope_capture_policy: VersionedCanonicalPolicy
    detectors: tuple[VersionedComponentRef, ...] = Field(min_length=1, max_length=256)
    rulepacks: tuple[VersionedComponentRef, ...] = Field(default_factory=tuple, max_length=128)
    parsers: tuple[VersionedComponentRef, ...] = Field(default_factory=tuple, max_length=128)
    # The selected profile is copied onto the authoritative Run, whose stable
    # persistence contract is VARCHAR(255).  Keep this narrower than a generic
    # 256-character AuditToken so a domain-valid Audit cannot fail at that FK
    # binding boundary on length-enforcing databases.
    model_profile: AuditToken | None = Field(default=None, max_length=255)
    model_profile_digest: Sha256Digest | None = None
    model_data_egress_policy: ModelDataEgressPolicy
    validation_policy: ValidationPolicy
    validation_policy_document: VersionedCanonicalPolicy
    budget: AuditBudget
    execution_selection: AuditExecutionSelection
    capability_matrix: AuditCapabilityMatrix
    policy_digest: Sha256Digest
    config_digest: Sha256Digest
    schema_versions: tuple[SchemaVersionRef, ...] = Field(min_length=1, max_length=128)

    @field_validator("detectors", "rulepacks", "parsers")
    @classmethod
    def validate_components(
        cls, values: tuple[VersionedComponentRef, ...]
    ) -> tuple[VersionedComponentRef, ...]:
        return _ensure_sorted_unique(
            values,
            key=lambda value: value.component_id,
            label="versioned components",
        )

    @field_validator("schema_versions")
    @classmethod
    def validate_schema_versions(
        cls, values: tuple[SchemaVersionRef, ...]
    ) -> tuple[SchemaVersionRef, ...]:
        return _ensure_sorted_unique(
            values,
            key=lambda value: value.schema_id,
            label="schema versions",
        )

    @model_validator(mode="after")
    def validate_contract(self) -> AuditContract:
        if self.baseline_audit_id == self.audit_id:
            raise ValueError("baseline_audit_id must refer to a different Audit")
        if self.mode is AuditMode.DEEP and self.analysis_profile is not AnalysisProfile.HYBRID:
            raise ValueError("Deep Audit mode requires the hybrid analysis profile")
        if self.mode is AuditMode.DEEP and self.budget.max_epochs < 2:
            raise ValueError("Deep Audit mode requires at least two epochs")
        if self.mode is AuditMode.DIFF:
            if self.source_target.base_revision is None:
                raise ValueError("Diff Audit mode requires a base revision")
            if self.source_target.base_revision == self.source_target.revision:
                raise ValueError("Diff base and head revisions must differ")
        elif self.source_target.base_revision is not None:
            raise ValueError("base_revision is only valid for Diff Audit mode")

        has_model_profile = self.model_profile is not None
        has_model_digest = self.model_profile_digest is not None
        if has_model_profile != has_model_digest:
            raise ValueError("model profile and digest must be supplied together")
        if self.analysis_profile is AnalysisProfile.DETERMINISTIC:
            if has_model_profile:
                raise ValueError("deterministic analysis cannot freeze an active model profile")
            if self.model_data_egress_policy.mode is not ModelDataEgressMode.LOCAL_ONLY:
                raise ValueError("deterministic analysis cannot enable remote model egress")
            if self.model_data_egress_policy.model_profile_digest is not None:
                raise ValueError("deterministic analysis cannot bind model egress metadata")
            if (
                self.budget.max_model_calls
                or self.budget.max_input_tokens
                or self.budget.max_output_tokens
            ):
                raise ValueError("deterministic analysis must reserve zero model calls and tokens")
        else:
            if not has_model_profile:
                raise ValueError("hybrid analysis requires a model profile and digest")
            assert self.model_profile_digest is not None
            if not hmac.compare_digest(
                self.model_data_egress_policy.model_profile_digest or "",
                self.model_profile_digest,
            ):
                raise ValueError("model egress policy must bind the selected model profile")
            if self.model_data_egress_policy.operator_consent_requirement_digest is None:
                raise ValueError("hybrid analysis requires a frozen model consent requirement")
            if (
                self.budget.max_model_calls < 1
                or self.budget.max_input_tokens < 1
                or self.budget.max_output_tokens < 1
            ):
                raise ValueError("hybrid analysis requires non-zero model call and token budgets")

        if self.validation_policy is ValidationPolicy.STATIC_ONLY:
            if self.budget.max_dynamic_validations != 0:
                raise ValueError("static_only requires zero dynamic validation budget")
        elif self.budget.max_dynamic_validations < 1:
            raise ValueError("dynamic validation policy requires a non-zero validation budget")

        if (
            self.validation_policy_document.policy_schema_version
            != AUDIT_VALIDATION_POLICY_SCHEMA_VERSION
        ):
            raise ValueError("validation policy document uses an unsupported schema")
        validation_document = _parse_canonical_json(
            self.validation_policy_document.canonical_json,
            maximum_bytes=MAX_AUDIT_POLICY_BYTES,
            label="validation policy document",
        )
        assert isinstance(validation_document, dict)
        if validation_document.get("validation_policy") != self.validation_policy.value:
            raise ValueError("validation policy document does not bind validation_policy")

        self._validate_capability_contract()
        return self

    def _validate_capability_contract(self) -> None:
        expected_required = set(_GLOBAL_REQUIRED_CAPABILITIES)
        declared_not_applicable: set[str] = set()
        content_capabilities = {
            *(f"detector:{component.component_id}" for component in self.detectors),
            *(f"parser:{component.component_id}" for component in self.parsers),
        }
        if self.analysis_profile is AnalysisProfile.HYBRID:
            expected_required.update(_HYBRID_REQUIRED_CAPABILITIES)
        else:
            declared_not_applicable.update(_HYBRID_REQUIRED_CAPABILITIES)
        if self.mode is AuditMode.DEEP:
            expected_required.update(_DEEP_REQUIRED_CAPABILITIES)
        if self.mode is AuditMode.DIFF:
            expected_required.update(_DIFF_REQUIRED_CAPABILITIES)
        if self.validation_policy is not ValidationPolicy.STATIC_ONLY:
            expected_required.update(_DYNAMIC_REQUIRED_CAPABILITIES)
            selected_validation = _VALIDATION_POLICY_CAPABILITY[self.validation_policy]
            expected_required.add(selected_validation)
            declared_not_applicable.update(
                _STATIC_NOT_APPLICABLE_CAPABILITIES - {selected_validation}
            )
        else:
            declared_not_applicable.update(_STATIC_NOT_APPLICABLE_CAPABILITIES)

        for capability_id in sorted(expected_required):
            entry = self.capability_matrix.global_requirement(capability_id)
            if entry is None or entry.requirement is not AuditPhaseRequirement.REQUIRED:
                raise ValueError(f"capability {capability_id} must be globally required")
            if entry.phase is not _KNOWN_CAPABILITY_PHASES[capability_id]:
                raise ValueError(f"capability {capability_id} is assigned to the wrong phase")

        for capability_id in sorted(declared_not_applicable):
            entry = self.capability_matrix.global_requirement(capability_id)
            if entry is None or entry.requirement is not AuditPhaseRequirement.NOT_APPLICABLE:
                raise ValueError(f"capability {capability_id} must be globally not_applicable")
            if entry.phase is not _KNOWN_CAPABILITY_PHASES[capability_id]:
                raise ValueError(f"capability {capability_id} is assigned to the wrong phase")

        for entry in self.capability_matrix.entries:
            expected_phase = (
                AuditPhase.DETERMINISTIC_PROBE
                if entry.capability_id in content_capabilities
                else _KNOWN_CAPABILITY_PHASES.get(entry.capability_id)
            )
            if expected_phase is not None and entry.phase is not expected_phase:
                raise ValueError(
                    f"capability {entry.capability_id} is assigned to the wrong phase"
                )
            if (
                entry.capability_id in expected_required | content_capabilities
                and (entry.scope_classes or entry.language_tiers)
            ):
                global_entry = self.capability_matrix.global_requirement(
                    entry.capability_id
                )
                if global_entry is None:
                    raise ValueError(
                        f"required capability {entry.capability_id} needs a global binding"
                    )
                if entry.requirement is not AuditPhaseRequirement.REQUIRED:
                    raise ValueError(
                        f"required capability {entry.capability_id} cannot be downgraded by scope"
                    )
                scoped_binding = (
                    entry.provider_id,
                    entry.node_id,
                    entry.backend_id,
                    entry.min_version_and_digest,
                    entry.proof_kind,
                    entry.proof_digest,
                    entry.missing_outcome,
                )
                global_binding = (
                    global_entry.provider_id,
                    global_entry.node_id,
                    global_entry.backend_id,
                    global_entry.min_version_and_digest,
                    global_entry.proof_kind,
                    global_entry.proof_digest,
                    global_entry.missing_outcome,
                )
                if scoped_binding != global_binding:
                    raise ValueError(
                        f"required capability {entry.capability_id} cannot override its binding"
                    )
            if (
                entry.capability_id in declared_not_applicable
                and entry.requirement is not AuditPhaseRequirement.NOT_APPLICABLE
            ):
                raise ValueError(
                    f"not-applicable capability {entry.capability_id} cannot be enabled by scope"
                )

        self._validate_execution_bound_capabilities()

    def _validate_execution_bound_capabilities(self) -> None:
        selection = self.execution_selection
        source_ingest = self.capability_matrix.global_requirement("source_ingest")
        assert source_ingest is not None
        if (
            source_ingest.node_id != selection.source_node_id
            or source_ingest.backend_id != selection.source_ingest_backend_id
            or source_ingest.min_version_and_digest is None
            or not hmac.compare_digest(
                source_ingest.min_version_and_digest.component_digest,
                selection.source_ingest_backend_digest,
            )
            or source_ingest.proof_digest is None
            or not hmac.compare_digest(
                source_ingest.proof_digest,
                selection.source_prepare_proof_digest,
            )
        ):
            raise ValueError("source_ingest capability must bind source node, backend, and proof")

        analysis_backend = self.capability_matrix.global_requirement("analysis_backend")
        assert analysis_backend is not None
        if (
            analysis_backend.node_id != selection.selected_node_id
            or analysis_backend.backend_id != selection.required_backend_id
            or analysis_backend.min_version_and_digest is None
            or not hmac.compare_digest(
                analysis_backend.min_version_and_digest.component_digest,
                selection.analysis_backend_digest,
            )
            or analysis_backend.proof_digest is None
            or not hmac.compare_digest(
                analysis_backend.proof_digest,
                selection.analysis_prepare_proof_digest,
            )
        ):
            raise ValueError(
                "analysis_backend capability must bind analysis node, backend, and proof"
            )

        content_components = tuple(
            (f"detector:{component.component_id}", component)
            for component in self.detectors
        ) + tuple(
            (f"parser:{component.component_id}", component) for component in self.parsers
        )
        for capability_id, component in content_components:
            if len(capability_id) > 256:
                raise ValueError("component identity is too long for capability binding")
            entry = self.capability_matrix.global_requirement(capability_id)
            if (
                entry is None
                or entry.requirement is not AuditPhaseRequirement.REQUIRED
                or entry.phase is not AuditPhase.DETERMINISTIC_PROBE
                or entry.node_id != selection.selected_node_id
                or entry.backend_id != selection.required_backend_id
                or entry.min_version_and_digest is None
                or entry.min_version_and_digest.minimum_version != component.version
                or not hmac.compare_digest(
                    entry.min_version_and_digest.component_digest,
                    component.digest,
                )
            ):
                raise ValueError(
                    f"capability {capability_id} must bind its component and analysis backend"
                )

        analysis_bound = {"dynamic_sandbox"}
        if self.analysis_profile is AnalysisProfile.HYBRID:
            analysis_bound.update(_HYBRID_REQUIRED_CAPABILITIES)
        if self.validation_policy is not ValidationPolicy.STATIC_ONLY:
            analysis_bound.add(_VALIDATION_POLICY_CAPABILITY[self.validation_policy])
        for capability_id in sorted(analysis_bound):
            entry = self.capability_matrix.global_requirement(capability_id)
            if entry is None:
                continue
            if (
                entry.node_id != selection.selected_node_id
                or entry.backend_id != selection.required_backend_id
            ):
                raise ValueError(
                    f"capability {capability_id} must bind the selected analysis backend"
                )

    def canonical_json(self) -> str:
        canonical = _canonical_json(self.model_dump(mode="json"))
        if len(canonical.encode("utf-8")) > MAX_AUDIT_CONTRACT_BYTES:
            raise ValueError("canonical Audit contract exceeds its byte limit")
        return canonical

    @property
    def contract_digest(self) -> str:
        return _domain_digest(AUDIT_CONTRACT_SCHEMA_VERSION, self.canonical_json().encode("utf-8"))

    @property
    def source_target_digest(self) -> str:
        payload = _canonical_json(self.source_target.model_dump(mode="json")).encode("utf-8")
        return _domain_digest("riftx.source-target/v1", payload)


class AuditContractRecord(AuditStrictModel):
    contract_id: AuditId = Field(default_factory=new_id)
    audit_id: AuditId
    schema_version: Literal["riftx.audit-contract/v1"] = AUDIT_CONTRACT_SCHEMA_VERSION
    canonical_contract_json: str = Field(
        min_length=2,
        max_length=MAX_AUDIT_CONTRACT_BYTES,
        repr=False,
    )
    contract_digest: Sha256Digest
    source_target_digest: Sha256Digest
    source_node_id: AuditNodeId
    source_ingest_backend_digest: Sha256Digest
    source_prepare_proof_digest: Sha256Digest
    selected_node_id: AuditNodeId
    required_backend_id: AuditToken
    snapshot_hydration_policy_digest: Sha256Digest
    created_at: AwareDatetime = Field(default_factory=utc_now)
    sealed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_record(self) -> AuditContractRecord:
        _parse_canonical_json(
            self.canonical_contract_json,
            maximum_bytes=MAX_AUDIT_CONTRACT_BYTES,
            label="canonical Audit contract",
        )
        contract = AuditContract.model_validate_json(self.canonical_contract_json)
        if contract.canonical_json() != self.canonical_contract_json:
            raise ValueError("Audit contract does not round-trip to its canonical encoding")
        checks = (
            (self.audit_id, contract.audit_id, "audit_id"),
            (self.contract_digest, contract.contract_digest, "contract_digest"),
            (self.source_target_digest, contract.source_target_digest, "source_target_digest"),
            (
                self.source_node_id,
                contract.execution_selection.source_node_id,
                "source_node_id",
            ),
            (
                self.source_ingest_backend_digest,
                contract.execution_selection.source_ingest_backend_digest,
                "source_ingest_backend_digest",
            ),
            (
                self.source_prepare_proof_digest,
                contract.execution_selection.source_prepare_proof_digest,
                "source_prepare_proof_digest",
            ),
            (
                self.selected_node_id,
                contract.execution_selection.selected_node_id,
                "selected_node_id",
            ),
            (
                self.required_backend_id,
                contract.execution_selection.required_backend_id,
                "required_backend_id",
            ),
            (
                self.snapshot_hydration_policy_digest,
                contract.execution_selection.snapshot_hydration_policy_digest,
                "snapshot_hydration_policy_digest",
            ),
        )
        for actual, expected, label in checks:
            equal = (
                hmac.compare_digest(actual, expected)
                if isinstance(actual, str) and isinstance(expected, str)
                else actual == expected
            )
            if not equal:
                raise ValueError(f"Audit contract record {label} does not match canonical contract")
        if self.sealed_at is not None and self.sealed_at < self.created_at:
            raise ValueError("Audit contract sealed_at must not precede created_at")
        return self

    @classmethod
    def from_contract(
        cls,
        contract: AuditContract,
        *,
        contract_id: str | None = None,
        created_at: datetime | None = None,
        sealed_at: datetime | None = None,
    ) -> AuditContractRecord:
        contract = AuditContract.model_validate(contract)
        selection = contract.execution_selection
        return cls(
            contract_id=contract_id if contract_id is not None else new_id(),
            audit_id=contract.audit_id,
            canonical_contract_json=contract.canonical_json(),
            contract_digest=contract.contract_digest,
            source_target_digest=contract.source_target_digest,
            source_node_id=selection.source_node_id,
            source_ingest_backend_digest=selection.source_ingest_backend_digest,
            source_prepare_proof_digest=selection.source_prepare_proof_digest,
            selected_node_id=selection.selected_node_id,
            required_backend_id=selection.required_backend_id,
            snapshot_hydration_policy_digest=selection.snapshot_hydration_policy_digest,
            created_at=created_at or utc_now(),
            sealed_at=sealed_at,
        )

    def contract(self) -> AuditContract:
        return AuditContract.model_validate_json(self.canonical_contract_json)

    def seal(self, *, at: datetime | None = None) -> AuditContractRecord:
        if self.sealed_at is not None:
            return self
        return self._validated_replace(sealed_at=at or utc_now())

    def _validated_replace(self, **updates: object) -> AuditContractRecord:
        payload = self.model_dump(mode="python")
        payload.update(updates)
        return type(self).model_validate(payload)


_AUDIT_TRANSITIONS: Mapping[AuditLifecycleStatus, frozenset[AuditLifecycleStatus]] = {
    AuditLifecycleStatus.DRAFT: frozenset(
        {AuditLifecycleStatus.QUEUED, AuditLifecycleStatus.CANCELLING, AuditLifecycleStatus.FAILING}
    ),
    AuditLifecycleStatus.QUEUED: frozenset(
        {
            AuditLifecycleStatus.PREFLIGHTING,
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        }
    ),
    AuditLifecycleStatus.PREFLIGHTING: frozenset(
        {
            AuditLifecycleStatus.SNAPSHOTTING,
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        }
    ),
    AuditLifecycleStatus.SNAPSHOTTING: frozenset(
        {
            AuditLifecycleStatus.RUNNING,
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        }
    ),
    AuditLifecycleStatus.RUNNING: frozenset(
        {
            AuditLifecycleStatus.WAITING_APPROVAL,
            AuditLifecycleStatus.PAUSING,
            AuditLifecycleStatus.FINALIZING,
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        }
    ),
    AuditLifecycleStatus.WAITING_APPROVAL: frozenset(
        {
            AuditLifecycleStatus.RUNNING,
            AuditLifecycleStatus.PAUSING,
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        }
    ),
    AuditLifecycleStatus.PAUSING: frozenset(
        {
            AuditLifecycleStatus.PAUSED,
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        }
    ),
    AuditLifecycleStatus.PAUSED: frozenset(
        {
            AuditLifecycleStatus.RUNNING,
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        }
    ),
    AuditLifecycleStatus.FINALIZING: frozenset(
        {
            AuditLifecycleStatus.CLEANING,
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        }
    ),
    AuditLifecycleStatus.CANCELLING: frozenset({AuditLifecycleStatus.CLEANING}),
    AuditLifecycleStatus.FAILING: frozenset(
        {AuditLifecycleStatus.CANCELLING, AuditLifecycleStatus.CLEANING}
    ),
    AuditLifecycleStatus.CLEANING: frozenset(
        {
            AuditLifecycleStatus.SEALING_CORE,
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        }
    ),
    AuditLifecycleStatus.SEALING_CORE: frozenset(
        {
            AuditLifecycleStatus.REPORTING,
            AuditLifecycleStatus.COMPLETED_PARTIAL,
            AuditLifecycleStatus.FAILED,
            AuditLifecycleStatus.CANCELLED,
        }
    ),
    AuditLifecycleStatus.REPORTING: frozenset(
        {
            AuditLifecycleStatus.PACKAGING,
            AuditLifecycleStatus.COMPLETED_PARTIAL,
            AuditLifecycleStatus.FAILED,
            AuditLifecycleStatus.CANCELLED,
        }
    ),
    AuditLifecycleStatus.PACKAGING: frozenset(
        {
            AuditLifecycleStatus.COMPLETED,
            AuditLifecycleStatus.COMPLETED_PARTIAL,
            AuditLifecycleStatus.FAILED,
            AuditLifecycleStatus.CANCELLED,
        }
    ),
    AuditLifecycleStatus.COMPLETED: frozenset(),
    AuditLifecycleStatus.COMPLETED_PARTIAL: frozenset(),
    AuditLifecycleStatus.FAILED: frozenset(),
    AuditLifecycleStatus.CANCELLED: frozenset(),
}

_PHASES = tuple(AuditPhase)
_PHASE_INDEX = {phase: index for index, phase in enumerate(_PHASES)}
_ANALYSIS_SNAPSHOT_STATUSES = frozenset(
    {
        AuditLifecycleStatus.RUNNING,
        AuditLifecycleStatus.WAITING_APPROVAL,
        AuditLifecycleStatus.PAUSING,
        AuditLifecycleStatus.PAUSED,
        AuditLifecycleStatus.FINALIZING,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        AuditLifecycleStatus.COMPLETED,
        AuditLifecycleStatus.COMPLETED_PARTIAL,
        AuditLifecycleStatus.FAILED,
        AuditLifecycleStatus.CANCELLED,
    }
)
_PUBLICATION_FAILURES = frozenset(
    {
        AuditPublicationStatus.SEAL_FAILED,
        AuditPublicationStatus.REPORT_FAILED,
        AuditPublicationStatus.PACKAGE_FAILED,
    }
)
_PUBLICATION_ACTIVE = frozenset(
    {
        AuditPublicationStatus.SEALING_CORE,
        AuditPublicationStatus.REPORT_PENDING,
        AuditPublicationStatus.REPORTING,
        AuditPublicationStatus.PACKAGING,
    }
)
_PUBLICATION_FINAL = _PUBLICATION_FAILURES | {AuditPublicationStatus.PUBLISHED}
_TERMINAL_PUBLICATION_STATES = _PUBLICATION_ACTIVE | _PUBLICATION_FINAL
_OUTCOME_UNSET_STATUSES = frozenset(
    {
        AuditLifecycleStatus.DRAFT,
        AuditLifecycleStatus.QUEUED,
        AuditLifecycleStatus.PREFLIGHTING,
        AuditLifecycleStatus.SNAPSHOTTING,
        AuditLifecycleStatus.RUNNING,
        AuditLifecycleStatus.WAITING_APPROVAL,
        AuditLifecycleStatus.PAUSING,
        AuditLifecycleStatus.PAUSED,
    }
)
_STARTED_REQUIRED_STATUSES = frozenset(
    {
        AuditLifecycleStatus.QUEUED,
        AuditLifecycleStatus.PREFLIGHTING,
        AuditLifecycleStatus.SNAPSHOTTING,
        AuditLifecycleStatus.RUNNING,
        AuditLifecycleStatus.WAITING_APPROVAL,
        AuditLifecycleStatus.PAUSING,
        AuditLifecycleStatus.PAUSED,
        AuditLifecycleStatus.FINALIZING,
    }
)
_PREPUBLICATION_STATUSES = frozenset(
    {
        AuditLifecycleStatus.DRAFT,
        AuditLifecycleStatus.QUEUED,
        AuditLifecycleStatus.PREFLIGHTING,
        AuditLifecycleStatus.SNAPSHOTTING,
        AuditLifecycleStatus.RUNNING,
        AuditLifecycleStatus.WAITING_APPROVAL,
        AuditLifecycleStatus.PAUSING,
        AuditLifecycleStatus.PAUSED,
        AuditLifecycleStatus.FINALIZING,
        AuditLifecycleStatus.CANCELLING,
        AuditLifecycleStatus.FAILING,
        AuditLifecycleStatus.CLEANING,
    }
)
_CLOSURE_OUTCOMES: Mapping[AuditTerminalOutcome, frozenset[AuditClosureStatus]] = {
    AuditTerminalOutcome.COMPLETE: frozenset(
        {
            AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE,
            AuditClosureStatus.COMPLETE_WITH_POLICY_EXCLUSIONS,
        }
    ),
    AuditTerminalOutcome.PARTIAL: frozenset(
        {AuditClosureStatus.PARTIAL_CAPABILITY, AuditClosureStatus.PARTIAL_BUDGET}
    ),
    AuditTerminalOutcome.FAILED: frozenset({AuditClosureStatus.FAILED}),
    AuditTerminalOutcome.CANCELLED: frozenset({AuditClosureStatus.CANCELLED}),
}
_RUN_TERMINAL_OUTCOMES: Mapping[AuditTerminalOutcome, RunStatus] = {
    AuditTerminalOutcome.COMPLETE: RunStatus.COMPLETED,
    AuditTerminalOutcome.PARTIAL: RunStatus.COMPLETED,
    AuditTerminalOutcome.FAILED: RunStatus.FAILED,
    AuditTerminalOutcome.CANCELLED: RunStatus.CANCELLED,
}


def _project_terminal_lifecycle(
    outcome: AuditTerminalOutcome,
    publication: AuditPublicationStatus,
) -> AuditLifecycleStatus:
    if outcome is AuditTerminalOutcome.COMPLETE:
        return (
            AuditLifecycleStatus.COMPLETED
            if publication is AuditPublicationStatus.PUBLISHED
            else AuditLifecycleStatus.COMPLETED_PARTIAL
        )
    if outcome is AuditTerminalOutcome.PARTIAL:
        return AuditLifecycleStatus.COMPLETED_PARTIAL
    if outcome is AuditTerminalOutcome.FAILED:
        return AuditLifecycleStatus.FAILED
    return AuditLifecycleStatus.CANCELLED


class AuditScan(AuditStrictModel):
    id: AuditId = Field(default_factory=new_id)
    run_id: AuditId
    project_id: AuditId
    contract_id: AuditId
    snapshot_id: AuditId | None = None
    base_snapshot_id: AuditId | None = None
    baseline_audit_id: AuditId | None = None
    purpose: AuditPurpose = AuditPurpose.PRIMARY
    parent_audit_id: AuditId | None = None
    mode: AuditMode
    analysis_profile: AnalysisProfile
    lifecycle_status: AuditLifecycleStatus = AuditLifecycleStatus.DRAFT
    current_phase: AuditPhase = AuditPhase.AUTHORIZE_AND_FREEZE
    terminal_outcome: AuditTerminalOutcome | None = None
    cleanup_proof_digest: Sha256Digest | None = None
    run_terminal_status: RunStatus | None = None
    closure_status: AuditClosureStatus | None = None
    publication_status: AuditPublicationStatus = AuditPublicationStatus.NOT_STARTED
    core_seal_root: Sha256Digest | None = None
    initial_distribution_revision_id: AuditId | None = None
    latest_distribution_revision_id: AuditId | None = None
    model_profile: AuditToken | None = Field(default=None, max_length=255)
    selected_node_id: AuditNodeId
    required_backend_id: AuditToken
    policy_digest: Sha256Digest
    budget_digest: Sha256Digest
    config_digest: Sha256Digest
    contract_digest: Sha256Digest
    # Temporal's deterministic ``riftx-code-audit-{audit_id}`` identifier can
    # exceed the 128-character Audit ID bound, so it uses the wider token type.
    temporal_workflow_id: AuditToken
    created_at: AwareDatetime = Field(default_factory=utc_now)
    started_at: AwareDatetime | None = None
    analysis_finished_at: AwareDatetime | None = None
    publication_finished_at: AwareDatetime | None = None
    sealed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_scan(self) -> AuditScan:
        if self.temporal_workflow_id != f"riftx-code-audit-{self.id}":
            raise ValueError("Audit temporal_workflow_id must be deterministic")
        if self.purpose is AuditPurpose.PRIMARY:
            if self.parent_audit_id is not None:
                raise ValueError("primary Audit cannot have a parent_audit_id")
        elif self.parent_audit_id is None or self.parent_audit_id == self.id:
            raise ValueError("follow-up and retest Audits require a distinct parent_audit_id")

        if self.mode is AuditMode.DEEP and self.analysis_profile is not AnalysisProfile.HYBRID:
            raise ValueError("Deep Audit mode requires the hybrid analysis profile")
        if self.baseline_audit_id == self.id:
            raise ValueError("baseline_audit_id must refer to a different Audit")
        if (
            self.analysis_profile is AnalysisProfile.DETERMINISTIC
            and self.model_profile is not None
        ):
            raise ValueError("deterministic Audit cannot carry an active model_profile")
        if self.analysis_profile is AnalysisProfile.HYBRID and self.model_profile is None:
            raise ValueError("hybrid Audit requires a model_profile")

        in_analysis_phase = (
            _PHASE_INDEX[AuditPhase.MAP_SCOPE]
            <= _PHASE_INDEX[self.current_phase]
            <= _PHASE_INDEX[AuditPhase.VALIDATE_CLOSURE]
        )
        requires_analysis_snapshot = (
            self.lifecycle_status in _ANALYSIS_SNAPSHOT_STATUSES
            or (
                self.lifecycle_status
                in {AuditLifecycleStatus.CANCELLING, AuditLifecycleStatus.FAILING}
                and in_analysis_phase
            )
        )
        if requires_analysis_snapshot and self.snapshot_id is None:
            raise ValueError("analysis lifecycle requires a sealed snapshot")
        if (
            self.mode is AuditMode.DIFF
            and requires_analysis_snapshot
            and self.base_snapshot_id is None
        ):
            raise ValueError("Diff analysis lifecycle requires a sealed base snapshot")
        if self.mode is AuditMode.DIFF:
            if (self.snapshot_id is None) != (self.base_snapshot_id is None):
                raise ValueError("Diff snapshots must be bound atomically")
            if self.snapshot_id is not None and self.snapshot_id == self.base_snapshot_id:
                raise ValueError("Diff head and base snapshots must differ")
        if self.mode is not AuditMode.DIFF and self.base_snapshot_id is not None:
            raise ValueError("base_snapshot_id is only valid for Diff Audit mode")
        if (
            self.terminal_outcome in {AuditTerminalOutcome.COMPLETE, AuditTerminalOutcome.PARTIAL}
            and self.snapshot_id is None
        ):
            raise ValueError("complete or partial outcome requires a sealed snapshot")

        self._validate_phase_lifecycle()
        self._validate_outcome_and_publication()
        self._validate_timestamps()
        return self

    def _validate_phase_lifecycle(self) -> None:
        pre_analysis = {
            AuditLifecycleStatus.DRAFT,
            AuditLifecycleStatus.QUEUED,
            AuditLifecycleStatus.PREFLIGHTING,
            AuditLifecycleStatus.SNAPSHOTTING,
        }
        active_analysis = {
            AuditLifecycleStatus.RUNNING,
            AuditLifecycleStatus.WAITING_APPROVAL,
            AuditLifecycleStatus.PAUSING,
            AuditLifecycleStatus.PAUSED,
        }
        if (
            self.lifecycle_status in pre_analysis
            and self.current_phase is not AuditPhase.AUTHORIZE_AND_FREEZE
        ):
            raise ValueError("pre-analysis lifecycle must remain in authorize_and_freeze")
        if self.lifecycle_status in active_analysis and not (
            _PHASE_INDEX[AuditPhase.MAP_SCOPE]
            <= _PHASE_INDEX[self.current_phase]
            <= _PHASE_INDEX[AuditPhase.VALIDATE_CLOSURE]
        ):
            raise ValueError("active analysis lifecycle requires an analysis phase")
        if (
            self.lifecycle_status is AuditLifecycleStatus.FINALIZING
            and self.current_phase is not AuditPhase.VALIDATE_CLOSURE
        ):
            raise ValueError("finalizing requires validate_closure phase")
        expected_phase = {
            AuditLifecycleStatus.CLEANING: AuditPhase.CLEANUP,
            AuditLifecycleStatus.SEALING_CORE: AuditPhase.SEAL_CORE,
            AuditLifecycleStatus.REPORTING: AuditPhase.GENERATE_REPORTS,
            AuditLifecycleStatus.PACKAGING: AuditPhase.PACKAGE_AND_PUBLISH,
        }.get(self.lifecycle_status)
        if expected_phase is not None and self.current_phase is not expected_phase:
            raise ValueError("lifecycle_status does not match current_phase")
        if self.lifecycle_status in {
            AuditLifecycleStatus.CANCELLING,
            AuditLifecycleStatus.FAILING,
        } and _PHASE_INDEX[self.current_phase] > _PHASE_INDEX[AuditPhase.CLEANUP]:
            raise ValueError("effect-stop lifecycle cannot advance beyond cleanup")
        if self.lifecycle_status in _TERMINAL_STATUSES:
            terminal_phase = {
                AuditPublicationStatus.SEALING_CORE: AuditPhase.SEAL_CORE,
                AuditPublicationStatus.REPORT_PENDING: AuditPhase.GENERATE_REPORTS,
                AuditPublicationStatus.REPORTING: AuditPhase.GENERATE_REPORTS,
                AuditPublicationStatus.PACKAGING: AuditPhase.PACKAGE_AND_PUBLISH,
                AuditPublicationStatus.SEAL_FAILED: AuditPhase.SEAL_CORE,
                AuditPublicationStatus.REPORT_FAILED: AuditPhase.GENERATE_REPORTS,
            }.get(self.publication_status, AuditPhase.PACKAGE_AND_PUBLISH)
            if self.current_phase is not terminal_phase:
                raise ValueError("terminal publication status does not match current_phase")

    def _validate_outcome_and_publication(self) -> None:
        if self.lifecycle_status in _OUTCOME_UNSET_STATUSES and self.terminal_outcome is not None:
            raise ValueError("analysis lifecycle cannot have a terminal_outcome yet")
        if (
            self.lifecycle_status is AuditLifecycleStatus.FINALIZING
            and self.terminal_outcome
            not in {AuditTerminalOutcome.COMPLETE, AuditTerminalOutcome.PARTIAL}
        ):
            raise ValueError("finalizing requires complete or partial terminal_outcome")
        if (
            self.lifecycle_status is AuditLifecycleStatus.CANCELLING
            and self.terminal_outcome is not AuditTerminalOutcome.CANCELLED
        ):
            raise ValueError("cancelling requires cancelled terminal_outcome")
        if (
            self.lifecycle_status is AuditLifecycleStatus.FAILING
            and self.terminal_outcome is not AuditTerminalOutcome.FAILED
        ):
            raise ValueError("failing requires failed terminal_outcome")
        if (
            self.lifecycle_status
            not in _OUTCOME_UNSET_STATUSES
            | {
                AuditLifecycleStatus.FINALIZING,
                AuditLifecycleStatus.CANCELLING,
                AuditLifecycleStatus.FAILING,
            }
            and self.terminal_outcome is None
        ):
            raise ValueError("cleanup and publication lifecycle require a terminal_outcome")

        if self.closure_status is not None:
            if self.terminal_outcome is None:
                raise ValueError("closure_status requires a terminal_outcome")
            if self.cleanup_proof_digest is None or self.run_terminal_status is None:
                raise ValueError("closure_status requires cleanup and Run terminal proof")
            if self.closure_status not in _CLOSURE_OUTCOMES[self.terminal_outcome]:
                raise ValueError("closure_status does not match terminal_outcome")
            closure_lifecycles = {
                AuditLifecycleStatus.CLEANING,
                AuditLifecycleStatus.SEALING_CORE,
                AuditLifecycleStatus.REPORTING,
                AuditLifecycleStatus.PACKAGING,
            } | _TERMINAL_STATUSES
            if self.lifecycle_status not in closure_lifecycles:
                raise ValueError("closure_status cannot precede cleanup convergence")
        has_cleanup_proof = self.cleanup_proof_digest is not None
        has_run_terminal = self.run_terminal_status is not None
        if has_cleanup_proof != has_run_terminal:
            raise ValueError("cleanup proof and Run terminal status must be recorded together")
        if has_cleanup_proof:
            if self.terminal_outcome is None:
                raise ValueError("cleanup proof requires a terminal_outcome")
            cleanup_lifecycles = {
                AuditLifecycleStatus.CLEANING,
                AuditLifecycleStatus.SEALING_CORE,
                AuditLifecycleStatus.REPORTING,
                AuditLifecycleStatus.PACKAGING,
            } | _TERMINAL_STATUSES
            if self.lifecycle_status not in cleanup_lifecycles:
                raise ValueError("cleanup proof cannot precede cleaning")
            if self.run_terminal_status is not _RUN_TERMINAL_OUTCOMES[
                self.terminal_outcome
            ]:
                raise ValueError("Run terminal status does not match terminal_outcome")
        if self.lifecycle_status in {
            AuditLifecycleStatus.SEALING_CORE,
            AuditLifecycleStatus.REPORTING,
            AuditLifecycleStatus.PACKAGING,
        } | _TERMINAL_STATUSES and self.closure_status is None:
            raise ValueError("sealing and terminal lifecycle require closure_status")

        if self.lifecycle_status in _TERMINAL_STATUSES:
            assert self.terminal_outcome is not None
            if self.publication_status not in _TERMINAL_PUBLICATION_STATES:
                raise ValueError("terminal Audit requires publication progress or failure")
            expected_lifecycle = _project_terminal_lifecycle(
                self.terminal_outcome,
                self.publication_status,
            )
            if self.lifecycle_status is not expected_lifecycle:
                raise ValueError("terminal lifecycle does not match terminal_outcome")
        if (self.core_seal_root is None) != (self.sealed_at is None):
            raise ValueError("core_seal_root and sealed_at must be recorded together")
        if self.publication_status in {
            AuditPublicationStatus.NOT_STARTED,
            AuditPublicationStatus.SEALING_CORE,
            AuditPublicationStatus.SEAL_FAILED,
        } and self.core_seal_root is not None:
            raise ValueError("core seal facts are premature for publication_status")
        if self.publication_status in {
            AuditPublicationStatus.REPORT_PENDING,
            AuditPublicationStatus.REPORTING,
            AuditPublicationStatus.PACKAGING,
            AuditPublicationStatus.PUBLISHED,
            AuditPublicationStatus.REPORT_FAILED,
            AuditPublicationStatus.PACKAGE_FAILED,
        } and self.core_seal_root is None:
            raise ValueError("post-seal publication state requires a core seal")
        if (self.latest_distribution_revision_id is None) != (
            self.initial_distribution_revision_id is None
        ):
            raise ValueError("initial and latest distribution revisions must be recorded together")
        if (self.latest_distribution_revision_id is None) != (
            self.publication_finished_at is None
        ):
            raise ValueError("successful distribution revisions require publication_finished_at")
        if self.publication_status is AuditPublicationStatus.PUBLISHED:
            if (
                self.initial_distribution_revision_id is None
                or self.latest_distribution_revision_id is None
                or self.publication_finished_at is None
            ):
                raise ValueError("published Audit requires distribution revisions and finish time")
        elif any(
            (
                self.initial_distribution_revision_id,
                self.latest_distribution_revision_id,
                self.publication_finished_at,
            )
        ):
            raise ValueError("distribution facts require published publication_status")

        allowed_publication_states = {
            AuditLifecycleStatus.SEALING_CORE: {
                AuditPublicationStatus.SEALING_CORE,
                AuditPublicationStatus.REPORT_PENDING,
                AuditPublicationStatus.SEAL_FAILED,
            },
            AuditLifecycleStatus.REPORTING: {
                AuditPublicationStatus.REPORTING,
                AuditPublicationStatus.REPORT_FAILED,
            },
            AuditLifecycleStatus.PACKAGING: {
                AuditPublicationStatus.PACKAGING,
                AuditPublicationStatus.PACKAGE_FAILED,
                AuditPublicationStatus.PUBLISHED,
            },
        }
        if (
            self.lifecycle_status in _PREPUBLICATION_STATUSES
            and self.publication_status is not AuditPublicationStatus.NOT_STARTED
        ):
            raise ValueError("publication cannot start before sealing_core")
        if (
            self.lifecycle_status in allowed_publication_states
            and self.publication_status not in allowed_publication_states[self.lifecycle_status]
        ):
            raise ValueError("publication_status does not match lifecycle_status")

    def _validate_timestamps(self) -> None:
        if self.lifecycle_status is AuditLifecycleStatus.DRAFT and self.started_at is not None:
            raise ValueError("draft Audit cannot have started_at")
        if self.lifecycle_status in _STARTED_REQUIRED_STATUSES and self.started_at is None:
            raise ValueError("started Audit lifecycle requires started_at")
        if (
            self.lifecycle_status
            in {AuditLifecycleStatus.CANCELLING, AuditLifecycleStatus.FAILING}
            and _PHASE_INDEX[AuditPhase.MAP_SCOPE]
            <= _PHASE_INDEX[self.current_phase]
            <= _PHASE_INDEX[AuditPhase.VALIDATE_CLOSURE]
            and self.started_at is None
        ):
            raise ValueError("analysis-phase effect stop requires started_at")
        if (
            self.terminal_outcome in {AuditTerminalOutcome.COMPLETE, AuditTerminalOutcome.PARTIAL}
            and self.started_at is None
        ):
            raise ValueError("complete or partial outcome requires started_at")
        publication_lifecycle = {
            AuditLifecycleStatus.SEALING_CORE,
            AuditLifecycleStatus.REPORTING,
            AuditLifecycleStatus.PACKAGING,
        } | _TERMINAL_STATUSES
        if self.lifecycle_status in publication_lifecycle and self.analysis_finished_at is None:
            raise ValueError("publication lifecycle requires analysis_finished_at")
        if (
            self.lifecycle_status not in publication_lifecycle
            and self.analysis_finished_at is not None
        ):
            raise ValueError("analysis_finished_at is premature for lifecycle_status")
        ordered = (
            ("created_at", self.created_at),
            ("started_at", self.started_at),
            ("analysis_finished_at", self.analysis_finished_at),
            ("sealed_at", self.sealed_at),
            ("publication_finished_at", self.publication_finished_at),
        )
        previous_name = "created_at"
        previous = self.created_at.astimezone(UTC)
        for name, value in ordered[1:]:
            if value is None:
                continue
            current = value.astimezone(UTC)
            if current < previous:
                raise ValueError(f"{name} must not precede {previous_name}")
            previous_name = name
            previous = current

    def _validated_replace(self, **updates: object) -> AuditScan:
        payload = self.model_dump(mode="python")
        payload.update(updates)
        return type(self).model_validate(payload)

    def can_transition_to(self, target: AuditLifecycleStatus) -> bool:
        return isinstance(target, AuditLifecycleStatus) and target in _AUDIT_TRANSITIONS[
            self.lifecycle_status
        ]

    def transition_to(
        self,
        target: AuditLifecycleStatus,
        *,
        at: datetime | None = None,
        terminal_outcome: AuditTerminalOutcome | None = None,
    ) -> AuditScan:
        if not self.can_transition_to(target):
            raise InvalidStateTransitionError("AuditScan", self.lifecycle_status, target)
        if terminal_outcome is not None and target is not AuditLifecycleStatus.FINALIZING:
            raise ValueError("terminal_outcome can only be selected when finalizing")
        changed_at = at or utc_now()
        updates: dict[str, object] = {"lifecycle_status": target}
        if target is AuditLifecycleStatus.QUEUED and self.started_at is None:
            updates["started_at"] = changed_at
        if target is AuditLifecycleStatus.RUNNING and self.lifecycle_status is (
            AuditLifecycleStatus.SNAPSHOTTING
        ):
            updates["current_phase"] = AuditPhase.MAP_SCOPE
        if target is AuditLifecycleStatus.FINALIZING:
            if self.current_phase is not AuditPhase.VALIDATE_CLOSURE:
                raise ValueError("finalizing requires validate_closure phase")
            if terminal_outcome not in {
                AuditTerminalOutcome.COMPLETE,
                AuditTerminalOutcome.PARTIAL,
            }:
                raise ValueError("finalizing requires complete or partial terminal_outcome")
            updates["terminal_outcome"] = terminal_outcome
        elif target is AuditLifecycleStatus.CANCELLING:
            updates["terminal_outcome"] = AuditTerminalOutcome.CANCELLED
        elif target is AuditLifecycleStatus.FAILING:
            if self.terminal_outcome is AuditTerminalOutcome.CANCELLED:
                raise ValueError("cancelled terminal outcome cannot be downgraded to failed")
            updates["terminal_outcome"] = AuditTerminalOutcome.FAILED
        if (
            target in {AuditLifecycleStatus.CANCELLING, AuditLifecycleStatus.FAILING}
            and self.lifecycle_status is AuditLifecycleStatus.CLEANING
            and (self.cleanup_proof_digest is not None or self.closure_status is not None)
        ):
            raise ValueError("converged cleanup outcome cannot be changed")
        if target is AuditLifecycleStatus.CLEANING:
            updates["current_phase"] = AuditPhase.CLEANUP
        if target is AuditLifecycleStatus.SEALING_CORE:
            if self.terminal_outcome is None:
                raise ValueError("sealing_core requires a durable terminal_outcome")
            if self.cleanup_proof_digest is None or self.run_terminal_status is None:
                raise ValueError("sealing_core requires cleanup and Run terminal proof")
            if self.closure_status is None:
                raise ValueError("sealing_core requires cleanup and a recorded closure")
            updates["analysis_finished_at"] = self.analysis_finished_at or changed_at
            updates["publication_status"] = AuditPublicationStatus.SEALING_CORE
            updates["current_phase"] = AuditPhase.SEAL_CORE
        elif target is AuditLifecycleStatus.REPORTING:
            if self.publication_status is not AuditPublicationStatus.REPORT_PENDING:
                raise ValueError("reporting requires a successfully sealed core")
            updates["publication_status"] = AuditPublicationStatus.REPORTING
            updates["current_phase"] = AuditPhase.GENERATE_REPORTS
        elif target is AuditLifecycleStatus.PACKAGING:
            if self.publication_status is not AuditPublicationStatus.REPORTING:
                raise ValueError("packaging requires report generation to be active")
            updates["publication_status"] = AuditPublicationStatus.PACKAGING
            updates["current_phase"] = AuditPhase.PACKAGE_AND_PUBLISH
        if target in _TERMINAL_STATUSES:
            if self.terminal_outcome is None:
                raise ValueError("terminal transition requires a durable terminal_outcome")
            if self.publication_status not in _PUBLICATION_FINAL:
                raise ValueError("terminal transition requires published or failed publication")
            expected_target = _project_terminal_lifecycle(
                self.terminal_outcome,
                self.publication_status,
            )
            if target is not expected_target:
                raise ValueError("terminal target does not match outcome and publication")
        return self._validated_replace(**updates)

    def bind_snapshots(
        self,
        *,
        snapshot_id: str,
        base_snapshot_id: str | None = None,
    ) -> AuditScan:
        if self.lifecycle_status not in {
            AuditLifecycleStatus.DRAFT,
            AuditLifecycleStatus.QUEUED,
            AuditLifecycleStatus.PREFLIGHTING,
            AuditLifecycleStatus.SNAPSHOTTING,
        }:
            raise ValueError("snapshots can only be bound before analysis starts")
        if self.snapshot_id is not None or self.base_snapshot_id is not None:
            if self.snapshot_id == snapshot_id and self.base_snapshot_id == base_snapshot_id:
                return self
            raise ValueError("Audit snapshots are immutable once bound")
        if self.mode is AuditMode.DIFF and base_snapshot_id is None:
            raise ValueError("Diff Audit requires both head and base snapshots")
        if snapshot_id == base_snapshot_id:
            raise ValueError("Diff head and base snapshots must differ")
        if self.mode is not AuditMode.DIFF and base_snapshot_id is not None:
            raise ValueError("base snapshot is only valid for Diff Audit mode")
        return self._validated_replace(
            snapshot_id=snapshot_id,
            base_snapshot_id=base_snapshot_id,
        )

    def transition_phase_to(self, target: AuditPhase) -> AuditScan:
        if not isinstance(target, AuditPhase):
            raise InvalidStateTransitionError("AuditPhase", self.current_phase, target)
        if self.lifecycle_status is not AuditLifecycleStatus.RUNNING:
            raise InvalidStateTransitionError("AuditPhase", self.current_phase, target)
        current_index = _PHASE_INDEX[self.current_phase]
        target_index = _PHASE_INDEX[target]
        if (
            target_index != current_index + 1
            or target_index > _PHASE_INDEX[AuditPhase.VALIDATE_CLOSURE]
        ):
            raise InvalidStateTransitionError("AuditPhase", self.current_phase, target)
        return self._validated_replace(current_phase=target)

    def record_closure(self, closure: AuditClosureStatus) -> AuditScan:
        if self.closure_status is not None:
            if self.closure_status is closure:
                return self
            raise ValueError("Audit closure is immutable once recorded")
        if self.terminal_outcome is None:
            raise ValueError("Audit closure requires a durable terminal_outcome")
        if (
            self.lifecycle_status is not AuditLifecycleStatus.CLEANING
            or self.current_phase is not AuditPhase.CLEANUP
        ):
            raise ValueError("Audit closure requires cleanup convergence")
        if self.cleanup_proof_digest is None or self.run_terminal_status is None:
            raise ValueError("Audit closure requires cleanup and Run terminal proof")
        expected = {
            AuditTerminalOutcome.COMPLETE: {
                AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE,
                AuditClosureStatus.COMPLETE_WITH_POLICY_EXCLUSIONS,
            },
            AuditTerminalOutcome.PARTIAL: {
                AuditClosureStatus.PARTIAL_CAPABILITY,
                AuditClosureStatus.PARTIAL_BUDGET,
            },
            AuditTerminalOutcome.FAILED: {AuditClosureStatus.FAILED},
            AuditTerminalOutcome.CANCELLED: {AuditClosureStatus.CANCELLED},
        }
        if closure not in expected[self.terminal_outcome]:
            raise ValueError("closure does not match the durable terminal_outcome")
        return self._validated_replace(closure_status=closure)

    def record_cleanup_convergence(
        self,
        *,
        cleanup_proof_digest: str,
        run_terminal_status: RunStatus,
    ) -> AuditScan:
        if self.cleanup_proof_digest is not None or self.run_terminal_status is not None:
            if (
                self.cleanup_proof_digest == cleanup_proof_digest
                and self.run_terminal_status is run_terminal_status
            ):
                return self
            raise ValueError("cleanup convergence proof is immutable once recorded")
        if (
            self.lifecycle_status is not AuditLifecycleStatus.CLEANING
            or self.current_phase is not AuditPhase.CLEANUP
        ):
            raise ValueError("cleanup convergence can only be recorded during cleanup")
        if self.terminal_outcome is None:
            raise ValueError("cleanup convergence requires a terminal_outcome")
        if run_terminal_status is not _RUN_TERMINAL_OUTCOMES[self.terminal_outcome]:
            raise ValueError("Run terminal status does not match terminal_outcome")
        return self._validated_replace(
            cleanup_proof_digest=cleanup_proof_digest,
            run_terminal_status=run_terminal_status,
        )

    def record_core_seal(
        self,
        *,
        core_seal_root: str,
        at: datetime | None = None,
    ) -> AuditScan:
        if (
            self.publication_status is AuditPublicationStatus.REPORT_PENDING
            and self.core_seal_root == core_seal_root
        ):
            return self
        if self.publication_status is not AuditPublicationStatus.SEALING_CORE:
            raise ValueError("core seal requires active sealing_core publication")
        if self.lifecycle_status is not AuditLifecycleStatus.SEALING_CORE and not (
            self.lifecycle_status in _TERMINAL_STATUSES
            and self.publication_status is AuditPublicationStatus.SEALING_CORE
        ):
            raise ValueError("core seal can only be recorded during sealing_core publication")
        if self.closure_status is None:
            raise ValueError("core seal requires a recorded closure")
        if self.core_seal_root is not None:
            raise ValueError("core seal is immutable once recorded")
        updates: dict[str, object] = {
            "core_seal_root": core_seal_root,
            "sealed_at": at or utc_now(),
            "publication_status": AuditPublicationStatus.REPORT_PENDING,
        }
        if self.lifecycle_status in _TERMINAL_STATUSES:
            updates["current_phase"] = AuditPhase.GENERATE_REPORTS
        return self._validated_replace(**updates)

    def begin_publication_retry(self) -> AuditScan:
        if self.lifecycle_status not in _TERMINAL_STATUSES:
            raise ValueError("publication retry requires a terminal Audit lifecycle")
        retry = {
            AuditPublicationStatus.SEAL_FAILED: (
                AuditPublicationStatus.SEALING_CORE,
                AuditPhase.SEAL_CORE,
            ),
            AuditPublicationStatus.REPORT_FAILED: (
                AuditPublicationStatus.REPORT_PENDING,
                AuditPhase.GENERATE_REPORTS,
            ),
            AuditPublicationStatus.PACKAGE_FAILED: (
                AuditPublicationStatus.PACKAGING,
                AuditPhase.PACKAGE_AND_PUBLISH,
            ),
        }.get(self.publication_status)
        if retry is None:
            raise ValueError("publication retry requires a failed publication state")
        status, phase = retry
        return self._validated_replace(publication_status=status, current_phase=phase)

    def transition_terminal_publication_to(
        self,
        target: AuditPublicationStatus,
    ) -> AuditScan:
        if self.lifecycle_status not in _TERMINAL_STATUSES:
            raise ValueError("terminal publication transition requires a terminal Audit")
        allowed = {
            AuditPublicationStatus.REPORT_PENDING: AuditPublicationStatus.REPORTING,
            AuditPublicationStatus.REPORTING: AuditPublicationStatus.PACKAGING,
        }
        if allowed.get(self.publication_status) is not target:
            raise InvalidStateTransitionError(
                "AuditPublication", self.publication_status, target
            )
        phase = (
            AuditPhase.GENERATE_REPORTS
            if target is AuditPublicationStatus.REPORTING
            else AuditPhase.PACKAGE_AND_PUBLISH
        )
        return self._validated_replace(publication_status=target, current_phase=phase)

    def record_publication_failure(self, status: AuditPublicationStatus) -> AuditScan:
        allowed = {
            AuditPublicationStatus.SEALING_CORE: AuditPublicationStatus.SEAL_FAILED,
            AuditPublicationStatus.REPORTING: AuditPublicationStatus.REPORT_FAILED,
            AuditPublicationStatus.PACKAGING: AuditPublicationStatus.PACKAGE_FAILED,
        }
        if allowed.get(self.publication_status) is not status:
            raise InvalidStateTransitionError(
                "AuditPublication", self.publication_status, status
            )
        return self._validated_replace(publication_status=status)

    def record_distribution_revision(
        self,
        *,
        revision_id: str,
        at: datetime | None = None,
    ) -> AuditScan:
        if (
            self.publication_status is AuditPublicationStatus.PUBLISHED
            and self.latest_distribution_revision_id == revision_id
        ):
            return self
        if self.publication_status is not AuditPublicationStatus.PACKAGING:
            raise ValueError("distribution revision requires packaging publication state")
        existing_revision = self.latest_distribution_revision_id == revision_id
        finished_at = (
            self.publication_finished_at
            if existing_revision and self.publication_finished_at is not None
            else at or utc_now()
        )
        if finished_at.tzinfo is None or finished_at.utcoffset() is None:
            raise ValueError("publication revision time must be timezone-aware")
        if (
            self.publication_finished_at is not None
            and finished_at.astimezone(UTC)
            < self.publication_finished_at.astimezone(UTC)
        ):
            raise ValueError("publication revision time must move monotonically forward")
        updates: dict[str, object] = {
            "initial_distribution_revision_id": (
                self.initial_distribution_revision_id or revision_id
            ),
            "latest_distribution_revision_id": revision_id,
            "publication_status": AuditPublicationStatus.PUBLISHED,
            "publication_finished_at": finished_at,
        }
        if (
            self.lifecycle_status in _TERMINAL_STATUSES
            and self.terminal_outcome is AuditTerminalOutcome.COMPLETE
        ):
            updates["lifecycle_status"] = AuditLifecycleStatus.COMPLETED
        return self._validated_replace(
            **updates,
        )

    def validate_contract_record(self, record: AuditContractRecord) -> AuditContract:
        AuditScan.model_validate(self)
        record = AuditContractRecord.model_validate(record)
        if record.contract_id != self.contract_id:
            raise ValueError("AuditScan contract_id does not match AuditContractRecord")
        if not hmac.compare_digest(record.contract_digest, self.contract_digest):
            raise ValueError("AuditScan contract_digest does not match AuditContractRecord")
        if self.lifecycle_status is not AuditLifecycleStatus.DRAFT and record.sealed_at is None:
            raise ValueError("started Audit requires a sealed AuditContractRecord")
        if (
            self.started_at is not None
            and record.sealed_at is not None
            and record.sealed_at > self.started_at
        ):
            raise ValueError("AuditContractRecord must be sealed before Audit start")
        contract = record.contract()
        checks = (
            (contract.audit_id, self.id, "audit_id"),
            (contract.project_id, self.project_id, "project_id"),
            (contract.mode, self.mode, "mode"),
            (contract.analysis_profile, self.analysis_profile, "analysis_profile"),
            (contract.baseline_audit_id, self.baseline_audit_id, "baseline_audit_id"),
            (contract.model_profile, self.model_profile, "model_profile"),
            (
                contract.execution_selection.selected_node_id,
                self.selected_node_id,
                "selected_node_id",
            ),
            (
                contract.execution_selection.required_backend_id,
                self.required_backend_id,
                "required_backend_id",
            ),
            (contract.policy_digest, self.policy_digest, "policy_digest"),
            (contract.config_digest, self.config_digest, "config_digest"),
            (contract.budget.digest, self.budget_digest, "budget_digest"),
        )
        for actual, expected, label in checks:
            if actual != expected:
                raise ValueError(f"AuditScan {label} does not match its canonical contract")
        return contract
