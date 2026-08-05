from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from riftx.domain.audit import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    AuditPreflightBudgetStatus,
    AuditPreflightCapabilityFact,
    AuditPreflightCapabilityMatrix,
    AuditPreflightCapabilityStatus,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightLanguageEstimate,
    AuditPreflightLeaseEnvelope,
    AuditPreflightMinimumFeasibleBudget,
    AuditPreflightResult,
    AuditPreflightSecurityContext,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightTarget,
    PreflightRequest,
)
from riftx.domain.audit_preflight_plan import (
    TOKEN_NONCE_BYTES,
    TOKEN_NONCE_WIRE_LENGTH,
    TOKEN_WIRE_LENGTH,
    AuditPreflightPlan,
    AuditPreflightPlanScope,
    AuditPreflightPlanStatus,
    AuditPreflightPlanTarget,
    AuditPreflightTokenCodec,
    audit_preflight_plan_can_transition,
    audit_preflight_plan_digest,
    audit_preflight_plan_scope_digest,
    audit_preflight_plan_target_digest,
    audit_preflight_token_hash,
    validate_audit_preflight_plan_transition,
)
from riftx.domain.errors import InvalidStateTransitionError
from riftx.domain.runner import RunnerPrincipal

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
JOB_EXPIRY = NOW + timedelta(hours=2)
RESULT_COMPLETED = NOW + timedelta(minutes=10)
RESULT_EXPIRY = NOW + timedelta(hours=1)
PLAN_CREATED = NOW + timedelta(minutes=11)
PLAN_EXPIRY = NOW + timedelta(minutes=40)
REVISION = "1" * 40


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


def _pending_job(request: PreflightRequest | None = None) -> AuditPreflightJob:
    request = request or _request()
    return AuditPreflightJob(
        job_id="preflight-job-1",
        client_request_id=request.client_request_id,
        operator_principal_id="operator-1",
        authorization_scope_digest=_digest("authorization"),
        request_digest=request.request_digest,
        restricted_request_json=request.canonical_json(),
        source_root_identity_digest=_digest("source-root"),
        backend_id=request.source_execution_target.source_ingest_backend,
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        expires_at=JOB_EXPIRY,
        created_at=NOW,
        updated_at=NOW,
    )


def _running_job(request: PreflightRequest | None = None) -> AuditPreflightJob:
    pending = _pending_job(request)
    lease = AuditPreflightLeaseEnvelope(
        owner=pending.effect_owner(),
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=7),
        lease_id="lease-1",
        lease_expires_at=NOW + timedelta(minutes=30),
        expected_state_version=2,
        output_contract_digest=_digest("output-contract"),
    )
    return AuditPreflightJob.model_validate(
        {
            **pending.model_dump(mode="python"),
            "status": AuditPreflightJobStatus.RUNNING,
            "state_version": 3,
            "attempt": 1,
            "lease_id": lease.lease_id,
            "lease_owner_instance_id": lease.runner_principal.instance_id,
            "lease_owner_epoch": lease.runner_principal.epoch,
            "lease_expires_at": lease.lease_expires_at,
            "lease_expected_state_version": lease.expected_state_version,
            "lease_output_contract_digest": lease.output_contract_digest,
            "lease_envelope_digest": lease.lease_envelope_digest,
            "capsule_id": "capsule-1",
            "capsule_prepare_proof_digest": _digest("prepare-proof"),
            "started_at": NOW + timedelta(minutes=1),
            "updated_at": NOW + timedelta(minutes=1),
        }
    )


def _capability_matrix() -> AuditPreflightCapabilityMatrix:
    return AuditPreflightCapabilityMatrix(
        entries=(
            AuditPreflightCapabilityFact(
                capability_id="detector_inventory",
                status=AuditPreflightCapabilityStatus.UNAVAILABLE,
                reason_code="audit_inventory_unavailable",
            ),
            AuditPreflightCapabilityFact(
                capability_id="source_ingest",
                status=AuditPreflightCapabilityStatus.AVAILABLE,
                component_version="v1",
                component_digest=_digest("source-ingest-component"),
                proof_digest=_digest("source-ingest-proof"),
            ),
        )
    )


def _result(job: AuditPreflightJob) -> AuditPreflightResult:
    return AuditPreflightResult(
        preflight_job_id=job.job_id,
        request_digest=job.request_digest,
        effect_owner_digest=job.effect_owner_digest,
        source_root_identity_digest=job.source_root_identity_digest,
        repository_identity_digest=_digest("repository"),
        content_identity_digest=_digest("content"),
        backend_id=job.backend_id,
        image_digest=job.image_digest,
        policy_digest=job.policy_digest,
        capsule_prepare_proof_digest=job.capsule_prepare_proof_digest or _digest("missing"),
        target_kind=SourceTargetKind.WORKING_TREE,
        revision="HEAD",
        mode=AuditMode.STANDARD,
        include_untracked=False,
        head_revision=REVISION,
        resolved_revision=REVISION,
        dirty=True,
        staged=False,
        unstaged=True,
        untracked=False,
        file_count=12,
        total_bytes=1_024,
        max_file_bytes=256,
        language_estimates=(
            AuditPreflightLanguageEstimate(
                language_id="python",
                file_count=8,
                total_bytes=800,
            ),
        ),
        capability_matrix=_capability_matrix(),
        minimum_feasible_budget=AuditPreflightMinimumFeasibleBudget(
            status=AuditPreflightBudgetStatus.UNAVAILABLE,
            provenance_digest=_digest("budget-provenance"),
            reason_code="audit_inventory_unavailable",
        ),
        completed_at=RESULT_COMPLETED,
        expires_at=RESULT_EXPIRY,
    )


def _succeeded_job(
    request: PreflightRequest | None = None,
) -> tuple[PreflightRequest, AuditPreflightJob, AuditPreflightResult]:
    request = request or _request()
    running = _running_job(request)
    result = _result(running)
    succeeded = AuditPreflightJob.model_validate(
        {
            **running.model_dump(mode="python"),
            "status": AuditPreflightJobStatus.SUCCEEDED,
            "state_version": 4,
            "result_schema_version": result.schema_version,
            "result_json": result.canonical_json(),
            "result_digest": result.result_digest,
            "exit_receipt_digest": _digest("exit-receipt"),
            "finished_at": result.completed_at,
            "updated_at": result.completed_at,
        }
    )
    return request, succeeded, result


def _codec(
    *,
    key_id: str = "preflight-key-1",
    key_byte: bytes = b"K",
    nonce_byte: bytes = b"N",
) -> AuditPreflightTokenCodec:
    return AuditPreflightTokenCodec(
        key_id=key_id,
        key=key_byte * 32,
        nonce_factory=lambda size: nonce_byte * size,
    )


def _issue_plan(
    *,
    codec: AuditPreflightTokenCodec | None = None,
    plan_id: str = "preflight-plan-1",
) -> Any:
    request, job, result = _succeeded_job()
    return AuditPreflightPlan.from_succeeded(
        job=job,
        result=result,
        restricted_request=request,
        token_codec=codec or _codec(),
        plan_id=plan_id,
        created_at=PLAN_CREATED,
        expires_at=PLAN_EXPIRY,
    )


def test_plan_is_issued_only_from_the_exact_succeeded_job_result_and_request() -> None:
    issue = _issue_plan()
    plan = issue.plan

    assert plan.status is AuditPreflightPlanStatus.AVAILABLE
    assert plan.state_version == 1
    assert plan.target.repository_path == "/srv/source/repository"
    assert plan.target_digest == audit_preflight_plan_target_digest(plan.target)
    assert plan.scope_digest == audit_preflight_plan_scope_digest(plan.scope)
    assert plan.plan_digest == audit_preflight_plan_digest(plan)
    assert plan.content_identity_digest == _digest("content")
    assert len(plan.token_verifier.nonce) == TOKEN_NONCE_WIRE_LENGTH
    assert len(issue.token) == TOKEN_WIRE_LENGTH
    assert issue.token not in plan.canonical_json()
    assert "preflight_token" not in type(plan).model_fields

    request, succeeded, result = _succeeded_job()
    running = _running_job(request)
    with pytest.raises(ValueError, match="succeeded Job"):
        AuditPreflightPlan.from_succeeded(
            job=running,
            result=result,
            restricted_request=request,
            token_codec=_codec(),
            created_at=PLAN_CREATED,
        )
    with pytest.raises(ValueError, match="restricted request"):
        AuditPreflightPlan.from_succeeded(
            job=succeeded,
            result=result,
            restricted_request=_request(include_paths=("app",)),
            token_codec=_codec(),
            created_at=PLAN_CREATED,
        )


def test_target_and_scope_digests_are_domain_separated_and_sensitive() -> None:
    plan = _issue_plan().plan
    changed_target = AuditPreflightPlanTarget.model_validate(
        {
            **plan.target.model_dump(mode="python"),
            "repository_path": "/srv/source/other-repository",
            "target_digest": "",
        }
    )
    changed_scope = AuditPreflightPlanScope(
        include_paths=("app",),
        exclude_paths=plan.scope.exclude_paths,
    )

    assert changed_target.target_digest != plan.target_digest
    assert changed_scope.scope_digest != plan.scope_digest
    assert plan.target_digest != plan.scope_digest

    with pytest.raises(ValidationError, match="target digest"):
        AuditPreflightPlanTarget.model_validate(
            {**plan.target.model_dump(mode="python"), "target_digest": _digest("forged")}
        )
    with pytest.raises(ValidationError, match="scope digest"):
        AuditPreflightPlanScope.model_validate(
            {**plan.scope.model_dump(mode="python"), "scope_digest": _digest("forged")}
        )


def test_plan_digest_excludes_token_verifier_and_all_mutable_state() -> None:
    first = _issue_plan(codec=_codec(key_id="key-a", nonce_byte=b"A"))
    second = _issue_plan(codec=_codec(key_id="key-b", key_byte=b"B", nonce_byte=b"B"))

    assert first.plan.plan_digest == second.plan.plan_digest
    assert first.plan.token_verifier != second.plan.token_verifier
    assert first.token != second.token

    reserved = first.plan.reserve(
        audit_id="audit-1",
        client_request_id="223e4567-e89b-42d3-a456-426614174000",
        at=PLAN_CREATED + timedelta(minutes=1),
    )
    consumed = reserved.consume(
        audit_id="audit-1",
        start_request_id="323e4567-e89b-42d3-a456-426614174000",
        at=PLAN_CREATED + timedelta(minutes=2),
    )

    assert reserved.plan_digest == first.plan.plan_digest
    assert consumed.plan_digest == first.plan.plan_digest
    assert reserved.identity_json() == first.plan.identity_json()
    assert consumed.identity_json() == first.plan.identity_json()


def test_token_codec_uses_fixed_canonical_wire_and_persisted_verifier_only() -> None:
    codec = _codec()
    issue = _issue_plan(codec=codec)
    token = issue.token
    plan = issue.plan

    assert len(token) == TOKEN_WIRE_LENGTH
    assert "=" not in token
    assert len(plan.token_verifier.nonce) == TOKEN_NONCE_WIRE_LENGTH
    assert audit_preflight_token_hash(token) == plan.token_verifier.token_hash
    assert plan.verify_token(token, codec=codec)
    assert codec.token_for(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        verifier=plan.token_verifier,
    ) == token

    replacement = "A" if token[-1] != "A" else "B"
    assert not plan.verify_token(token[:-1] + replacement, codec=codec)
    assert not plan.verify_token(token + "=", codec=codec)
    assert not plan.verify_token(token, codec=_codec(key_byte=b"Z"))
    assert token not in plan.model_dump_json()

    with pytest.raises(ValueError, match="canonical base64url"):
        audit_preflight_token_hash(token + "=")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        AuditPreflightTokenCodec(key_id="key", key=b"short")
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        AuditPreflightTokenCodec(
            key_id="key",
            key=b"K" * 32,
            nonce_factory=lambda _size: b"short",
        ).issue(plan_id=plan.plan_id, plan_digest=plan.plan_digest)


def test_reserve_consume_and_revoke_enforce_closed_validated_transitions() -> None:
    available = _issue_plan().plan
    reserved_at = PLAN_CREATED + timedelta(minutes=1)
    consumed_at = reserved_at + timedelta(minutes=1)
    reserved = available.reserve(
        audit_id="audit-1",
        client_request_id="223e4567-e89b-42d3-a456-426614174000",
        at=reserved_at,
    )
    consumed = reserved.consume(
        audit_id="audit-1",
        start_request_id="323e4567-e89b-42d3-a456-426614174000",
        at=consumed_at,
    )

    assert reserved.status is AuditPreflightPlanStatus.RESERVED
    assert reserved.state_version == 2
    assert consumed.status is AuditPreflightPlanStatus.CONSUMED
    assert consumed.state_version == 3
    assert consumed.reserved_audit_id == consumed.consumed_audit_id == "audit-1"

    with pytest.raises(ValueError, match="different Audit"):
        reserved.consume(
            audit_id="audit-2",
            start_request_id="423e4567-e89b-42d3-a456-426614174000",
            at=consumed_at,
        )
    with pytest.raises(InvalidStateTransitionError):
        available.consume(
            audit_id="audit-1",
            start_request_id="423e4567-e89b-42d3-a456-426614174000",
            at=consumed_at,
        )
    with pytest.raises(InvalidStateTransitionError):
        consumed.revoke(reason_code="audit_snapshot_changed", at=consumed_at)

    revoked_available = available.revoke(
        reason_code="audit_policy_changed",
        at=reserved_at,
    )
    revoked_reserved = reserved.revoke(
        reason_code="audit_snapshot_changed",
        at=consumed_at,
    )
    assert revoked_available.state_version == 2
    assert revoked_available.reserved_audit_id is None
    assert revoked_reserved.state_version == 3
    assert revoked_reserved.reserved_audit_id == "audit-1"


def test_transition_graph_and_expiry_guards_are_fail_closed() -> None:
    legal = {
        (AuditPreflightPlanStatus.AVAILABLE, AuditPreflightPlanStatus.RESERVED),
        (AuditPreflightPlanStatus.AVAILABLE, AuditPreflightPlanStatus.REVOKED),
        (AuditPreflightPlanStatus.RESERVED, AuditPreflightPlanStatus.CONSUMED),
        (AuditPreflightPlanStatus.RESERVED, AuditPreflightPlanStatus.REVOKED),
    }
    all_pairs = {
        (current, target)
        for current in AuditPreflightPlanStatus
        for target in AuditPreflightPlanStatus
    }
    assert {pair for pair in all_pairs if audit_preflight_plan_can_transition(*pair)} == legal
    for current, target in all_pairs - legal:
        with pytest.raises(InvalidStateTransitionError):
            validate_audit_preflight_plan_transition(current, target)

    plan = _issue_plan().plan
    with pytest.raises(ValueError, match="outside its lifetime"):
        plan.reserve(
            audit_id="audit-1",
            client_request_id="223e4567-e89b-42d3-a456-426614174000",
            at=plan.expires_at,
        )
    reserved = plan.reserve(
        audit_id="audit-1",
        client_request_id="223e4567-e89b-42d3-a456-426614174000",
        at=PLAN_CREATED + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="outside its reserved lifetime"):
        reserved.consume(
            audit_id="audit-1",
            start_request_id="323e4567-e89b-42d3-a456-426614174000",
            at=reserved.expires_at,
        )


def test_plan_rejects_forged_digest_and_partial_lifecycle_shapes() -> None:
    plan = _issue_plan().plan
    with pytest.raises(ValidationError, match="Plan digest"):
        AuditPreflightPlan.model_validate(
            {
                **plan.model_dump(mode="python"),
                "repository_identity_digest": _digest("forged-repository"),
            }
        )
    with pytest.raises(ValidationError, match="reservation facts"):
        AuditPreflightPlan.model_validate(
            {
                **plan.model_dump(mode="python"),
                "status": AuditPreflightPlanStatus.RESERVED,
                "state_version": 2,
                "reserved_audit_id": "audit-1",
                "updated_at": PLAN_CREATED + timedelta(minutes=1),
            }
        )
    with pytest.raises(TypeError, match="forbid"):
        plan.model_copy(update={"status": AuditPreflightPlanStatus.REVOKED})


def test_nonce_factory_receives_the_required_32_byte_size() -> None:
    observed: list[int] = []

    def nonce_factory(size: int) -> bytes:
        observed.append(size)
        return b"Q" * size

    codec = AuditPreflightTokenCodec(
        key_id="key-1",
        key=b"K" * 32,
        nonce_factory=nonce_factory,
    )
    _issue_plan(codec=codec)

    assert observed == [TOKEN_NONCE_BYTES]
