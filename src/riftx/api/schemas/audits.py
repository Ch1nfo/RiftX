"""Strict HTTP wire schemas for the draft-only Code Audit API surface."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from riftx.application.ports import AuditAggregate
from riftx.application.services import (
    AuditContractBlueprint,
    AuditDraftResult,
    CreateAuditDraft,
)
from riftx.domain import (
    AnalysisProfile,
    AuditClosureStatus,
    AuditContract,
    AuditLanguageTier,
    AuditLifecycleStatus,
    AuditMode,
    AuditPhase,
    AuditPhaseRequirement,
    AuditPublicationStatus,
    AuditPurpose,
    AuditRuntimeMissingOutcome,
    AuditStartMissingOutcome,
    AuditTerminalOutcome,
    AuditVcsKind,
    ModelDataEgressMode,
    ModelExecutionLocality,
    ModelTrainingUsage,
    RunStatus,
    SourceTargetKind,
    ValidationPolicy,
)

_AUDIT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$"
_AUDIT_NODE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,63}$"
_AUDIT_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,255}$"
_AUDIT_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+:/\-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SHA256 = re.compile(_SHA256_PATTERN)

type _AuditId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_AUDIT_ID_PATTERN),
]
type _EngagementId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=_AUDIT_ID_PATTERN),
]
type _AuditNodeId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=_AUDIT_NODE_ID_PATTERN),
]
type _AuditToken = Annotated[
    str,
    Field(min_length=1, max_length=256, pattern=_AUDIT_TOKEN_PATTERN),
]
type _AuditVersion = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_AUDIT_VERSION_PATTERN),
]
type _Sha256Digest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=_SHA256_PATTERN),
]


class _AuditWireModel(BaseModel):
    """Non-strict JSON adapter which still rejects every undeclared field."""

    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        populate_by_name=True,
        validate_default=True,
    )


class _AuditResponseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )


class AuditSourceTargetRequest(_AuditWireModel):
    repository_path: str = Field(min_length=1, max_length=4096, repr=False)
    kind: SourceTargetKind
    revision: str = Field(min_length=1, max_length=1024)
    base_revision: str | None = Field(default=None, min_length=1, max_length=1024)
    include_untracked: bool = False


class AuditBudgetRequest(_AuditWireModel):
    schema_version: Literal["riftx.audit-budget/v1"] = "riftx.audit-budget/v1"
    max_wall_seconds: int = Field(ge=1, le=7_200)
    max_detector_jobs: int = Field(ge=1, le=4_096)
    max_worker_jobs: int = Field(ge=1, le=64)
    max_epochs: int = Field(ge=1, le=8)
    max_model_calls: int = Field(ge=0, le=100)
    max_input_tokens: int = Field(ge=0, le=2_000_000)
    max_output_tokens: int = Field(ge=0, le=200_000)
    max_read_bytes: int = Field(ge=1, le=2_147_483_648)
    max_candidates: int = Field(ge=1, le=1_000)
    max_signals: int = Field(ge=1, le=16_000)
    max_dynamic_validations: int = Field(ge=0, le=1_000)
    max_artifact_output_bytes: int = Field(ge=1, le=268_435_456)


class AuditCapabilityMissingOutcomeRequest(_AuditWireModel):
    start: AuditStartMissingOutcome
    runtime: AuditRuntimeMissingOutcome


class AuditCapabilityVersionRequest(_AuditWireModel):
    minimum_version: _AuditVersion
    component_digest: _Sha256Digest


class AuditCapabilityRequirementRequest(_AuditWireModel):
    matrix_schema_version: Literal["riftx.audit-capability-matrix/v1"] = (
        "riftx.audit-capability-matrix/v1"
    )
    phase: AuditPhase
    capability_id: _AuditToken
    requirement: AuditPhaseRequirement
    scope_classes: tuple[_AuditToken, ...] = Field(default_factory=tuple, max_length=64)
    language_tiers: tuple[AuditLanguageTier, ...] = Field(
        default_factory=tuple,
        max_length=4,
    )
    provider_id: _AuditToken | None = None
    node_id: _AuditId | None = None
    backend_id: _AuditToken | None = None
    min_version_and_digest: AuditCapabilityVersionRequest | None = None
    proof_kind: _AuditToken | None = None
    proof_digest: _Sha256Digest | None = None
    missing_outcome: AuditCapabilityMissingOutcomeRequest
    reason_code: _AuditToken | None = None


class AuditCapabilityMatrixRequest(_AuditWireModel):
    schema_version: Literal["riftx.audit-capability-matrix/v1"] = "riftx.audit-capability-matrix/v1"
    entries: tuple[AuditCapabilityRequirementRequest, ...] = Field(
        min_length=1,
        max_length=512,
    )


class AuditVersionedComponentRequest(_AuditWireModel):
    component_id: _AuditToken
    version: _AuditVersion
    digest: _Sha256Digest


class AuditSchemaVersionRequest(_AuditWireModel):
    schema_id: _AuditToken
    version: _AuditVersion


class AuditCanonicalPolicyRequest(_AuditWireModel):
    document_schema_version: Literal["riftx.versioned-policy-document/v1"] = (
        "riftx.versioned-policy-document/v1"
    )
    policy_schema_version: _AuditVersion
    canonical_json: str = Field(min_length=2, max_length=65_536, repr=False)
    digest: _Sha256Digest


class AuditModelDisclosureRequest(_AuditWireModel):
    schema_version: Literal["riftx.model-retention-training-disclosure/v1"] = (
        "riftx.model-retention-training-disclosure/v1"
    )
    data_residency_regions: tuple[_AuditToken, ...] = Field(
        min_length=1,
        max_length=32,
    )
    retention_days: int = Field(ge=0, le=3_650)
    training_usage: ModelTrainingUsage
    provider_terms_version: _AuditVersion
    provider_terms_digest: _Sha256Digest


class AuditModelDataEgressRequest(_AuditWireModel):
    schema_version: Literal["riftx.model-data-egress/v1"] = "riftx.model-data-egress/v1"
    mode: ModelDataEgressMode
    model_profile_digest: _Sha256Digest | None = None
    endpoint_origin_digest: _Sha256Digest | None = None
    provider_display_name: str | None = Field(default=None, min_length=1, max_length=256)
    execution_locality: ModelExecutionLocality | None = None
    retention_training_disclosure: AuditModelDisclosureRequest | None = None
    allowed_scope_classes: tuple[_AuditToken, ...] = Field(
        default_factory=tuple,
        max_length=64,
    )
    allowed_remote_origins: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    max_bytes_per_call: int = Field(ge=1, le=131_072)
    max_bytes_per_audit: int = Field(ge=1, le=16_777_216)
    redaction_policy_version: _AuditVersion | None = None
    redaction_policy_digest: _Sha256Digest | None = None
    operator_consent_requirement_digest: _Sha256Digest | None = None
    operator_consent_at: AwareDatetime | None = None
    policy_digest: _Sha256Digest | None = None


class AuditExecutionSelectionRequest(_AuditWireModel):
    schema_version: Literal["riftx.audit-execution-selection/v1"] = (
        "riftx.audit-execution-selection/v1"
    )
    source_node_id: _AuditNodeId
    source_ingest_backend_id: _AuditToken
    source_ingest_backend_digest: _Sha256Digest
    source_prepare_proof_digest: _Sha256Digest
    selected_node_id: _AuditNodeId
    required_backend_id: _AuditToken
    analysis_backend_digest: _Sha256Digest
    analysis_prepare_proof_digest: _Sha256Digest
    analysis_image_digest: _Sha256Digest
    analysis_policy_digest: _Sha256Digest
    snapshot_hydration_policy_digest: _Sha256Digest
    selection_policy_version: _AuditVersion
    eligible_candidates_digest: _Sha256Digest


class AuditContractDraftRequest(_AuditWireModel):
    """Caller-owned frozen fields; aggregate identities are always server-bound."""

    schema_version: Literal["riftx.audit-contract/v1"] = "riftx.audit-contract/v1"
    source_target: AuditSourceTargetRequest = Field(repr=False)
    mode: AuditMode
    analysis_profile: AnalysisProfile
    baseline_audit_id: _AuditId | None = None
    scope_capture_policy: AuditCanonicalPolicyRequest = Field(repr=False)
    detectors: tuple[AuditVersionedComponentRequest, ...] = Field(
        min_length=1,
        max_length=256,
    )
    rulepacks: tuple[AuditVersionedComponentRequest, ...] = Field(
        default_factory=tuple,
        max_length=128,
    )
    parsers: tuple[AuditVersionedComponentRequest, ...] = Field(
        default_factory=tuple,
        max_length=128,
    )
    model_profile: _AuditToken | None = Field(default=None, max_length=255)
    model_profile_digest: _Sha256Digest | None = None
    model_data_egress_policy: AuditModelDataEgressRequest = Field(repr=False)
    validation_policy: ValidationPolicy
    validation_policy_document: AuditCanonicalPolicyRequest = Field(repr=False)
    budget: AuditBudgetRequest
    execution_selection: AuditExecutionSelectionRequest = Field(repr=False)
    capability_matrix: AuditCapabilityMatrixRequest = Field(repr=False)
    policy_digest: _Sha256Digest
    config_digest: _Sha256Digest
    schema_versions: tuple[AuditSchemaVersionRequest, ...] = Field(
        min_length=1,
        max_length=128,
    )

    @model_validator(mode="after")
    def validate_domain_contract(self) -> AuditContractDraftRequest:
        self._domain_contract()
        return self

    def to_blueprint(self) -> AuditContractBlueprint:
        return AuditContractBlueprint.from_contract(self._domain_contract())

    def _domain_contract(self) -> AuditContract:
        audit_id = "wire-unbound-audit"
        if self.baseline_audit_id == audit_id:
            audit_id = "wire-unbound-audit-alternate"
        payload = self.model_dump(mode="python")
        payload.update(
            audit_id=audit_id,
            project_id="wire-unbound-project",
        )
        try:
            return AuditContract.model_validate(payload)
        except (TypeError, ValueError):
            raise ValueError("The frozen Code Audit contract is invalid") from None


class CreateAuditDraftRequest(_AuditWireModel):
    """AUD-104 draft-only request; no Preflight or execution admission is implied."""

    client_request_id: str = Field(min_length=36, max_length=36)
    project_name: str = Field(min_length=1, max_length=255)
    repository_identity_digest: _Sha256Digest
    contract: AuditContractDraftRequest = Field(repr=False)
    engagement_id: _EngagementId | None = None
    default_branch: str | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("client_request_id")
    @classmethod
    def validate_client_request_id(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except (AttributeError, ValueError):
            raise ValueError("client_request_id must be a canonical UUID") from None
        if parsed.int == 0 or str(parsed) != value:
            raise ValueError("client_request_id must be a non-zero canonical UUID")
        return value

    @field_validator("project_name")
    @classmethod
    def validate_project_name(cls, value: str) -> str:
        return _bounded_text(value, maximum_bytes=1024, label="project_name")

    @field_validator("engagement_id")
    @classmethod
    def validate_engagement_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, maximum_bytes=256, label="engagement_id")

    @field_validator("default_branch")
    @classmethod
    def validate_default_branch(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, maximum_bytes=4096, label="default_branch")

    def to_command(self, *, authorization_reference: str) -> CreateAuditDraft:
        return CreateAuditDraft(
            client_request_id=self.client_request_id,
            project_name=self.project_name,
            repository_identity_digest=self.repository_identity_digest,
            authorization_reference=_authorization_reference(authorization_reference),
            contract=self.contract.to_blueprint(),
            engagement_id=self.engagement_id,
            default_branch=self.default_branch,
        )


class AuditListQuery(_AuditWireModel):
    run_id: _AuditId | None = None
    project_id: _AuditId | None = None
    engagement_id: _AuditId | None = None
    lifecycle_status: AuditLifecycleStatus | None = Field(default=None, alias="status")
    mode: AuditMode | None = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_created_range(self) -> AuditListQuery:
        if (
            self.created_from is not None
            and self.created_to is not None
            and self.created_from > self.created_to
        ):
            raise ValueError("created_from must not be later than created_to")
        return self


class AuditProjectSummaryResponse(_AuditResponseModel):
    id: str
    engagement_id: str
    display_name: str
    vcs_kind: AuditVcsKind
    default_branch: str | None


class AuditResponse(_AuditResponseModel):
    """Positive public projection which never serializes the frozen Contract."""

    id: str
    run_id: str
    project: AuditProjectSummaryResponse
    state_version: int = Field(ge=1)
    snapshot_id: str | None
    base_snapshot_id: str | None
    baseline_audit_id: str | None
    purpose: AuditPurpose
    parent_audit_id: str | None
    mode: AuditMode
    analysis_profile: AnalysisProfile
    lifecycle_status: AuditLifecycleStatus
    current_phase: AuditPhase
    terminal_outcome: AuditTerminalOutcome | None
    closure_status: AuditClosureStatus | None
    publication_status: AuditPublicationStatus
    initial_distribution_revision_id: str | None
    latest_distribution_revision_id: str | None
    model_profile: str | None
    run_status: RunStatus
    created_at: datetime
    started_at: datetime | None
    analysis_finished_at: datetime | None
    publication_finished_at: datetime | None
    sealed_at: datetime | None

    @classmethod
    def from_aggregate(cls, aggregate: AuditAggregate) -> Self:
        if not isinstance(aggregate, AuditAggregate):
            raise TypeError("aggregate must be an AuditAggregate")
        scan = aggregate.audit.value
        project = aggregate.project.value
        return cls(
            id=scan.id,
            run_id=scan.run_id,
            project=AuditProjectSummaryResponse(
                id=project.id,
                engagement_id=project.engagement_id,
                display_name=project.display_name,
                vcs_kind=project.vcs_kind,
                default_branch=project.default_branch,
            ),
            state_version=aggregate.audit.state_version,
            snapshot_id=scan.snapshot_id,
            base_snapshot_id=scan.base_snapshot_id,
            baseline_audit_id=scan.baseline_audit_id,
            purpose=scan.purpose,
            parent_audit_id=scan.parent_audit_id,
            mode=scan.mode,
            analysis_profile=scan.analysis_profile,
            lifecycle_status=scan.lifecycle_status,
            current_phase=scan.current_phase,
            terminal_outcome=scan.terminal_outcome,
            closure_status=scan.closure_status,
            publication_status=scan.publication_status,
            initial_distribution_revision_id=scan.initial_distribution_revision_id,
            latest_distribution_revision_id=scan.latest_distribution_revision_id,
            model_profile=scan.model_profile,
            run_status=aggregate.run.status,
            created_at=scan.created_at,
            started_at=scan.started_at,
            analysis_finished_at=scan.analysis_finished_at,
            publication_finished_at=scan.publication_finished_at,
            sealed_at=scan.sealed_at,
        )


class AuditDraftResponse(_AuditResponseModel):
    created: bool
    replayed: bool
    audit: AuditResponse

    @model_validator(mode="after")
    def validate_disposition(self) -> AuditDraftResponse:
        if self.created == self.replayed:
            raise ValueError("exactly one of created or replayed must be true")
        return self

    @classmethod
    def from_result(cls, result: AuditDraftResult) -> Self:
        if not isinstance(result, AuditDraftResult):
            raise TypeError("result must be an AuditDraftResult")
        return cls(
            created=result.created,
            replayed=result.replayed,
            audit=AuditResponse.from_aggregate(result.aggregate),
        )


class AuditListResponse(_AuditResponseModel):
    items: list[AuditResponse]
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)

    @classmethod
    def from_aggregates(
        cls,
        aggregates: Sequence[AuditAggregate],
        *,
        limit: int,
        offset: int,
    ) -> Self:
        return cls(
            items=[AuditResponse.from_aggregate(aggregate) for aggregate in aggregates],
            limit=limit,
            offset=offset,
        )


def _bounded_text(value: str, *, maximum_bytes: int, label: str) -> str:
    if value != value.strip() or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError(f"{label} must use bounded canonical text")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its encoded byte limit")
    return value


def _authorization_reference(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("authorization_reference must be a lowercase SHA-256 digest")
    return value


__all__ = [
    "AuditContractDraftRequest",
    "AuditDraftResponse",
    "AuditListQuery",
    "AuditListResponse",
    "AuditProjectSummaryResponse",
    "AuditResponse",
    "CreateAuditDraftRequest",
]
