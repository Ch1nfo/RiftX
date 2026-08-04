"""Authoritative, preflight-bound draft contracts for Code Audit v2.

AUD-201 can freeze only facts proved by the AUD-200 SourceIngest preflight.  It
cannot honestly select an analysis backend, snapshot implementation, detector
inventory, or Start delivery path yet.  The models in this module make that
staging boundary explicit: a v2 contract created here is immutable and useful
for review/replay, but is never Start-eligible.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .audit import (
    MAX_AUDIT_CONTRACT_BYTES,
    AnalysisProfile,
    AuditMode,
    AuditStrictModel,
    SourceTarget,
    SourceTargetKind,
    ValidationPolicy,
)
from .audit_preflight import (
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST,
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID,
    AUDIT_PREFLIGHT_CAPABILITY_MATRIX_SCHEMA_VERSION,
    AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION,
    AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION,
    AuditPreflightBudgetStatus,
    AuditPreflightCapabilityFact,
    AuditPreflightCapabilityMatrix,
    AuditPreflightCapabilityStatus,
    AuditPreflightMinimumFeasibleBudget,
)
from .audit_preflight_plan import AuditPreflightPlan, AuditPreflightPlanStatus
from .base import new_id, utc_now

AUDIT_CONTRACT_V2_SCHEMA_VERSION: Literal["riftx.audit-contract/v2"] = (
    "riftx.audit-contract/v2"
)
AUDIT_SOURCE_TARGET_V2_SCHEMA_VERSION: Literal["riftx.audit-source-target/v2"] = (
    "riftx.audit-source-target/v2"
)
AUDIT_SOURCE_SCOPE_V2_SCHEMA_VERSION: Literal["riftx.audit-source-scope/v2"] = (
    "riftx.audit-source-scope/v2"
)
AUDIT_SOURCE_BINDING_V2_SCHEMA_VERSION: Literal["riftx.audit-source-binding/v2"] = (
    "riftx.audit-source-binding/v2"
)
AUDIT_PREFLIGHT_CAPABILITY_SNAPSHOT_V2_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-capability-snapshot/v2"
] = "riftx.audit-preflight-capability-snapshot/v2"
AUDIT_DRAFT_EXECUTION_SELECTION_V2_SCHEMA_VERSION: Literal[
    "riftx.audit-draft-execution-selection/v2"
] = "riftx.audit-draft-execution-selection/v2"
AUDIT_EXECUTION_READINESS_V2_SCHEMA_VERSION: Literal[
    "riftx.audit-execution-readiness/v2"
] = "riftx.audit-execution-readiness/v2"
AUDIT_SECURITY_CONTEXT_BINDING_V2_SCHEMA_VERSION: Literal[
    "riftx.audit-security-context-binding/v2"
] = "riftx.audit-security-context-binding/v2"
AUDIT_DRAFT_BUDGET_V2_SCHEMA_VERSION: Literal["riftx.audit-draft-budget/v2"] = (
    "riftx.audit-draft-budget/v2"
)
AUDIT_MODEL_EGRESS_V2_SCHEMA_VERSION: Literal["riftx.model-data-egress/v2"] = (
    "riftx.model-data-egress/v2"
)

AUDIT_CONTRACT_V2_STAGE: Literal["preflight_bound_draft"] = "preflight_bound_draft"
AUDIT_CONTRACT_V2_MISSING_CAPABILITIES: tuple[str, ...] = (
    "analysis_backend_prepare",
    "detector_registry",
    "scope_ledger",
    "snapshot_materializer",
    "snapshot_mount",
    "snapshot_store",
    "start_delivery",
)

MAX_AUDIT_SOURCE_SCOPE_PATHS = 512
MAX_AUDIT_SOURCE_SCOPE_PATH_BYTES = 4_096
MAX_AUDIT_SOURCE_SCOPE_TOTAL_BYTES = 64 * 1_024

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$"
_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+:/\-]{0,127}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_DIGEST_OR_EMPTY_PATTERN = r"^(?:|[0-9a-f]{64})$"
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

type AuditContractV2Id = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_ID_PATTERN),
]
type AuditContractV2Version = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_VERSION_PATTERN),
]
type AuditContractV2Digest = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=_DIGEST_PATTERN),
]
type AuditContractV2GitObjectId = Annotated[
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


def _set_or_validate_digest(
    model: AuditStrictModel,
    *,
    field_name: str,
    expected: str,
    label: str,
) -> None:
    supplied = getattr(model, field_name)
    if supplied:
        if not hmac.compare_digest(supplied, expected):
            raise ValueError(f"{label} digest does not match its frozen fields")
    else:
        object.__setattr__(model, field_name, expected)


def _validate_relative_path(value: str) -> str:
    if value != value.strip() or not value:
        raise ValueError("source scope path must be non-empty without surrounding whitespace")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("source scope path must use NFC normalization")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("source scope path must not contain control characters")
    if len(value.encode("utf-8")) > MAX_AUDIT_SOURCE_SCOPE_PATH_BYTES:
        raise ValueError("source scope path exceeds its byte limit")
    if value.startswith(("/", "~")) or "\\" in value or _WINDOWS_DRIVE_PREFIX.match(value):
        raise ValueError("source scope path must be repository-relative")
    if any(component in {"", ".", ".."} for component in value.split("/")):
        raise ValueError("source scope path must not contain empty or dot segments")
    return value


def _capability_by_id(
    matrix: AuditPreflightCapabilityMatrix,
    capability_id: str,
) -> AuditPreflightCapabilityFact:
    matches = tuple(entry for entry in matrix.entries if entry.capability_id == capability_id)
    if len(matches) != 1:
        raise ValueError(f"preflight capability {capability_id} must appear exactly once")
    return matches[0]


class AuditSourceTargetV2(AuditStrictModel):
    """Source target and resolved Git state proved by AUD-200."""

    schema_version: Literal["riftx.audit-source-target/v2"] = (
        AUDIT_SOURCE_TARGET_V2_SCHEMA_VERSION
    )
    repository_path: str = Field(min_length=1, max_length=4_096, repr=False)
    kind: SourceTargetKind
    revision: str = Field(min_length=1, max_length=1_024)
    base_revision: None = None
    include_untracked: bool = False
    head_revision: AuditContractV2GitObjectId
    resolved_revision: AuditContractV2GitObjectId
    resolved_base_revision: None = None
    merge_base_revision: None = None
    target_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @model_validator(mode="after")
    def validate_target(self) -> AuditSourceTargetV2:
        SourceTarget(
            repository_path=self.repository_path,
            kind=self.kind,
            revision=self.revision,
            base_revision=self.base_revision,
            include_untracked=self.include_untracked,
        )
        expected = _domain_digest(
            AUDIT_SOURCE_TARGET_V2_SCHEMA_VERSION,
            self.model_dump(mode="json", exclude={"target_digest"}),
        )
        _set_or_validate_digest(
            self,
            field_name="target_digest",
            expected=expected,
            label="source target",
        )
        return self


class AuditSourceScopeV2(AuditStrictModel):
    """Canonical caller Scope frozen by the preflight request digest."""

    schema_version: Literal["riftx.audit-source-scope/v2"] = (
        AUDIT_SOURCE_SCOPE_V2_SCHEMA_VERSION
    )
    include_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=MAX_AUDIT_SOURCE_SCOPE_PATHS,
    )
    exclude_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=MAX_AUDIT_SOURCE_SCOPE_PATHS,
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
        for value in values:
            _validate_relative_path(value)
        if values != tuple(sorted(values)):
            raise ValueError("source scope paths must use canonical sorted order")
        if len(values) != len(set(values)):
            raise ValueError("source scope paths must not contain duplicates")
        if sum(len(value.encode("utf-8")) for value in values) > (
            MAX_AUDIT_SOURCE_SCOPE_TOTAL_BYTES
        ):
            raise ValueError("source scope paths exceed their total byte limit")
        return values

    @model_validator(mode="after")
    def validate_scope(self) -> AuditSourceScopeV2:
        if set(self.include_paths).intersection(self.exclude_paths):
            raise ValueError("include_paths and exclude_paths must not overlap")
        expected = _domain_digest(
            AUDIT_SOURCE_SCOPE_V2_SCHEMA_VERSION,
            self.model_dump(mode="json", exclude={"scope_digest"}),
        )
        _set_or_validate_digest(
            self,
            field_name="scope_digest",
            expected=expected,
            label="source scope",
        )
        return self


class AuditSourceBindingV2(AuditStrictModel):
    """Exact AUD-200 ownership, source, SourceIngest, and Git proofs."""

    schema_version: Literal["riftx.audit-source-binding/v2"] = (
        AUDIT_SOURCE_BINDING_V2_SCHEMA_VERSION
    )
    preflight_job_id: AuditContractV2Id
    preflight_request_schema_version: Literal["riftx.audit-preflight-request/v1"] = (
        AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION
    )
    preflight_request_digest: AuditContractV2Digest
    preflight_result_schema_version: Literal["riftx.audit-preflight-result/v1"] = (
        AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION
    )
    preflight_result_digest: AuditContractV2Digest
    preflight_effect_owner_digest: AuditContractV2Digest
    source_node_id: Literal["local"] = "local"
    source_root_identity_digest: AuditContractV2Digest
    repository_identity_digest: AuditContractV2Digest
    content_identity_digest: AuditContractV2Digest
    source_ingest_backend_id: AuditContractV2Id
    source_ingest_backend_component_version: AuditContractV2Version
    source_ingest_backend_component_digest: AuditContractV2Digest
    source_ingest_image_digest: AuditContractV2Digest
    source_ingest_policy_digest: AuditContractV2Digest
    capsule_prepare_proof_digest: AuditContractV2Digest
    source_ingest_execution_proof_digest: AuditContractV2Digest
    git_component_version: AuditContractV2Version
    git_component_digest: AuditContractV2Digest
    git_proof_digest: AuditContractV2Digest
    binding_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @model_validator(mode="after")
    def validate_binding_digest(self) -> AuditSourceBindingV2:
        expected = _domain_digest(
            AUDIT_SOURCE_BINDING_V2_SCHEMA_VERSION,
            self.model_dump(mode="json", exclude={"binding_digest"}),
        )
        _set_or_validate_digest(
            self,
            field_name="binding_digest",
            expected=expected,
            label="source binding",
        )
        return self


class AuditPreflightCapabilitySnapshotV2(AuditStrictModel):
    """The exact, limited capability knowledge returned by AUD-200."""

    schema_version: Literal["riftx.audit-preflight-capability-snapshot/v2"] = (
        AUDIT_PREFLIGHT_CAPABILITY_SNAPSHOT_V2_SCHEMA_VERSION
    )
    matrix_schema_version: Literal["riftx.audit-preflight-capability-matrix/v1"] = (
        AUDIT_PREFLIGHT_CAPABILITY_MATRIX_SCHEMA_VERSION
    )
    matrix_digest: AuditContractV2Digest
    entries: tuple[AuditPreflightCapabilityFact, ...] = Field(max_length=128)
    snapshot_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @model_validator(mode="after")
    def validate_snapshot(self) -> AuditPreflightCapabilitySnapshotV2:
        matrix = AuditPreflightCapabilityMatrix(
            entries=self.entries,
            matrix_digest=self.matrix_digest,
        )
        identities = {entry.capability_id for entry in matrix.entries}
        if identities != {"detector_inventory", "git_metadata", "source_ingest"}:
            raise ValueError("AUD-201 requires the exact AUD-200 capability snapshot")

        detector_inventory = _capability_by_id(matrix, "detector_inventory")
        if (
            detector_inventory.status is not AuditPreflightCapabilityStatus.UNAVAILABLE
            or detector_inventory.reason_code != "audit_inventory_unavailable"
        ):
            raise ValueError("AUD-201 detector inventory must remain honestly unavailable")
        for capability_id in ("git_metadata", "source_ingest"):
            capability = _capability_by_id(matrix, capability_id)
            if capability.status is not AuditPreflightCapabilityStatus.AVAILABLE:
                raise ValueError("AUD-201 source and Git capabilities must be proved available")

        expected = _domain_digest(
            AUDIT_PREFLIGHT_CAPABILITY_SNAPSHOT_V2_SCHEMA_VERSION,
            self.model_dump(mode="json", exclude={"snapshot_digest"}),
        )
        _set_or_validate_digest(
            self,
            field_name="snapshot_digest",
            expected=expected,
            label="preflight capability snapshot",
        )
        return self

    @classmethod
    def from_matrix(
        cls,
        matrix: AuditPreflightCapabilityMatrix,
    ) -> AuditPreflightCapabilitySnapshotV2:
        return cls(
            matrix_schema_version=matrix.schema_version,
            matrix_digest=matrix.matrix_digest,
            entries=matrix.entries,
        )

    def capability(self, capability_id: str) -> AuditPreflightCapabilityFact:
        return _capability_by_id(
            AuditPreflightCapabilityMatrix(
                entries=self.entries,
                matrix_digest=self.matrix_digest,
            ),
            capability_id,
        )


class AuditDraftExecutionSelectionV2(AuditStrictModel):
    """Staged execution selection with every unproved later-stage fact empty."""

    schema_version: Literal["riftx.audit-draft-execution-selection/v2"] = (
        AUDIT_DRAFT_EXECUTION_SELECTION_V2_SCHEMA_VERSION
    )
    source_node_id: Literal["local"] = "local"
    source_ingest_backend_id: AuditContractV2Id
    source_ingest_backend_component_digest: AuditContractV2Digest
    source_prepare_proof_digest: AuditContractV2Digest
    source_execution_proof_digest: AuditContractV2Digest
    security_context_bundle_id: Literal["riftx.audit-empty-security-context/v1"] = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    )
    security_context_bundle_digest: AuditContractV2Digest = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    )
    analysis_node_id: None = None
    analysis_backend_id: None = None
    analysis_backend_component_digest: None = None
    analysis_backend_image_digest: None = None
    analysis_backend_policy_digest: None = None
    analysis_backend_prepare_proof_digest: None = None
    snapshot_store_component_digest: None = None
    snapshot_store_proof_digest: None = None
    snapshot_materializer_component_digest: None = None
    snapshot_materializer_policy_digest: None = None
    snapshot_materializer_proof_digest: None = None
    snapshot_mount_policy_digest: None = None
    snapshot_mount_proof_digest: None = None
    eligible_candidates_digest: None = None
    selection_policy_version: None = None
    selection_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @field_validator("security_context_bundle_digest")
    @classmethod
    def validate_empty_context_digest(cls, value: str) -> str:
        if not hmac.compare_digest(value, AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST):
            raise ValueError("draft execution selection requires canonical empty context")
        return value

    @model_validator(mode="after")
    def validate_selection_digest(self) -> AuditDraftExecutionSelectionV2:
        expected = _domain_digest(
            AUDIT_DRAFT_EXECUTION_SELECTION_V2_SCHEMA_VERSION,
            self.model_dump(mode="json", exclude={"selection_digest"}),
        )
        _set_or_validate_digest(
            self,
            field_name="selection_digest",
            expected=expected,
            label="draft execution selection",
        )
        return self


class AuditExecutionReadinessV2(AuditStrictModel):
    """Why an AUD-201 contract cannot yet be admitted to Start."""

    schema_version: Literal["riftx.audit-execution-readiness/v2"] = (
        AUDIT_EXECUTION_READINESS_V2_SCHEMA_VERSION
    )
    status: Literal["blocked"] = "blocked"
    missing_capabilities: tuple[
        Literal[
            "analysis_backend_prepare",
            "detector_registry",
            "scope_ledger",
            "snapshot_materializer",
            "snapshot_mount",
            "snapshot_store",
            "start_delivery",
        ],
        ...,
    ] = AUDIT_CONTRACT_V2_MISSING_CAPABILITIES
    reason_code: Literal["audit_contract_not_start_ready"] = "audit_contract_not_start_ready"

    @field_validator("missing_capabilities")
    @classmethod
    def validate_missing_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != AUDIT_CONTRACT_V2_MISSING_CAPABILITIES:
            raise ValueError("AUD-201 readiness must list every unimplemented Start capability")
        return values


class AuditSecurityContextBindingV2(AuditStrictModel):
    """Insert-only AUD-201 binding to the canonical empty context root."""

    schema_version: Literal["riftx.audit-security-context-binding/v2"] = (
        AUDIT_SECURITY_CONTEXT_BINDING_V2_SCHEMA_VERSION
    )
    audit_id: AuditContractV2Id
    preflight_plan_id: AuditContractV2Id
    preflight_plan_digest: AuditContractV2Digest
    operator_principal_id: AuditContractV2Id
    authorization_scope_digest: AuditContractV2Digest
    security_context_bundle_id: Literal["riftx.audit-empty-security-context/v1"] = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    )
    security_context_bundle_digest: AuditContractV2Digest = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    )
    binding_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @field_validator("security_context_bundle_digest")
    @classmethod
    def validate_empty_context_digest(cls, value: str) -> str:
        if not hmac.compare_digest(value, AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST):
            raise ValueError("AUD-201 security context binding must use canonical empty context")
        return value

    @model_validator(mode="after")
    def validate_binding_digest(self) -> AuditSecurityContextBindingV2:
        expected = _domain_digest(
            AUDIT_SECURITY_CONTEXT_BINDING_V2_SCHEMA_VERSION,
            self.model_dump(mode="json", exclude={"binding_digest"}),
        )
        _set_or_validate_digest(
            self,
            field_name="binding_digest",
            expected=expected,
            label="security context binding",
        )
        return self


class AuditDraftBudgetV2(AuditStrictModel):
    """Caller budget frozen for an inactive deterministic draft."""

    schema_version: Literal["riftx.audit-draft-budget/v2"] = (
        AUDIT_DRAFT_BUDGET_V2_SCHEMA_VERSION
    )
    max_wall_seconds: int = Field(strict=True, ge=1, le=7_200)
    max_detector_jobs: int = Field(strict=True, ge=0, le=4_096)
    max_worker_jobs: int = Field(strict=True, ge=1, le=64)
    max_epochs: int = Field(strict=True, ge=1, le=8)
    max_model_calls: Literal[0] = 0
    max_input_tokens: Literal[0] = 0
    max_output_tokens: Literal[0] = 0
    max_read_bytes: int = Field(strict=True, ge=1, le=2_147_483_648)
    max_candidates: int = Field(strict=True, ge=1, le=1_000)
    max_signals: int = Field(strict=True, ge=1, le=16_000)
    max_dynamic_validations: Literal[0] = 0
    max_artifact_output_bytes: int = Field(strict=True, ge=1, le=268_435_456)

    @property
    def budget_digest(self) -> str:
        return _domain_digest(
            AUDIT_DRAFT_BUDGET_V2_SCHEMA_VERSION,
            self.model_dump(mode="json"),
        )


class ModelDataEgressPolicyV2(AuditStrictModel):
    """Inactive model policy; AUD-201 has no model/provider execution capability."""

    schema_version: Literal["riftx.model-data-egress/v2"] = (
        AUDIT_MODEL_EGRESS_V2_SCHEMA_VERSION
    )
    disposition: Literal["inactive"] = "inactive"
    mode: Literal["local_only"] = "local_only"
    security_context_bundle_digest: AuditContractV2Digest = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    )
    model_profile: None = None
    model_profile_digest: None = None
    endpoint_origin_digest: None = None
    provider_display_name: None = None
    execution_locality: None = None
    retention_training_disclosure: None = None
    allowed_scope_classes: tuple[()] = ()
    allowed_remote_origins: tuple[()] = ()
    max_bytes_per_call: Literal[0] = 0
    max_bytes_per_audit: Literal[0] = 0
    redaction_policy_version: None = None
    redaction_policy_digest: None = None
    operator_consent_requirement_digest: None = None
    operator_consent_at: None = None
    policy_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=_DIGEST_OR_EMPTY_PATTERN,
    )

    @field_validator("security_context_bundle_digest")
    @classmethod
    def validate_empty_context_digest(cls, value: str) -> str:
        if not hmac.compare_digest(value, AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST):
            raise ValueError("inactive model egress must bind canonical empty context")
        return value

    @model_validator(mode="after")
    def validate_policy_digest(self) -> ModelDataEgressPolicyV2:
        expected = _domain_digest(
            AUDIT_MODEL_EGRESS_V2_SCHEMA_VERSION,
            self.model_dump(mode="json", exclude={"policy_digest"}),
        )
        _set_or_validate_digest(
            self,
            field_name="policy_digest",
            expected=expected,
            label="model data egress policy",
        )
        return self

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))


class AuditContractV2(AuditStrictModel):
    """Immutable, authoritative, but deliberately non-startable AUD-201 contract."""

    schema_version: Literal["riftx.audit-contract/v2"] = AUDIT_CONTRACT_V2_SCHEMA_VERSION
    contract_stage: Literal["preflight_bound_draft"] = AUDIT_CONTRACT_V2_STAGE
    start_eligible: Literal[False] = False
    audit_id: AuditContractV2Id
    project_id: AuditContractV2Id
    preflight_plan_id: AuditContractV2Id
    preflight_plan_digest: AuditContractV2Digest
    operator_principal_id: AuditContractV2Id
    source_target: AuditSourceTargetV2
    source_scope: AuditSourceScopeV2
    source_binding: AuditSourceBindingV2
    security_context_bundle_id: Literal["riftx.audit-empty-security-context/v1"] = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    )
    security_context_bundle_digest: AuditContractV2Digest = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    )
    security_context_binding: AuditSecurityContextBindingV2
    mode: AuditMode = AuditMode.STANDARD
    analysis_profile: AnalysisProfile = AnalysisProfile.DETERMINISTIC
    validation_policy: ValidationPolicy = ValidationPolicy.STATIC_ONLY
    baseline_audit_id: None = None
    detectors: tuple[()] = ()
    rulepacks: tuple[()] = ()
    parsers: tuple[()] = ()
    model_profile: None = None
    model_profile_digest: None = None
    model_data_egress_policy: ModelDataEgressPolicyV2
    budget: AuditDraftBudgetV2
    minimum_feasible_budget: AuditPreflightMinimumFeasibleBudget
    preflight_capability_snapshot: AuditPreflightCapabilitySnapshotV2
    execution_selection: AuditDraftExecutionSelectionV2
    execution_readiness: AuditExecutionReadinessV2 = Field(
        default_factory=AuditExecutionReadinessV2
    )

    @field_validator("security_context_bundle_digest")
    @classmethod
    def validate_empty_context_digest(cls, value: str) -> str:
        if not hmac.compare_digest(value, AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST):
            raise ValueError("AUD-201 contract must bind canonical empty context")
        return value

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: AuditMode) -> AuditMode:
        if value is not AuditMode.STANDARD:
            raise ValueError("AUD-201 supports only standard mode")
        return value

    @field_validator("analysis_profile")
    @classmethod
    def validate_analysis_profile(cls, value: AnalysisProfile) -> AnalysisProfile:
        if value is not AnalysisProfile.DETERMINISTIC:
            raise ValueError("AUD-201 supports only deterministic analysis")
        return value

    @field_validator("validation_policy")
    @classmethod
    def validate_validation_policy(cls, value: ValidationPolicy) -> ValidationPolicy:
        if value is not ValidationPolicy.STATIC_ONLY:
            raise ValueError("AUD-201 supports only static_only validation")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> AuditContractV2:
        security_binding = self.security_context_binding
        if (
            security_binding.audit_id != self.audit_id
            or security_binding.preflight_plan_id != self.preflight_plan_id
            or security_binding.operator_principal_id != self.operator_principal_id
            or not hmac.compare_digest(
                security_binding.preflight_plan_digest,
                self.preflight_plan_digest,
            )
        ):
            raise ValueError(
                "security context binding must bind this Audit, principal, and Preflight plan"
            )
        if (
            security_binding.security_context_bundle_id != self.security_context_bundle_id
            or not hmac.compare_digest(
                security_binding.security_context_bundle_digest,
                self.security_context_bundle_digest,
            )
            or not hmac.compare_digest(
                self.model_data_egress_policy.security_context_bundle_digest,
                self.security_context_bundle_digest,
            )
        ):
            raise ValueError("contract roots must bind the same canonical empty context")

        selection = self.execution_selection
        source = self.source_binding
        if (
            selection.source_node_id != source.source_node_id
            or selection.source_ingest_backend_id != source.source_ingest_backend_id
            or not hmac.compare_digest(
                selection.source_ingest_backend_component_digest,
                source.source_ingest_backend_component_digest,
            )
            or not hmac.compare_digest(
                selection.source_prepare_proof_digest,
                source.capsule_prepare_proof_digest,
            )
            or not hmac.compare_digest(
                selection.source_execution_proof_digest,
                source.source_ingest_execution_proof_digest,
            )
        ):
            raise ValueError("draft execution selection must match the AUD-200 source binding")

        source_capability = self.preflight_capability_snapshot.capability("source_ingest")
        git_capability = self.preflight_capability_snapshot.capability("git_metadata")
        if (
            source_capability.component_version
            != source.source_ingest_backend_component_version
            or source_capability.component_digest
            != source.source_ingest_backend_component_digest
            or source_capability.proof_digest
            != source.source_ingest_execution_proof_digest
        ):
            raise ValueError("source_ingest snapshot must match the source binding")
        if (
            git_capability.component_version != source.git_component_version
            or git_capability.component_digest != source.git_component_digest
            or git_capability.proof_digest != source.git_proof_digest
        ):
            raise ValueError("git_metadata snapshot must match the source binding")

        if self.minimum_feasible_budget.status is AuditPreflightBudgetStatus.BLOCKING:
            raise ValueError("blocking preflight result cannot create an Audit contract")
        return self

    @classmethod
    def from_preflight_plan(
        cls,
        *,
        audit_id: str,
        project_id: str,
        plan: AuditPreflightPlan,
        budget: AuditDraftBudgetV2,
    ) -> AuditContractV2:
        """Build only from the durable, authoritative Plan used by Create v2."""

        if plan.status is not AuditPreflightPlanStatus.RESERVED:
            raise ValueError("Audit contract requires a reserved Preflight plan")
        if plan.reserved_audit_id != audit_id:
            raise ValueError("Preflight plan is reserved for a different Audit")
        if plan.target.mode is not AuditMode.STANDARD:
            raise ValueError("AUD-201 supports only standard Preflight plans")

        capability_snapshot = AuditPreflightCapabilitySnapshotV2.from_matrix(
            plan.capability_matrix
        )
        source_ingest = capability_snapshot.capability("source_ingest")
        git_metadata = capability_snapshot.capability("git_metadata")
        assert source_ingest.component_version is not None
        assert source_ingest.component_digest is not None
        assert source_ingest.proof_digest is not None
        assert git_metadata.component_version is not None
        assert git_metadata.component_digest is not None
        assert git_metadata.proof_digest is not None

        target = plan.target
        source_target = AuditSourceTargetV2(
            repository_path=target.repository_path,
            kind=target.kind,
            revision=target.revision,
            base_revision=target.base_revision,
            include_untracked=target.include_untracked,
            head_revision=target.head_revision,
            resolved_revision=target.resolved_revision,
            resolved_base_revision=target.resolved_base_revision,
            merge_base_revision=target.merge_base_revision,
        )
        source_scope = AuditSourceScopeV2(
            include_paths=plan.scope.include_paths,
            exclude_paths=plan.scope.exclude_paths,
        )
        source_binding = AuditSourceBindingV2(
            preflight_job_id=plan.preflight_job_id,
            preflight_request_schema_version=plan.request_schema_version,
            preflight_request_digest=plan.request_digest,
            preflight_result_schema_version=plan.result_schema_version,
            preflight_result_digest=plan.result_digest,
            preflight_effect_owner_digest=plan.effect_owner_digest,
            source_node_id=plan.source_node_id,
            source_root_identity_digest=plan.source_root_identity_digest,
            repository_identity_digest=plan.repository_identity_digest,
            content_identity_digest=plan.content_identity_digest,
            source_ingest_backend_id=plan.backend_id,
            source_ingest_backend_component_version=source_ingest.component_version,
            source_ingest_backend_component_digest=source_ingest.component_digest,
            source_ingest_image_digest=plan.image_digest,
            source_ingest_policy_digest=plan.policy_digest,
            capsule_prepare_proof_digest=plan.capsule_prepare_proof_digest,
            source_ingest_execution_proof_digest=source_ingest.proof_digest,
            git_component_version=git_metadata.component_version,
            git_component_digest=git_metadata.component_digest,
            git_proof_digest=git_metadata.proof_digest,
        )
        security_binding = AuditSecurityContextBindingV2(
            audit_id=audit_id,
            preflight_plan_id=plan.plan_id,
            preflight_plan_digest=plan.plan_digest,
            operator_principal_id=plan.operator_principal_id,
            authorization_scope_digest=plan.authorization_scope_digest,
            security_context_bundle_id=plan.security_context_id,
            security_context_bundle_digest=plan.security_context_digest,
        )
        execution_selection = AuditDraftExecutionSelectionV2(
            source_node_id=source_binding.source_node_id,
            source_ingest_backend_id=source_binding.source_ingest_backend_id,
            source_ingest_backend_component_digest=(
                source_binding.source_ingest_backend_component_digest
            ),
            source_prepare_proof_digest=source_binding.capsule_prepare_proof_digest,
            source_execution_proof_digest=(
                source_binding.source_ingest_execution_proof_digest
            ),
            security_context_bundle_id=plan.security_context_id,
            security_context_bundle_digest=plan.security_context_digest,
        )
        return cls(
            audit_id=audit_id,
            project_id=project_id,
            preflight_plan_id=plan.plan_id,
            preflight_plan_digest=plan.plan_digest,
            operator_principal_id=plan.operator_principal_id,
            source_target=source_target,
            source_scope=source_scope,
            source_binding=source_binding,
            security_context_bundle_id=plan.security_context_id,
            security_context_bundle_digest=plan.security_context_digest,
            security_context_binding=security_binding,
            model_data_egress_policy=ModelDataEgressPolicyV2(
                security_context_bundle_digest=plan.security_context_digest
            ),
            budget=budget,
            minimum_feasible_budget=plan.minimum_feasible_budget,
            preflight_capability_snapshot=capability_snapshot,
            execution_selection=execution_selection,
        )

    def canonical_json(self) -> str:
        canonical = _canonical_json(self.model_dump(mode="json"))
        if len(canonical.encode("utf-8")) > MAX_AUDIT_CONTRACT_BYTES:
            raise ValueError("canonical Audit v2 contract exceeds its byte limit")
        return canonical

    @property
    def contract_digest(self) -> str:
        return _domain_digest(
            AUDIT_CONTRACT_V2_SCHEMA_VERSION,
            self.model_dump(mode="json"),
        )

    @property
    def source_target_digest(self) -> str:
        return self.source_target.target_digest

    @property
    def source_scope_digest(self) -> str:
        return self.source_scope.scope_digest

    @classmethod
    def from_canonical_json(cls, canonical_json: str) -> AuditContractV2:
        """Restore only an exact canonical v2 document; reject rewritten JSON."""

        if len(canonical_json.encode("utf-8")) > MAX_AUDIT_CONTRACT_BYTES:
            raise ValueError("canonical Audit v2 contract exceeds its byte limit")
        restored = cls.model_validate_json(canonical_json)
        if restored.canonical_json() != canonical_json:
            raise ValueError("Audit v2 contract JSON is not canonical")
        return restored


class AuditContractRecordV2(AuditStrictModel):
    """Persistence-facing canonical record for a staged v2 contract."""

    contract_id: AuditContractV2Id = Field(default_factory=new_id)
    audit_id: AuditContractV2Id
    schema_version: Literal["riftx.audit-contract/v2"] = AUDIT_CONTRACT_V2_SCHEMA_VERSION
    canonical_contract_json: str = Field(
        min_length=2,
        max_length=MAX_AUDIT_CONTRACT_BYTES,
        repr=False,
    )
    contract_digest: AuditContractV2Digest
    source_target_digest: AuditContractV2Digest
    source_node_id: Literal["local"] = "local"
    source_ingest_backend_digest: AuditContractV2Digest
    source_prepare_proof_digest: AuditContractV2Digest
    preflight_plan_id: AuditContractV2Id
    preflight_plan_digest: AuditContractV2Digest
    security_context_bundle_id: Literal["riftx.audit-empty-security-context/v1"] = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    )
    security_context_bundle_digest: AuditContractV2Digest = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    )
    created_at: AwareDatetime = Field(default_factory=utc_now)
    sealed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_record(self) -> AuditContractRecordV2:
        contract = AuditContractV2.from_canonical_json(self.canonical_contract_json)
        selection = contract.execution_selection
        checks: tuple[tuple[object, object, str], ...] = (
            (self.audit_id, contract.audit_id, "audit_id"),
            (self.contract_digest, contract.contract_digest, "contract_digest"),
            (
                self.source_target_digest,
                contract.source_target_digest,
                "source_target_digest",
            ),
            (self.source_node_id, selection.source_node_id, "source_node_id"),
            (
                self.source_ingest_backend_digest,
                selection.source_ingest_backend_component_digest,
                "source_ingest_backend_digest",
            ),
            (
                self.source_prepare_proof_digest,
                selection.source_prepare_proof_digest,
                "source_prepare_proof_digest",
            ),
            (self.preflight_plan_id, contract.preflight_plan_id, "preflight_plan_id"),
            (
                self.preflight_plan_digest,
                contract.preflight_plan_digest,
                "preflight_plan_digest",
            ),
            (
                self.security_context_bundle_id,
                contract.security_context_bundle_id,
                "security_context_bundle_id",
            ),
            (
                self.security_context_bundle_digest,
                contract.security_context_bundle_digest,
                "security_context_bundle_digest",
            ),
        )
        for actual, expected, label in checks:
            equal = (
                hmac.compare_digest(actual, expected)
                if isinstance(actual, str) and isinstance(expected, str)
                else actual == expected
            )
            if not equal:
                raise ValueError(f"Audit v2 contract record {label} does not match contract")
        if self.sealed_at is not None and self.sealed_at < self.created_at:
            raise ValueError("Audit v2 contract sealed_at must not precede created_at")
        return self

    @classmethod
    def from_contract(
        cls,
        contract: AuditContractV2,
        *,
        contract_id: str | None = None,
        created_at: datetime | None = None,
        sealed_at: datetime | None = None,
    ) -> AuditContractRecordV2:
        contract = AuditContractV2.model_validate(contract)
        selection = contract.execution_selection
        return cls(
            contract_id=contract_id if contract_id is not None else new_id(),
            audit_id=contract.audit_id,
            canonical_contract_json=contract.canonical_json(),
            contract_digest=contract.contract_digest,
            source_target_digest=contract.source_target_digest,
            source_node_id=selection.source_node_id,
            source_ingest_backend_digest=(
                selection.source_ingest_backend_component_digest
            ),
            source_prepare_proof_digest=selection.source_prepare_proof_digest,
            preflight_plan_id=contract.preflight_plan_id,
            preflight_plan_digest=contract.preflight_plan_digest,
            security_context_bundle_id=contract.security_context_bundle_id,
            security_context_bundle_digest=contract.security_context_bundle_digest,
            created_at=created_at or utc_now(),
            sealed_at=sealed_at,
        )

    def contract(self) -> AuditContractV2:
        return AuditContractV2.from_canonical_json(self.canonical_contract_json)

    def seal(self, *, at: datetime | None = None) -> AuditContractRecordV2:
        if self.sealed_at is not None:
            return self
        payload = self.model_dump(mode="python")
        payload["sealed_at"] = at or utc_now()
        return type(self).model_validate(payload)


def audit_contract_v2_digest(contract: AuditContractV2) -> str:
    return contract.contract_digest


def audit_contract_v2_canonical_payload(
    contract: AuditContractV2,
) -> Mapping[str, Any]:
    """Return a detached JSON-shaped value for persistence mappers."""

    return contract.model_dump(mode="json")
