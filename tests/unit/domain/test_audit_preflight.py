from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from riftx.domain.audit import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST,
    AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID,
    AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION,
    AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION,
    AuditPreflightBudgetStatus,
    AuditPreflightCapabilityFact,
    AuditPreflightCapabilityMatrix,
    AuditPreflightCapabilityStatus,
    AuditPreflightEffectOwner,
    AuditPreflightExitReceipt,
    AuditPreflightExitTerminalState,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightLanguageEstimate,
    AuditPreflightLeaseEnvelope,
    AuditPreflightMinimumFeasibleBudget,
    AuditPreflightObservedTerminalState,
    AuditPreflightResult,
    AuditPreflightSecurityContext,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightStopDisposition,
    AuditPreflightStopReceipt,
    AuditPreflightTarget,
    PreflightRequest,
    audit_preflight_can_transition,
    audit_preflight_effect_owner_digest,
    audit_preflight_exit_receipt_digest,
    audit_preflight_idempotency_drift_fields,
    audit_preflight_is_exact_replay,
    audit_preflight_is_exact_terminal_replay,
    audit_preflight_lease_envelope_digest,
    audit_preflight_result_digest,
    audit_preflight_stop_receipt_digest,
    audit_preflight_terminal_replay_drift_fields,
    validate_audit_preflight_transition,
)
from riftx.domain.errors import InvalidStateTransitionError
from riftx.domain.runner import RunnerPrincipal

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
JOB_EXPIRY = NOW + timedelta(hours=2)
REVISION = "1" * 40
BASE_REVISION = "2" * 40
MERGE_BASE = "3" * 40


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


def _pending_job(**updates: Any) -> AuditPreflightJob:
    request = updates.pop("request", _request())
    payload: dict[str, Any] = {
        "job_id": "preflight-job-1",
        "client_request_id": request.client_request_id,
        "operator_principal_id": "operator-1",
        "authorization_scope_digest": _digest("authorization"),
        "request_digest": request.request_digest,
        "restricted_request_json": request.canonical_json(),
        "source_root_identity_digest": _digest("source-root"),
        "backend_id": request.source_execution_target.source_ingest_backend,
        "image_digest": _digest("image"),
        "policy_digest": _digest("policy"),
        "expires_at": JOB_EXPIRY,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(updates)
    return AuditPreflightJob(**payload)


def _lease(
    job: AuditPreflightJob,
    *,
    expected_state_version: int = 2,
) -> AuditPreflightLeaseEnvelope:
    return AuditPreflightLeaseEnvelope(
        owner=job.effect_owner(),
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=7),
        lease_id="lease-1",
        lease_expires_at=NOW + timedelta(minutes=30),
        expected_state_version=expected_state_version,
        output_contract_digest=_digest("output-contract"),
    )


def _leased_job(
    *,
    status: AuditPreflightJobStatus = AuditPreflightJobStatus.CLAIMED,
    state_version: int = 2,
    running: bool = False,
    **updates: Any,
) -> AuditPreflightJob:
    pending = _pending_job()
    lease = _lease(pending, expected_state_version=state_version)
    changed_at = NOW + timedelta(minutes=1)
    payload = pending.model_dump(mode="python")
    payload.update(
        status=status,
        state_version=state_version,
        attempt=1,
        updated_at=changed_at,
        lease_id=lease.lease_id,
        lease_owner_instance_id=lease.runner_principal.instance_id,
        lease_owner_epoch=lease.runner_principal.epoch,
        lease_expires_at=lease.lease_expires_at,
        lease_expected_state_version=lease.expected_state_version,
        lease_output_contract_digest=lease.output_contract_digest,
        lease_envelope_digest=lease.lease_envelope_digest,
        capsule_id="capsule-1",
    )
    if running:
        payload.update(
            capsule_prepare_proof_digest=_digest("prepare-proof"),
            started_at=changed_at,
        )
    payload.update(updates)
    return AuditPreflightJob.model_validate(payload)


def _capability_matrix(
    *,
    blocking: bool = False,
) -> AuditPreflightCapabilityMatrix:
    entries = [
        AuditPreflightCapabilityFact(
            capability_id="source_ingest",
            status=AuditPreflightCapabilityStatus.AVAILABLE,
            component_version="v1",
            component_digest=_digest("source-ingest-component"),
            proof_digest=_digest("source-ingest-proof"),
        ),
    ]
    if blocking:
        entries.insert(
            0,
            AuditPreflightCapabilityFact(
                capability_id="detector_inventory",
                status=AuditPreflightCapabilityStatus.BLOCKING,
                reason_code="audit_inventory_unavailable",
            ),
        )
    else:
        entries.insert(
            0,
            AuditPreflightCapabilityFact(
                capability_id="detector_inventory",
                status=AuditPreflightCapabilityStatus.UNAVAILABLE,
                reason_code="audit_inventory_unavailable",
            ),
        )
    return AuditPreflightCapabilityMatrix(entries=tuple(entries))


def _budget(*, blocking: bool = False) -> AuditPreflightMinimumFeasibleBudget:
    return AuditPreflightMinimumFeasibleBudget(
        status=(
            AuditPreflightBudgetStatus.BLOCKING
            if blocking
            else AuditPreflightBudgetStatus.UNAVAILABLE
        ),
        provenance_digest=_digest("budget-provenance"),
        reason_code=("audit_preflight_blocked" if blocking else "audit_inventory_unavailable"),
    )


def _result(job: AuditPreflightJob, *, blocking: bool = False) -> AuditPreflightResult:
    completed_at = NOW + timedelta(minutes=10)
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
        capsule_prepare_proof_digest=_digest("prepare-proof"),
        target_kind=SourceTargetKind.WORKING_TREE,
        revision="HEAD",
        mode=AuditMode.STANDARD,
        include_untracked=False,
        head_revision=None if blocking else REVISION,
        resolved_revision=None if blocking else REVISION,
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
        capability_matrix=_capability_matrix(blocking=blocking),
        blocking_errors=("audit_preflight_blocked",) if blocking else (),
        minimum_feasible_budget=_budget(blocking=blocking),
        completed_at=completed_at,
        expires_at=completed_at + timedelta(minutes=30),
    )


@pytest.mark.parametrize(
    "repository_path",
    [
        "/",
        "/srv/repository",
        "C:/",
        "C:/source/repository",
        "//server/share/repository",
    ],
)
def test_request_accepts_only_canonical_absolute_wire_paths(repository_path: str) -> None:
    request = _request(repository_path=repository_path)

    assert request.repository_path == repository_path
    assert request.source_execution_target.node_id == "local"


@pytest.mark.parametrize(
    "repository_path",
    [
        "relative/repository",
        "~/repository",
        "/srv//repository",
        "/srv/../repository",
        "/srv/repository/",
        "c:/repository",
        "C:\\repository",
        "//SERVER/share/repository",
        "//server/share/repository/",
        "/srv/repo\x00evil",
        "/srv/répo",
    ],
)
def test_request_rejects_noncanonical_or_unsafe_repository_paths(
    repository_path: str,
) -> None:
    with pytest.raises(ValidationError, match="repository_path"):
        _request(repository_path=repository_path)


@pytest.mark.parametrize(
    "paths",
    [
        ("/absolute",),
        ("../escape",),
        ("src//module",),
        ("src\\module",),
        ("src/./module",),
        ("vendor", "src"),
        ("src", "src"),
    ],
)
def test_request_rejects_unsafe_or_noncanonical_repository_filters(
    paths: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError, match="repository-relative|canonical sorted|duplicates"):
        _request(include_paths=paths)


def test_request_target_mode_and_empty_context_are_fail_closed() -> None:
    diff = _request(
        mode=AuditMode.DIFF,
        target=AuditPreflightTarget(
            kind=SourceTargetKind.REVISION,
            revision="refs/heads/feature",
            base_revision="refs/heads/main",
        ),
    )
    assert diff.target.base_revision == "refs/heads/main"
    assert diff.security_context.context_id == AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_ID
    assert diff.security_context.context_digest == AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST

    with pytest.raises(ValidationError, match="diff mode requires"):
        _request(mode=AuditMode.DIFF)
    with pytest.raises(ValidationError, match="only diff mode"):
        _request(
            target=AuditPreflightTarget(
                kind=SourceTargetKind.WORKING_TREE,
                revision="HEAD",
                base_revision="main",
            )
        )
    with pytest.raises(ValidationError, match="cannot include untracked"):
        AuditPreflightTarget(
            kind=SourceTargetKind.REVISION,
            revision="HEAD",
            include_untracked=True,
        )
    with pytest.raises(ValidationError):
        AuditPreflightSecurityContext.model_validate(
            {
                "input_id": "context-1",
                "repository_paths": ["SECURITY.md"],
                "discover_defaults": True,
            }
        )


def test_request_digest_is_canonical_and_excludes_only_the_idempotency_key() -> None:
    first = _request()
    replay = _request(client_request_id="223e4567-e89b-42d3-a456-426614174000")
    drifted = _request(exclude_paths=("tests/fixtures", "vendor"))

    assert first.request_digest == replay.request_digest
    assert first.request_digest != drifted.request_digest
    assert PreflightRequest.model_validate_json(first.canonical_json()) == first
    assert first.canonical_json() != replay.canonical_json()
    assert first.request_identity_json() == replay.request_identity_json()


def test_owner_and_lease_envelopes_bind_every_authority_dimension() -> None:
    request = _request()
    owner = AuditPreflightEffectOwner.from_request(
        job_id="preflight-job-1",
        operator_principal_id="operator-1",
        authorization_scope_digest=_digest("authorization"),
        source_root_identity_digest=_digest("source-root"),
        request=request,
        backend_id="linux_container",
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        created_at=NOW,
        expires_at=JOB_EXPIRY,
    )
    lease = AuditPreflightLeaseEnvelope(
        owner=owner,
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=1),
        lease_id="lease-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        expected_state_version=2,
        output_contract_digest=_digest("output"),
    )

    assert owner.effect_owner_digest == audit_preflight_effect_owner_digest(owner)
    assert lease.lease_envelope_digest == audit_preflight_lease_envelope_digest(lease)
    assert "run_id" not in type(owner).model_fields
    assert "audit_id" not in type(owner).model_fields

    with pytest.raises(ValidationError, match="effect owner digest"):
        AuditPreflightEffectOwner.model_validate(
            {**owner.model_dump(mode="python"), "effect_owner_digest": _digest("forged")}
        )
    with pytest.raises(ValidationError, match="lease envelope digest"):
        AuditPreflightLeaseEnvelope.model_validate(
            {**lease.model_dump(mode="python"), "lease_envelope_digest": _digest("forged")}
        )


def test_capability_and_budget_facts_cannot_invent_proof_or_estimates() -> None:
    matrix = _capability_matrix()
    assert len(matrix.matrix_digest) == 64

    with pytest.raises(ValidationError, match="requires version"):
        AuditPreflightCapabilityFact(
            capability_id="source_ingest",
            status=AuditPreflightCapabilityStatus.AVAILABLE,
        )
    with pytest.raises(ValidationError, match="cannot claim implementation proof"):
        AuditPreflightCapabilityFact(
            capability_id="detector_inventory",
            status=AuditPreflightCapabilityStatus.UNAVAILABLE,
            component_version="v1",
            component_digest=_digest("component"),
            proof_digest=_digest("proof"),
            reason_code="not_available",
        )
    with pytest.raises(ValidationError, match="cannot contain invented estimates"):
        AuditPreflightMinimumFeasibleBudget(
            status=AuditPreflightBudgetStatus.UNAVAILABLE,
            minimum_wall_seconds=1,
            provenance_digest=_digest("provenance"),
            reason_code="not_available",
        )


def test_result_digest_is_stable_bounded_and_sensitive_to_facts() -> None:
    job = _pending_job()
    result = _result(job)
    changed = _result(job).model_dump(mode="python")
    changed["total_bytes"] = 1_025
    changed["result_digest"] = ""
    changed_result = AuditPreflightResult.model_validate(changed)

    assert result.result_digest == audit_preflight_result_digest(result)
    assert changed_result.result_digest != result.result_digest
    assert AuditPreflightResult.model_validate_json(result.canonical_json()) == result
    assert result.canonical_empty_context_digest == (AUDIT_PREFLIGHT_CANONICAL_EMPTY_CONTEXT_DIGEST)
    assert "repository_path" not in type(result).model_fields

    with pytest.raises(ValidationError, match="max_file_bytes"):
        AuditPreflightResult.model_validate(
            {
                **result.model_dump(mode="python"),
                "max_file_bytes": result.total_bytes + 1,
                "result_digest": "",
            }
        )
    with pytest.raises(ValidationError, match="safe codes"):
        AuditPreflightResult.model_validate(
            {
                **result.model_dump(mode="python"),
                "capability_warnings": ("z_warning", "a_warning"),
                "result_digest": "",
            }
        )
    with pytest.raises(ValidationError, match="empty-context digest"):
        AuditPreflightResult.model_validate(
            {
                **result.model_dump(mode="python"),
                "canonical_empty_context_digest": _digest("forged-empty-context"),
                "result_digest": "",
            }
        )


def test_blocked_result_requires_explicit_capability_and_budget_facts() -> None:
    result = _result(_pending_job(), blocking=True)

    assert result.blocking_errors == ("audit_preflight_blocked",)
    assert result.resolved_revision is None
    assert result.minimum_feasible_budget.status is AuditPreflightBudgetStatus.BLOCKING

    payload = result.model_dump(mode="python")
    payload.update(blocking_errors=(), result_digest="")
    with pytest.raises(ValidationError, match="blocking"):
        AuditPreflightResult.model_validate(payload)


def test_exit_receipt_binds_result_only_for_succeeded_or_rejected_exit() -> None:
    receipt = AuditPreflightExitReceipt(
        job_id="preflight-job-1",
        effect_owner_digest=_digest("owner"),
        lease_envelope_digest=_digest("lease"),
        capsule_id="capsule-1",
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=1),
        backend_id="linux_container",
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        process_identity_digest=_digest("process"),
        result_digest=_digest("result"),
        terminal_state=AuditPreflightExitTerminalState.SUCCEEDED,
        received_at=NOW,
    )

    assert receipt.receipt_digest == audit_preflight_exit_receipt_digest(receipt)
    with pytest.raises(ValidationError, match="requires exactly one result"):
        AuditPreflightExitReceipt.model_validate(
            {
                **receipt.model_dump(mode="python"),
                "terminal_state": AuditPreflightExitTerminalState.FAILED,
                "receipt_digest": "",
            }
        )


def test_stop_receipts_allow_only_affirmative_stopped_or_never_created_proof() -> None:
    stopped = AuditPreflightStopReceipt(
        job_id="preflight-job-1",
        effect_owner_digest=_digest("owner"),
        lease_envelope_digest=_digest("lease"),
        capsule_id="capsule-1",
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=1),
        backend_id="linux_container",
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        disposition=AuditPreflightStopDisposition.STOPPED,
        process_identity_digest=_digest("process"),
        observed_terminal_state=AuditPreflightObservedTerminalState.EXITED,
        received_at=NOW,
    )
    never_created = AuditPreflightStopReceipt(
        job_id="preflight-job-1",
        effect_owner_digest=_digest("owner"),
        lease_envelope_digest=_digest("lease"),
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=1),
        backend_id="linux_container",
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        disposition=AuditPreflightStopDisposition.NEVER_CREATED,
        never_created_proof_digest=_digest("never-created"),
        observed_terminal_state=AuditPreflightObservedTerminalState.NOT_CREATED,
        received_at=NOW,
    )

    assert stopped.receipt_digest == audit_preflight_stop_receipt_digest(stopped)
    assert never_created.receipt_digest == audit_preflight_stop_receipt_digest(never_created)
    assert stopped.receipt_digest != never_created.receipt_digest

    with pytest.raises(ValidationError, match="requires capsule"):
        AuditPreflightStopReceipt.model_validate(
            {
                **stopped.model_dump(mode="python"),
                "capsule_id": None,
                "receipt_digest": "",
            }
        )
    with pytest.raises(ValidationError, match="cannot claim a capsule"):
        AuditPreflightStopReceipt.model_validate(
            {
                **never_created.model_dump(mode="python"),
                "capsule_id": "capsule-forged",
                "receipt_digest": "",
            }
        )


def test_transition_graph_is_closed_and_terminal_states_cannot_leave() -> None:
    legal = {
        (AuditPreflightJobStatus.PENDING, AuditPreflightJobStatus.CLAIMED),
        (AuditPreflightJobStatus.PENDING, AuditPreflightJobStatus.CANCELLING),
        (AuditPreflightJobStatus.PENDING, AuditPreflightJobStatus.CANCELLED),
        (AuditPreflightJobStatus.CLAIMED, AuditPreflightJobStatus.RUNNING),
        (AuditPreflightJobStatus.CLAIMED, AuditPreflightJobStatus.CANCELLING),
        (AuditPreflightJobStatus.CLAIMED, AuditPreflightJobStatus.CANCELLED),
        (AuditPreflightJobStatus.CLAIMED, AuditPreflightJobStatus.OUTCOME_UNKNOWN),
        (AuditPreflightJobStatus.RUNNING, AuditPreflightJobStatus.SUCCEEDED),
        (AuditPreflightJobStatus.RUNNING, AuditPreflightJobStatus.REJECTED),
        (AuditPreflightJobStatus.RUNNING, AuditPreflightJobStatus.FAILED),
        (AuditPreflightJobStatus.RUNNING, AuditPreflightJobStatus.CANCELLING),
        (AuditPreflightJobStatus.RUNNING, AuditPreflightJobStatus.OUTCOME_UNKNOWN),
        (AuditPreflightJobStatus.OUTCOME_UNKNOWN, AuditPreflightJobStatus.RUNNING),
        (AuditPreflightJobStatus.OUTCOME_UNKNOWN, AuditPreflightJobStatus.SUCCEEDED),
        (AuditPreflightJobStatus.OUTCOME_UNKNOWN, AuditPreflightJobStatus.REJECTED),
        (AuditPreflightJobStatus.OUTCOME_UNKNOWN, AuditPreflightJobStatus.FAILED),
        (AuditPreflightJobStatus.OUTCOME_UNKNOWN, AuditPreflightJobStatus.CANCELLING),
        (AuditPreflightJobStatus.CANCELLING, AuditPreflightJobStatus.CANCELLED),
        (AuditPreflightJobStatus.CANCELLING, AuditPreflightJobStatus.OUTCOME_UNKNOWN),
    }
    all_pairs = {
        (current, target)
        for current in AuditPreflightJobStatus
        for target in AuditPreflightJobStatus
    }

    assert {pair for pair in all_pairs if audit_preflight_can_transition(*pair)} == legal
    for current, target in all_pairs - legal:
        with pytest.raises(InvalidStateTransitionError):
            validate_audit_preflight_transition(current, target)


def test_job_validates_pending_claimed_running_and_succeeded_shapes() -> None:
    pending = _pending_job()
    claimed = _leased_job()
    running = _leased_job(
        status=AuditPreflightJobStatus.RUNNING,
        state_version=3,
        running=True,
    )
    result = _result(running)
    receipt = AuditPreflightExitReceipt(
        job_id=running.job_id,
        effect_owner_digest=running.effect_owner_digest,
        lease_envelope_digest=running.lease_envelope_digest or _digest("missing"),
        capsule_id=running.capsule_id or "missing-capsule",
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=7),
        backend_id=running.backend_id,
        image_digest=running.image_digest,
        policy_digest=running.policy_digest,
        process_identity_digest=_digest("process"),
        result_digest=result.result_digest,
        terminal_state=AuditPreflightExitTerminalState.SUCCEEDED,
        received_at=result.completed_at,
    )
    succeeded_payload = running.model_dump(mode="python")
    succeeded_payload.update(
        status=AuditPreflightJobStatus.SUCCEEDED,
        state_version=4,
        result_schema_version=result.schema_version,
        result_json=result.canonical_json(),
        result_digest=result.result_digest,
        exit_receipt_digest=receipt.receipt_digest,
        updated_at=result.completed_at,
        finished_at=result.completed_at,
    )
    succeeded = AuditPreflightJob.model_validate(succeeded_payload)

    assert pending.state_version == 1
    assert claimed.lease_envelope() is not None
    assert running.started_at is not None
    assert succeeded.result_digest == result.result_digest
    assert succeeded.can_transition_to(AuditPreflightJobStatus.RUNNING) is False


@pytest.mark.parametrize(
    ("status", "updates", "message"),
    [
        (AuditPreflightJobStatus.CLAIMED, {}, "requires a complete lease"),
        (
            AuditPreflightJobStatus.RUNNING,
            {},
            "requires a complete lease|requires a started capsule",
        ),
        (
            AuditPreflightJobStatus.SUCCEEDED,
            {"finished_at": NOW + timedelta(minutes=1)},
            "requires a positive attempt",
        ),
        (
            AuditPreflightJobStatus.REJECTED,
            {
                "safe_error_code": "audit_repository_rejected",
                "finished_at": NOW + timedelta(minutes=1),
            },
            "requires a positive attempt",
        ),
        (
            AuditPreflightJobStatus.CANCELLED,
            {"finished_at": NOW + timedelta(minutes=1)},
            "requires affirmative stop proof",
        ),
    ],
)
def test_job_rejects_invalid_state_shapes(
    status: AuditPreflightJobStatus,
    updates: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _pending_job(
            status=status,
            state_version=2,
            updated_at=NOW + timedelta(minutes=1),
            **updates,
        )


def test_job_rejects_restricted_request_and_owner_binding_drift() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="request_digest"):
        _pending_job(request_digest=_digest("forged"))
    with pytest.raises(ValidationError, match="backend_id"):
        _pending_job(backend_id="different_backend")
    with pytest.raises(ValidationError, match="empty-context digest"):
        _pending_job(canonical_empty_context_digest=_digest("forged-empty-context"))
    with pytest.raises(ValidationError, match="canonical JSON"):
        _pending_job(restricted_request_json=request.canonical_json().replace(":", ": ", 1))


def test_job_rejects_terminal_proof_on_active_state_and_conflicting_proof_paths() -> None:
    with pytest.raises(ValidationError, match="active preflight Job cannot carry terminal proof"):
        _leased_job(stop_receipt_digest=_digest("premature-stop-receipt"))

    running = _leased_job(
        status=AuditPreflightJobStatus.RUNNING,
        state_version=3,
        running=True,
    )
    finished_at = NOW + timedelta(minutes=10)
    payload = running.model_dump(mode="python")
    payload.update(
        status=AuditPreflightJobStatus.FAILED,
        state_version=4,
        safe_error_code="audit_capsule_failed",
        exit_receipt_digest=_digest("exit-receipt"),
        stop_receipt_digest=_digest("stop-receipt"),
        updated_at=finished_at,
        finished_at=finished_at,
    )
    with pytest.raises(ValidationError, match="cannot combine exit proof"):
        AuditPreflightJob.model_validate(payload)


def test_exact_create_replay_helper_reports_every_identity_drift() -> None:
    job = _pending_job()
    exact = {
        "operator_principal_id": job.operator_principal_id,
        "client_request_id": job.client_request_id,
        "authorization_scope_digest": job.authorization_scope_digest,
        "request_schema_version": job.request_schema_version,
        "request_digest": job.request_digest,
    }

    assert audit_preflight_is_exact_replay(job, **exact)
    assert audit_preflight_idempotency_drift_fields(job, **exact) == ()

    drifted = dict(exact)
    drifted.update(
        authorization_scope_digest=_digest("other-scope"),
        request_digest=_digest("other-request"),
    )
    assert audit_preflight_idempotency_drift_fields(job, **drifted) == (
        "authorization_scope_digest",
        "request_digest",
    )
    assert not audit_preflight_is_exact_replay(job, **drifted)


def test_terminal_replay_helper_is_exact_only_for_all_terminal_facts() -> None:
    finished_at = NOW + timedelta(minutes=1)
    job = _pending_job(
        status=AuditPreflightJobStatus.FAILED,
        state_version=2,
        safe_error_code="audit_internal_failure",
        never_created_proof_digest=_digest("never-created"),
        updated_at=finished_at,
        finished_at=finished_at,
    )
    exact = {
        "status": job.status,
        "result_schema_version": None,
        "result_digest": None,
        "safe_error_code": job.safe_error_code,
        "never_created_proof_digest": job.never_created_proof_digest,
        "exit_receipt_digest": None,
        "stop_receipt_digest": None,
    }

    assert audit_preflight_is_exact_terminal_replay(job, **exact)
    assert audit_preflight_terminal_replay_drift_fields(job, **exact) == ()

    drifted = dict(exact)
    drifted["safe_error_code"] = "audit_protocol_failure"
    assert audit_preflight_terminal_replay_drift_fields(job, **drifted) == ("safe_error_code",)
    assert not audit_preflight_is_exact_terminal_replay(job, **drifted)


def test_preflight_domain_has_no_post_preflight_or_run_scoped_fields() -> None:
    forbidden = {
        "audit_id",
        "run_id",
        "plan_digest",
        "preflight_token",
        "cas_handle",
        "snapshot_id",
    }
    models = (
        PreflightRequest,
        AuditPreflightEffectOwner,
        AuditPreflightLeaseEnvelope,
        AuditPreflightResult,
        AuditPreflightExitReceipt,
        AuditPreflightStopReceipt,
        AuditPreflightJob,
    )

    for model in models:
        assert forbidden.isdisjoint(model.model_fields)
    assert AUDIT_PREFLIGHT_REQUEST_SCHEMA_VERSION.endswith("/v1")
    assert AUDIT_PREFLIGHT_RESULT_SCHEMA_VERSION.endswith("/v1")
