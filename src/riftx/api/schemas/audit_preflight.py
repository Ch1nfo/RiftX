"""Strict, path-redacted wire contracts for Code Audit Preflight Operator APIs."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)

from riftx.application.services.audit_preflight import AuditPreflightCreationResult
from riftx.domain import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID,
    AuditPreflightBudgetStatus,
    AuditPreflightCapabilityFact,
    AuditPreflightCapabilityMatrix,
    AuditPreflightCapabilityStatus,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightLanguageEstimate,
    AuditPreflightMinimumFeasibleBudget,
    AuditPreflightResult,
    AuditPreflightSecurityContext,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightTarget,
    PreflightRequest,
)

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$"
_SHORT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,63}$"
_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+:/\-]{0,127}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"

type _Id = Annotated[str, Field(min_length=1, max_length=128, pattern=_ID_PATTERN)]
type _ShortId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=_SHORT_ID_PATTERN),
]
type _Version = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_VERSION_PATTERN),
]
type _Digest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=_DIGEST_PATTERN),
]
type _GitObjectId = Annotated[
    str,
    Field(min_length=40, max_length=64, pattern=_GIT_OBJECT_ID_PATTERN),
]


class _PreflightWireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        validate_default=True,
    )


class _PreflightResponseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )


class AuditPreflightSourceExecutionTargetRequest(_PreflightWireModel):
    node_id: Literal["local"] = "local"
    source_ingest_backend: Literal["linux_container"] = "linux_container"


class AuditPreflightTargetRequest(_PreflightWireModel):
    kind: SourceTargetKind
    revision: str = Field(min_length=1, max_length=1_024, repr=False)
    base_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=1_024,
        repr=False,
    )
    include_untracked: StrictBool = False


class AuditPreflightSecurityContextRequest(_PreflightWireModel):
    """AUD-209 boundary: only the canonical empty context is accepted."""

    input_id: None = None
    repository_paths: tuple[()] = ()
    discover_defaults: StrictBool = False

    @model_validator(mode="after")
    def require_disabled_discovery(self) -> Self:
        if self.discover_defaults:
            raise ValueError("default context discovery is unavailable before AUD-209")
        return self


class CreateAuditPreflightRequest(_PreflightWireModel):
    schema_version: Literal["riftx.audit-preflight-request/v1"] = "riftx.audit-preflight-request/v1"
    client_request_id: str = Field(min_length=36, max_length=36)
    repository_path: str = Field(min_length=1, max_length=4_096, repr=False)
    source_execution_target: AuditPreflightSourceExecutionTargetRequest = Field(
        default_factory=AuditPreflightSourceExecutionTargetRequest
    )
    target: AuditPreflightTargetRequest
    include_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=512)
    exclude_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=512)
    security_context: AuditPreflightSecurityContextRequest = Field(
        default_factory=AuditPreflightSecurityContextRequest
    )
    mode: AuditMode = AuditMode.STANDARD

    @model_validator(mode="after")
    def validate_domain_contract(self) -> Self:
        try:
            request_id = UUID(self.client_request_id)
        except ValueError as exc:
            raise ValueError("client_request_id must be a canonical UUID") from exc
        if request_id.int == 0 or str(request_id) != self.client_request_id:
            raise ValueError("client_request_id must be a non-zero canonical UUID")
        try:
            self.to_domain()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Code Audit Preflight request contract is invalid") from exc
        return self

    def to_domain(self) -> PreflightRequest:
        return PreflightRequest(
            schema_version=self.schema_version,
            client_request_id=self.client_request_id,
            repository_path=self.repository_path,
            source_execution_target=AuditPreflightSourceExecutionTarget(
                **self.source_execution_target.model_dump(mode="python")
            ),
            target=AuditPreflightTarget(**self.target.model_dump(mode="python")),
            include_paths=self.include_paths,
            exclude_paths=self.exclude_paths,
            security_context=AuditPreflightSecurityContext(
                **self.security_context.model_dump(mode="python")
            ),
            mode=self.mode,
        )


class AuditPreflightLanguageEstimateResponse(_PreflightResponseModel):
    language_id: _Id
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)

    @classmethod
    def from_domain(
        cls,
        value: AuditPreflightLanguageEstimate,
    ) -> AuditPreflightLanguageEstimateResponse:
        return cls.model_validate(value, from_attributes=True)


class AuditPreflightCapabilityFactResponse(_PreflightResponseModel):
    capability_id: _Id
    status: AuditPreflightCapabilityStatus
    component_version: _Version | None = None
    component_digest: _Digest | None = None
    proof_digest: _Digest | None = None
    reason_code: _Id | None = None

    @classmethod
    def from_domain(
        cls,
        value: AuditPreflightCapabilityFact,
    ) -> AuditPreflightCapabilityFactResponse:
        return cls.model_validate(value, from_attributes=True)


class AuditPreflightCapabilityMatrixResponse(_PreflightResponseModel):
    schema_version: Literal["riftx.audit-preflight-capability-matrix/v1"]
    entries: tuple[AuditPreflightCapabilityFactResponse, ...]
    matrix_digest: _Digest

    @classmethod
    def from_domain(
        cls,
        value: AuditPreflightCapabilityMatrix,
    ) -> AuditPreflightCapabilityMatrixResponse:
        return cls(
            schema_version=value.schema_version,
            entries=tuple(
                AuditPreflightCapabilityFactResponse.from_domain(item) for item in value.entries
            ),
            matrix_digest=value.matrix_digest,
        )


class AuditPreflightMinimumFeasibleBudgetResponse(_PreflightResponseModel):
    schema_version: Literal["riftx.audit-preflight-minimum-feasible-budget/v1"]
    status: AuditPreflightBudgetStatus
    minimum_wall_seconds: int | None = Field(default=None, ge=1)
    maximum_wall_seconds: int | None = Field(default=None, ge=1)
    minimum_read_bytes: int | None = Field(default=None, ge=0)
    maximum_read_bytes: int | None = Field(default=None, ge=0)
    minimum_work_items: int | None = Field(default=None, ge=0)
    maximum_work_items: int | None = Field(default=None, ge=0)
    provenance_digest: _Digest
    reason_code: _Id | None = None

    @classmethod
    def from_domain(
        cls,
        value: AuditPreflightMinimumFeasibleBudget,
    ) -> AuditPreflightMinimumFeasibleBudgetResponse:
        return cls.model_validate(value, from_attributes=True)


class AuditPreflightResultResponse(_PreflightResponseModel):
    schema_version: Literal["riftx.audit-preflight-result/v1"]
    result_digest: _Digest
    preflight_job_id: _Id
    request_digest: _Digest
    source_node_id: Literal["local"]
    source_root_identity_digest: _Digest
    repository_identity_digest: _Digest
    content_identity_digest: _Digest
    backend_id: _Id
    image_digest: _Digest
    policy_digest: _Digest
    capsule_prepare_proof_digest: _Digest
    target_kind: SourceTargetKind
    revision: str = Field(min_length=1, max_length=1_024)
    base_revision: str | None = Field(default=None, min_length=1, max_length=1_024)
    mode: AuditMode
    include_untracked: bool
    head_revision: _GitObjectId | None = None
    resolved_revision: _GitObjectId | None = None
    resolved_base_revision: _GitObjectId | None = None
    merge_base_revision: _GitObjectId | None = None
    dirty: bool
    staged: bool
    unstaged: bool
    untracked: bool
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    max_file_bytes: int = Field(ge=0)
    language_estimates: tuple[AuditPreflightLanguageEstimateResponse, ...]
    capability_matrix: AuditPreflightCapabilityMatrixResponse
    capability_warnings: tuple[_Id, ...]
    blocking_errors: tuple[_Id, ...]
    minimum_feasible_budget: AuditPreflightMinimumFeasibleBudgetResponse
    canonical_empty_context_id: Literal["riftx.audit-empty-security-context/v1"]
    canonical_empty_context_digest: _Digest
    completed_at: AwareDatetime
    expires_at: AwareDatetime

    @classmethod
    def from_domain(cls, value: AuditPreflightResult) -> AuditPreflightResultResponse:
        return cls(
            schema_version=value.schema_version,
            result_digest=value.result_digest,
            preflight_job_id=value.preflight_job_id,
            request_digest=value.request_digest,
            source_node_id=value.source_node_id,
            source_root_identity_digest=value.source_root_identity_digest,
            repository_identity_digest=value.repository_identity_digest,
            content_identity_digest=value.content_identity_digest,
            backend_id=value.backend_id,
            image_digest=value.image_digest,
            policy_digest=value.policy_digest,
            capsule_prepare_proof_digest=value.capsule_prepare_proof_digest,
            target_kind=value.target_kind,
            revision=value.revision,
            base_revision=value.base_revision,
            mode=value.mode,
            include_untracked=value.include_untracked,
            head_revision=value.head_revision,
            resolved_revision=value.resolved_revision,
            resolved_base_revision=value.resolved_base_revision,
            merge_base_revision=value.merge_base_revision,
            dirty=value.dirty,
            staged=value.staged,
            unstaged=value.unstaged,
            untracked=value.untracked,
            file_count=value.file_count,
            total_bytes=value.total_bytes,
            max_file_bytes=value.max_file_bytes,
            language_estimates=tuple(
                AuditPreflightLanguageEstimateResponse.from_domain(item)
                for item in value.language_estimates
            ),
            capability_matrix=AuditPreflightCapabilityMatrixResponse.from_domain(
                value.capability_matrix
            ),
            capability_warnings=value.capability_warnings,
            blocking_errors=value.blocking_errors,
            minimum_feasible_budget=(
                AuditPreflightMinimumFeasibleBudgetResponse.from_domain(
                    value.minimum_feasible_budget
                )
            ),
            canonical_empty_context_id=value.canonical_empty_context_id,
            canonical_empty_context_digest=value.canonical_empty_context_digest,
            completed_at=value.completed_at,
            expires_at=value.expires_at,
        )


class AuditPreflightJobResponse(_PreflightResponseModel):
    schema_version: Literal["riftx.audit-preflight-job-projection/v1"] = (
        "riftx.audit-preflight-job-projection/v1"
    )
    job_id: _Id
    status: AuditPreflightJobStatus
    state_version: int = Field(ge=1)
    request_digest: _Digest
    source_node_id: Literal["local"]
    backend_id: _Id
    image_digest: _Digest
    policy_digest: _Digest
    canonical_empty_context_id: Literal["riftx.audit-empty-security-context/v1"] = (
        AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    )
    canonical_empty_context_digest: Literal[
        "24aa542ca6f995e7fc43c5c97fcfd00bee939dc1bd51797d6f895b893d008ed7"
    ] = "24aa542ca6f995e7fc43c5c97fcfd00bee939dc1bd51797d6f895b893d008ed7"
    safe_error_code: _Id | None = None
    result: AuditPreflightResultResponse | None = None
    expires_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    @classmethod
    def from_job(cls, job: AuditPreflightJob, **updates: object) -> Self:
        result = (
            AuditPreflightResult.model_validate_json(job.result_json)
            if job.result_json is not None
            else None
        )
        payload: dict[str, object] = {
            "job_id": job.job_id,
            "status": job.status,
            "state_version": job.state_version,
            "request_digest": job.request_digest,
            "source_node_id": job.source_node_id,
            "backend_id": job.backend_id,
            "image_digest": job.image_digest,
            "policy_digest": job.policy_digest,
            "canonical_empty_context_id": job.canonical_empty_context_id,
            "canonical_empty_context_digest": job.canonical_empty_context_digest,
            "safe_error_code": job.safe_error_code,
            "result": (
                AuditPreflightResultResponse.from_domain(result) if result is not None else None
            ),
            "expires_at": job.expires_at,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }
        payload.update(updates)
        return cls.model_validate(payload)


class AuditPreflightCreateResponse(AuditPreflightJobResponse):
    created: bool
    replayed: bool

    @classmethod
    def from_result(
        cls,
        result: AuditPreflightCreationResult,
    ) -> AuditPreflightCreateResponse:
        return cls.from_job(
            result.job,
            created=result.created,
            replayed=result.replayed,
        )


__all__ = [
    "AuditPreflightCreateResponse",
    "AuditPreflightJobResponse",
    "AuditPreflightResultResponse",
    "CreateAuditPreflightRequest",
]
