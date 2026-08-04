from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from riftx.domain.audit import AnalysisProfile, AuditMode, SourceTargetKind, ValidationPolicy
from riftx.domain.audit_contract_v2 import (
    AUDIT_CONTRACT_V2_MISSING_CAPABILITIES,
    AUDIT_CONTRACT_V2_SCHEMA_VERSION,
    AUDIT_CONTRACT_V2_STAGE,
    AUDIT_MODEL_EGRESS_V2_SCHEMA_VERSION,
    AuditContractRecordV2,
    AuditContractV2,
    AuditDraftBudgetV2,
    AuditSourceScopeV2,
    ModelDataEgressPolicyV2,
    audit_contract_v2_canonical_payload,
    audit_contract_v2_digest,
)
from riftx.domain.audit_preflight import (
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST,
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID,
    AuditPreflightBudgetStatus,
    AuditPreflightCapabilityFact,
    AuditPreflightCapabilityMatrix,
    AuditPreflightCapabilityStatus,
    AuditPreflightMinimumFeasibleBudget,
    AuditPreflightResult,
    AuditPreflightSecurityContext,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightTarget,
    PreflightRequest,
)
from riftx.domain.audit_preflight_plan import (
    AuditPreflightPlan,
    AuditPreflightPlanScope,
    AuditPreflightPlanTarget,
    AuditPreflightTokenVerifier,
    audit_preflight_minimum_budget_digest,
)

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
HEAD_REVISION = "a" * 40
RESOLVED_REVISION = "b" * 40


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _request(**updates: Any) -> PreflightRequest:
    payload: dict[str, Any] = {
        "client_request_id": "123e4567-e89b-42d3-a456-426614174000",
        "repository_path": "/srv/source/repository",
        "source_execution_target": AuditPreflightSourceExecutionTarget(
            source_ingest_backend="linux_container"
        ),
        "target": AuditPreflightTarget(
            kind=SourceTargetKind.WORKING_TREE,
            revision="HEAD",
        ),
        "include_paths": ("src",),
        "exclude_paths": ("src/generated", "vendor"),
        "security_context": AuditPreflightSecurityContext(),
        "mode": AuditMode.STANDARD,
    }
    payload.update(updates)
    return PreflightRequest(**payload)


def _capability_matrix() -> AuditPreflightCapabilityMatrix:
    return AuditPreflightCapabilityMatrix(
        entries=(
            AuditPreflightCapabilityFact(
                capability_id="detector_inventory",
                status=AuditPreflightCapabilityStatus.UNAVAILABLE,
                reason_code="audit_inventory_unavailable",
            ),
            AuditPreflightCapabilityFact(
                capability_id="git_metadata",
                status=AuditPreflightCapabilityStatus.AVAILABLE,
                component_version="2.44.0",
                component_digest=_digest("git-component"),
                proof_digest=_digest("git-proof"),
            ),
            AuditPreflightCapabilityFact(
                capability_id="source_ingest",
                status=AuditPreflightCapabilityStatus.AVAILABLE,
                component_version="riftx.audit-source-ingest-docker/v1",
                component_digest=_digest("source-ingest-component"),
                proof_digest=_digest("source-ingest-execution-proof"),
            ),
        )
    )


def _minimum_budget(
    *,
    status: AuditPreflightBudgetStatus = AuditPreflightBudgetStatus.UNAVAILABLE,
) -> AuditPreflightMinimumFeasibleBudget:
    if status is AuditPreflightBudgetStatus.ESTIMATED:
        return AuditPreflightMinimumFeasibleBudget(
            status=status,
            minimum_wall_seconds=30,
            maximum_wall_seconds=120,
            minimum_read_bytes=1_024,
            maximum_read_bytes=4_096,
            minimum_work_items=1,
            maximum_work_items=4,
            provenance_digest=_digest("minimum-budget"),
        )
    return AuditPreflightMinimumFeasibleBudget(
        status=status,
        provenance_digest=_digest("minimum-budget"),
        reason_code=(
            "audit_preflight_blocked"
            if status is AuditPreflightBudgetStatus.BLOCKING
            else "audit_inventory_unavailable"
        ),
    )


def _result(
    request: PreflightRequest,
    **updates: Any,
) -> AuditPreflightResult:
    payload: dict[str, Any] = {
        "preflight_job_id": "preflight-job-1",
        "request_digest": request.request_digest,
        "effect_owner_digest": _digest("effect-owner"),
        "source_root_identity_digest": _digest("source-root"),
        "repository_identity_digest": _digest("repository"),
        "content_identity_digest": _digest("content"),
        "backend_id": request.source_execution_target.source_ingest_backend,
        "image_digest": _digest("source-image"),
        "policy_digest": _digest("source-policy"),
        "capsule_prepare_proof_digest": _digest("capsule-prepare"),
        "target_kind": request.target.kind,
        "revision": request.target.revision,
        "base_revision": request.target.base_revision,
        "mode": request.mode,
        "include_untracked": request.target.include_untracked,
        "head_revision": HEAD_REVISION,
        "resolved_revision": RESOLVED_REVISION,
        "dirty": False,
        "staged": False,
        "unstaged": False,
        "untracked": False,
        "file_count": 10,
        "total_bytes": 4_096,
        "max_file_bytes": 1_024,
        "capability_matrix": _capability_matrix(),
        "minimum_feasible_budget": _minimum_budget(),
        "completed_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    payload.update(updates)
    return AuditPreflightResult(**payload)


def _draft_budget(**updates: Any) -> AuditDraftBudgetV2:
    payload: dict[str, Any] = {
        "max_wall_seconds": 1_800,
        "max_detector_jobs": 64,
        "max_worker_jobs": 8,
        "max_epochs": 1,
        "max_model_calls": 0,
        "max_input_tokens": 0,
        "max_output_tokens": 0,
        "max_read_bytes": 16_777_216,
        "max_candidates": 100,
        "max_signals": 1_000,
        "max_dynamic_validations": 0,
        "max_artifact_output_bytes": 16_777_216,
    }
    payload.update(updates)
    return AuditDraftBudgetV2(**payload)


def _contract(
    *,
    plan: AuditPreflightPlan | None = None,
) -> AuditContractV2:
    frozen_plan = plan
    if frozen_plan is None:
        available = _plan()
        frozen_plan = available.reserve(
            audit_id="audit-1",
            client_request_id="123e4567-e89b-42d3-a456-426614174001",
            at=available.created_at + timedelta(minutes=1),
        )
    return AuditContractV2.from_preflight_plan(
        audit_id="audit-1",
        project_id="project-1",
        plan=frozen_plan,
        budget=_draft_budget(),
    )


def _plan(
    *,
    request: PreflightRequest | None = None,
    result: AuditPreflightResult | None = None,
    capability_matrix: AuditPreflightCapabilityMatrix | None = None,
    minimum_feasible_budget: AuditPreflightMinimumFeasibleBudget | None = None,
) -> AuditPreflightPlan:
    request = request or _request()
    result = result or _result(request)
    frozen_capabilities = capability_matrix or result.capability_matrix
    frozen_minimum_budget = minimum_feasible_budget or result.minimum_feasible_budget
    target = AuditPreflightPlanTarget(
        repository_path=request.repository_path,
        source_ingest_backend_id=result.backend_id,
        kind=result.target_kind,
        revision=result.revision,
        mode=result.mode,
        include_untracked=result.include_untracked,
        head_revision=result.head_revision,
        resolved_revision=result.resolved_revision,
    )
    scope = AuditPreflightPlanScope(
        include_paths=request.include_paths,
        exclude_paths=request.exclude_paths,
    )
    return AuditPreflightPlan(
        plan_id="plan-1",
        preflight_job_id=result.preflight_job_id,
        preflight_client_request_id=request.client_request_id,
        operator_principal_id="operator-1",
        authorization_scope_digest=_digest("authorization"),
        request_digest=request.request_digest,
        result_digest=result.result_digest,
        effect_owner_digest=result.effect_owner_digest,
        source_root_identity_digest=result.source_root_identity_digest,
        repository_identity_digest=result.repository_identity_digest,
        content_identity_digest=result.content_identity_digest,
        backend_id=result.backend_id,
        image_digest=result.image_digest,
        policy_digest=result.policy_digest,
        capsule_prepare_proof_digest=result.capsule_prepare_proof_digest,
        target=target,
        target_digest=target.target_digest,
        scope=scope,
        scope_digest=scope.scope_digest,
        capability_matrix=frozen_capabilities,
        capability_matrix_digest=frozen_capabilities.matrix_digest,
        minimum_feasible_budget=frozen_minimum_budget,
        minimum_feasible_budget_digest=audit_preflight_minimum_budget_digest(
            frozen_minimum_budget
        ),
        preflight_completed_at=result.completed_at,
        created_at=result.completed_at + timedelta(minutes=1),
        expires_at=result.completed_at + timedelta(minutes=20),
        token_verifier=AuditPreflightTokenVerifier(
            key_id="preflight-key-1",
            nonce="A" * 43,
            token_hash=_digest("opaque-token"),
        ),
        updated_at=result.completed_at + timedelta(minutes=1),
    )


def test_reserved_plan_builds_only_an_honest_preflight_bound_draft() -> None:
    request = _request()
    result = _result(request)
    available = _plan(request=request, result=result)
    reserved = available.reserve(
        audit_id="audit-1",
        client_request_id="123e4567-e89b-42d3-a456-426614174001",
        at=available.created_at + timedelta(minutes=1),
    )
    contract = _contract(plan=reserved)

    assert contract.schema_version == AUDIT_CONTRACT_V2_SCHEMA_VERSION
    assert contract.contract_stage == AUDIT_CONTRACT_V2_STAGE
    assert contract.start_eligible is False
    assert contract.preflight_plan_id == "plan-1"
    assert contract.preflight_plan_digest == reserved.plan_digest
    assert contract.operator_principal_id == reserved.operator_principal_id

    assert contract.mode is AuditMode.STANDARD
    assert contract.analysis_profile is AnalysisProfile.DETERMINISTIC
    assert contract.validation_policy is ValidationPolicy.STATIC_ONLY
    assert contract.baseline_audit_id is None
    assert contract.detectors == contract.rulepacks == contract.parsers == ()
    assert contract.model_profile is contract.model_profile_digest is None
    assert contract.budget.max_model_calls == 0
    assert contract.budget.max_input_tokens == 0
    assert contract.budget.max_output_tokens == 0
    assert contract.budget.max_dynamic_validations == 0

    assert contract.source_target.repository_path == request.repository_path
    assert contract.source_target.revision == request.target.revision
    assert contract.source_target.head_revision == result.head_revision
    assert contract.source_target.resolved_revision == result.resolved_revision
    assert contract.source_scope.include_paths == request.include_paths
    assert contract.source_scope.exclude_paths == request.exclude_paths
    assert len(contract.source_scope.scope_digest) == 64
    assert contract.source_target_digest == contract.source_target.target_digest
    assert contract.source_scope_digest == contract.source_scope.scope_digest

    source = contract.source_binding
    assert source.preflight_job_id == result.preflight_job_id
    assert source.preflight_request_digest == request.request_digest
    assert source.preflight_result_digest == result.result_digest
    assert source.source_root_identity_digest == result.source_root_identity_digest
    assert source.repository_identity_digest == result.repository_identity_digest
    assert source.content_identity_digest == result.content_identity_digest
    assert source.source_ingest_backend_id == result.backend_id
    assert source.source_ingest_image_digest == result.image_digest
    assert source.source_ingest_policy_digest == result.policy_digest
    assert source.capsule_prepare_proof_digest == result.capsule_prepare_proof_digest

    facts = {entry.capability_id: entry for entry in result.capability_matrix.entries}
    assert source.source_ingest_execution_proof_digest == facts["source_ingest"].proof_digest
    assert source.source_ingest_backend_component_digest == (
        facts["source_ingest"].component_digest
    )
    assert source.git_component_version == facts["git_metadata"].component_version
    assert source.git_component_digest == facts["git_metadata"].component_digest
    assert source.git_proof_digest == facts["git_metadata"].proof_digest

    snapshot = contract.preflight_capability_snapshot
    assert snapshot.matrix_digest == result.capability_matrix.matrix_digest
    assert tuple(entry.capability_id for entry in snapshot.entries) == (
        "detector_inventory",
        "git_metadata",
        "source_ingest",
    )
    assert snapshot.entries[0].status is AuditPreflightCapabilityStatus.UNAVAILABLE
    assert snapshot.entries[0].component_digest is None
    assert snapshot.entries[0].proof_digest is None
    assert snapshot.entries[1].status is AuditPreflightCapabilityStatus.AVAILABLE
    assert snapshot.entries[2].status is AuditPreflightCapabilityStatus.AVAILABLE
    assert contract.minimum_feasible_budget == result.minimum_feasible_budget


def test_from_preflight_plan_uses_only_the_durable_authoritative_plan() -> None:
    available = _plan()
    plan = available.reserve(
        audit_id="audit-1",
        client_request_id="123e4567-e89b-42d3-a456-426614174001",
        at=available.created_at + timedelta(minutes=1),
    )

    contract = AuditContractV2.from_preflight_plan(
        audit_id="audit-1",
        project_id="project-1",
        plan=plan,
        budget=_draft_budget(),
    )

    assert contract.preflight_plan_id == plan.plan_id
    assert contract.preflight_plan_digest == plan.plan_digest
    assert contract.source_binding.preflight_job_id == plan.preflight_job_id
    assert contract.source_binding.preflight_request_digest == plan.request_digest
    assert contract.source_binding.preflight_result_digest == plan.result_digest
    assert contract.source_binding.source_root_identity_digest == (
        plan.source_root_identity_digest
    )
    assert contract.source_binding.repository_identity_digest == (
        plan.repository_identity_digest
    )
    assert contract.source_binding.content_identity_digest == plan.content_identity_digest
    assert contract.source_scope.include_paths == plan.scope.include_paths
    assert contract.source_scope.exclude_paths == plan.scope.exclude_paths
    assert contract.minimum_feasible_budget == plan.minimum_feasible_budget
    assert contract.security_context_binding.authorization_scope_digest == (
        plan.authorization_scope_digest
    )
    assert contract.security_context_binding.operator_principal_id == (
        plan.operator_principal_id
    )


def test_from_preflight_plan_requires_same_audit_reservation() -> None:
    plan = _plan()
    reserved = plan.reserve(
        audit_id="audit-1",
        client_request_id="123e4567-e89b-42d3-a456-426614174001",
        at=plan.created_at + timedelta(minutes=1),
    )

    contract = AuditContractV2.from_preflight_plan(
        audit_id="audit-1",
        project_id="project-1",
        plan=reserved,
        budget=_draft_budget(),
    )
    assert contract.preflight_plan_digest == plan.plan_digest

    with pytest.raises(ValueError, match="requires a reserved Preflight plan"):
        AuditContractV2.from_preflight_plan(
            audit_id="audit-1",
            project_id="project-1",
            plan=plan,
            budget=_draft_budget(),
        )

    with pytest.raises(ValueError, match="reserved for a different Audit"):
        AuditContractV2.from_preflight_plan(
            audit_id="audit-2",
            project_id="project-1",
            plan=reserved,
            budget=_draft_budget(),
        )

    revoked = plan.revoke(
        reason_code="audit_preflight_plan_revoked",
        at=plan.created_at + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="requires a reserved Preflight plan"):
        AuditContractV2.from_preflight_plan(
            audit_id="audit-1",
            project_id="project-1",
            plan=revoked,
            budget=_draft_budget(),
        )


def test_draft_explicitly_leaves_every_later_stage_proof_empty_and_blocked() -> None:
    contract = _contract()
    selection = contract.execution_selection
    absent_fields = (
        "analysis_node_id",
        "analysis_backend_id",
        "analysis_backend_component_digest",
        "analysis_backend_image_digest",
        "analysis_backend_policy_digest",
        "analysis_backend_prepare_proof_digest",
        "snapshot_store_component_digest",
        "snapshot_store_proof_digest",
        "snapshot_materializer_component_digest",
        "snapshot_materializer_policy_digest",
        "snapshot_materializer_proof_digest",
        "snapshot_mount_policy_digest",
        "snapshot_mount_proof_digest",
        "eligible_candidates_digest",
        "selection_policy_version",
    )

    assert all(getattr(selection, field_name) is None for field_name in absent_fields)
    assert contract.execution_readiness.status == "blocked"
    assert (
        contract.execution_readiness.missing_capabilities
        == AUDIT_CONTRACT_V2_MISSING_CAPABILITIES
    )
    assert contract.execution_readiness.reason_code == "audit_contract_not_start_ready"


def test_security_context_and_model_egress_are_fixed_to_inactive_empty_binding() -> None:
    contract = _contract()
    policy = contract.model_data_egress_policy
    binding = contract.security_context_binding

    assert contract.security_context_bundle_id == AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    assert (
        contract.security_context_bundle_digest
        == AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    )
    assert binding.audit_id == contract.audit_id
    assert binding.preflight_plan_id == contract.preflight_plan_id
    assert binding.preflight_plan_digest == contract.preflight_plan_digest
    assert binding.authorization_scope_digest == _digest("authorization")
    assert binding.security_context_bundle_id == AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    assert (
        binding.security_context_bundle_digest
        == AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST
    )

    assert policy.schema_version == AUDIT_MODEL_EGRESS_V2_SCHEMA_VERSION
    assert policy.disposition == "inactive"
    assert policy.mode == "local_only"
    assert policy.security_context_bundle_digest == contract.security_context_bundle_digest
    assert policy.max_bytes_per_call == policy.max_bytes_per_audit == 0
    assert policy.model_profile is policy.model_profile_digest is None
    assert policy.provider_display_name is None
    assert policy.allowed_remote_origins == ()
    assert policy.retention_training_disclosure is None
    assert policy.redaction_policy_digest is None
    assert policy.operator_consent_at is None
    assert len(policy.policy_digest) == 64
    assert policy.policy_digest != _digest(policy.canonical_json())


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("disposition", "active"),
        ("mode", "remote_redacted"),
        ("model_profile", "primary"),
        ("provider_display_name", "provider"),
        ("allowed_scope_classes", ("source",)),
        ("allowed_remote_origins", ("https://example.com",)),
        ("max_bytes_per_call", 1),
        ("max_bytes_per_audit", 1),
        ("redaction_policy_digest", _digest("redaction")),
        ("operator_consent_at", NOW),
    ],
)
def test_inactive_model_egress_rejects_active_or_placeholder_metadata(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        ModelDataEgressPolicyV2.model_validate({field_name: value})


def test_model_egress_rejects_wrong_context_and_tampered_policy_digest() -> None:
    with pytest.raises(ValidationError, match="canonical empty context"):
        ModelDataEgressPolicyV2(
            security_context_bundle_digest=_digest("not-empty-context")
        )

    policy = ModelDataEgressPolicyV2()
    payload = policy.model_dump(mode="python")
    payload["policy_digest"] = _digest("tampered")
    with pytest.raises(ValidationError, match="policy digest"):
        ModelDataEgressPolicyV2.model_validate(payload)


def test_scope_digest_covers_canonical_include_and_exclude_paths() -> None:
    first = AuditSourceScopeV2(
        include_paths=("src",),
        exclude_paths=("src/generated", "vendor"),
    )
    changed = AuditSourceScopeV2(
        include_paths=("src", "tests"),
        exclude_paths=("src/generated", "vendor"),
    )

    assert first.scope_digest != changed.scope_digest

    payload = first.model_dump(mode="python")
    payload["include_paths"] = ("src", "tests")
    with pytest.raises(ValidationError, match="source scope digest"):
        AuditSourceScopeV2.model_validate(payload)


@pytest.mark.parametrize(
    ("include_paths", "exclude_paths"),
    [
        (("tests", "src"), ()),
        (("src", "src"), ()),
        (("../src",), ()),
        (("/src",), ()),
        (("src",), ("src",)),
    ],
)
def test_scope_rejects_noncanonical_or_overlapping_paths(
    include_paths: tuple[str, ...],
    exclude_paths: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        AuditSourceScopeV2(
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )


def test_reserved_plan_rejects_any_non_aud200_capability_claim() -> None:
    entries = _capability_matrix().entries
    invented = AuditPreflightCapabilityMatrix(
        entries=(
            AuditPreflightCapabilityFact(
                capability_id="analysis_backend",
                status=AuditPreflightCapabilityStatus.AVAILABLE,
                component_version="v1",
                component_digest=_digest("invented-analysis"),
                proof_digest=_digest("invented-analysis-proof"),
            ),
            *entries,
        )
    )
    available = _plan(capability_matrix=invented)
    reserved = available.reserve(
        audit_id="audit-1",
        client_request_id="123e4567-e89b-42d3-a456-426614174001",
        at=available.created_at + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="exact AUD-200 capability snapshot"):
        _contract(plan=reserved)


def test_reserved_plan_rejects_detector_inventory_proof_instead_of_faking_readiness() -> None:
    entries = _capability_matrix().entries
    invented = AuditPreflightCapabilityMatrix(
        entries=(
            AuditPreflightCapabilityFact(
                capability_id="detector_inventory",
                status=AuditPreflightCapabilityStatus.AVAILABLE,
                component_version="v1",
                component_digest=_digest("invented-detectors"),
                proof_digest=_digest("invented-detector-proof"),
            ),
            *entries[1:],
        )
    )
    available = _plan(capability_matrix=invented)
    reserved = available.reserve(
        audit_id="audit-1",
        client_request_id="123e4567-e89b-42d3-a456-426614174001",
        at=available.created_at + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="detector inventory must remain honestly unavailable"):
        _contract(plan=reserved)


def test_reserved_plan_rejects_blocking_minimum_budget() -> None:
    available = _plan(
        minimum_feasible_budget=_minimum_budget(
            status=AuditPreflightBudgetStatus.BLOCKING
        )
    )
    reserved = available.reserve(
        audit_id="audit-1",
        client_request_id="123e4567-e89b-42d3-a456-426614174001",
        at=available.created_at + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="blocking preflight result"):
        _contract(plan=reserved)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("contract_stage", "start_ready"),
        ("start_eligible", True),
        ("mode", AuditMode.DEEP),
        ("analysis_profile", AnalysisProfile.HYBRID),
        ("validation_policy", ValidationPolicy.ISOLATED_TEST),
        ("baseline_audit_id", "audit-0"),
        ("detectors", ("invented-detector",)),
        ("rulepacks", ("invented-rulepack",)),
        ("parsers", ("invented-parser",)),
        ("model_profile", "primary"),
    ],
)
def test_contract_rejects_every_unsupported_aud201_mode_or_component(
    field_name: str,
    value: object,
) -> None:
    contract = _contract()
    payload = contract.model_dump(mode="python")
    payload[field_name] = value

    with pytest.raises(ValidationError):
        AuditContractV2.model_validate(payload)


@pytest.mark.parametrize(
    "field_name",
    [
        "analysis_backend_component_digest",
        "analysis_backend_image_digest",
        "analysis_backend_policy_digest",
        "analysis_backend_prepare_proof_digest",
        "snapshot_store_component_digest",
        "snapshot_store_proof_digest",
        "snapshot_materializer_component_digest",
        "snapshot_materializer_policy_digest",
        "snapshot_materializer_proof_digest",
        "snapshot_mount_policy_digest",
        "snapshot_mount_proof_digest",
        "eligible_candidates_digest",
    ],
)
def test_contract_rejects_placeholder_or_invented_later_stage_digests(
    field_name: str,
) -> None:
    contract = _contract()
    payload = contract.model_dump(mode="python")
    payload["execution_selection"][field_name] = _digest(field_name)

    with pytest.raises(ValidationError):
        AuditContractV2.model_validate(payload)


def test_contract_cross_checks_source_capability_and_security_bindings() -> None:
    contract = _contract()

    source_payload = contract.model_dump(mode="python")
    source_payload["source_binding"]["git_proof_digest"] = _digest("other-git-proof")
    source_payload["source_binding"]["binding_digest"] = ""
    with pytest.raises(ValidationError, match="git_metadata snapshot"):
        AuditContractV2.model_validate(source_payload)

    security_payload = contract.model_dump(mode="python")
    security_payload["security_context_binding"]["audit_id"] = "audit-2"
    security_payload["security_context_binding"]["binding_digest"] = ""
    with pytest.raises(ValidationError, match="bind this Audit, principal, and Preflight plan"):
        AuditContractV2.model_validate(security_payload)

    principal_payload = contract.model_dump(mode="python")
    principal_payload["security_context_binding"]["operator_principal_id"] = "operator-2"
    principal_payload["security_context_binding"]["binding_digest"] = ""
    with pytest.raises(ValidationError, match="bind this Audit, principal, and Preflight plan"):
        AuditContractV2.model_validate(principal_payload)


def test_contract_canonical_round_trip_and_v2_digest_are_stable() -> None:
    contract = _contract()
    canonical = contract.canonical_json()
    restored = AuditContractV2.from_canonical_json(canonical)

    assert restored == contract
    assert audit_contract_v2_digest(restored) == contract.contract_digest
    assert audit_contract_v2_canonical_payload(restored) == restored.model_dump(mode="json")

    noncanonical = canonical.replace(":", ": ", 1)
    with pytest.raises(ValueError, match="not canonical"):
        AuditContractV2.from_canonical_json(noncanonical)


def test_contract_record_v2_preserves_canonical_bytes_and_redundant_bindings() -> None:
    contract = _contract()
    record = AuditContractRecordV2.from_contract(
        contract,
        contract_id="contract-1",
        created_at=NOW,
    )

    assert record.contract_id == "contract-1"
    assert record.audit_id == contract.audit_id
    assert record.schema_version == AUDIT_CONTRACT_V2_SCHEMA_VERSION
    assert record.canonical_contract_json == contract.canonical_json()
    assert record.contract_digest == contract.contract_digest
    assert record.source_target_digest == contract.source_target.target_digest
    assert record.source_node_id == contract.execution_selection.source_node_id
    assert record.source_ingest_backend_digest == (
        contract.execution_selection.source_ingest_backend_component_digest
    )
    assert record.source_prepare_proof_digest == (
        contract.execution_selection.source_prepare_proof_digest
    )
    assert record.preflight_plan_id == contract.preflight_plan_id
    assert record.preflight_plan_digest == contract.preflight_plan_digest
    assert record.security_context_bundle_id == contract.security_context_bundle_id
    assert (
        record.security_context_bundle_digest == contract.security_context_bundle_digest
    )
    assert record.contract() == contract


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("audit_id", "audit-2"),
        ("contract_digest", _digest("wrong-contract")),
        ("source_target_digest", _digest("wrong-target")),
        ("source_ingest_backend_digest", _digest("wrong-backend")),
        ("source_prepare_proof_digest", _digest("wrong-prepare")),
        ("preflight_plan_id", "plan-2"),
        ("preflight_plan_digest", _digest("wrong-plan")),
        ("security_context_bundle_digest", _digest("wrong-context")),
    ],
)
def test_contract_record_v2_rejects_redundant_field_drift(
    field_name: str,
    value: object,
) -> None:
    record = AuditContractRecordV2.from_contract(
        _contract(),
        contract_id="contract-1",
        created_at=NOW,
    )
    payload = record.model_dump(mode="python")
    payload[field_name] = value

    with pytest.raises(ValidationError):
        AuditContractRecordV2.model_validate(payload)


def test_contract_record_v2_rejects_noncanonical_or_non_v2_contract_bytes() -> None:
    record = AuditContractRecordV2.from_contract(
        _contract(),
        contract_id="contract-1",
        created_at=NOW,
    )
    payload = record.model_dump(mode="python")
    payload["canonical_contract_json"] = record.canonical_contract_json.replace(
        ":",
        ": ",
        1,
    )
    with pytest.raises(ValidationError, match="not canonical"):
        AuditContractRecordV2.model_validate(payload)

    payload["canonical_contract_json"] = '{"schema_version":"riftx.audit-contract/v1"}'
    with pytest.raises(ValidationError):
        AuditContractRecordV2.model_validate(payload)


def test_contract_record_v2_seal_is_validated_and_idempotent() -> None:
    record = AuditContractRecordV2.from_contract(
        _contract(),
        contract_id="contract-1",
        created_at=NOW,
    )
    sealed = record.seal(at=NOW + timedelta(minutes=1))

    assert record.sealed_at is None
    assert sealed.sealed_at == NOW + timedelta(minutes=1)
    assert sealed.seal(at=NOW + timedelta(minutes=2)) is sealed

    with pytest.raises(ValidationError, match="must not precede"):
        record.seal(at=NOW - timedelta(seconds=1))


def test_contract_is_frozen_forbids_unknown_fields_and_unvalidated_copy_updates() -> None:
    contract = _contract()

    with pytest.raises(ValidationError):
        contract.start_eligible = True  # type: ignore[misc]
    with pytest.raises(TypeError, match="unvalidated model_copy"):
        contract.model_copy(update={"start_eligible": True})

    payload = contract.model_dump(mode="python")
    payload["invented_proof"] = _digest("invented")
    with pytest.raises(ValidationError):
        AuditContractV2.model_validate(payload)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_model_calls", 1),
        ("max_input_tokens", 1),
        ("max_output_tokens", 1),
        ("max_dynamic_validations", 1),
    ],
)
def test_draft_budget_rejects_model_or_dynamic_execution_budget(
    field_name: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        _draft_budget(**{field_name: value})
