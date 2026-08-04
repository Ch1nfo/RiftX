from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from pydantic import ValidationError

from riftx.domain import (
    AUDIT_CLIENT_REQUEST_SCHEMA_VERSION,
    AuditClientRequest,
    AuditPhase,
    AuditPhaseRun,
    AuditPhaseRunStatus,
    AuditProject,
    AuditRiskTier,
    AuditScopeKind,
    AuditScopeStatus,
    AuditScopeUnit,
    AuditStartIntent,
    AuditStartIntentStatus,
    AuditSummaryCount,
    AuditVcsKind,
    AuditWorkItem,
    AuditWorkStatus,
    InvalidStateTransitionError,
    SourceSnapshot,
    SourceTargetKind,
)

NOW = datetime(2026, 8, 3, 8, tzinfo=UTC)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _client_request(**updates: object) -> AuditClientRequest:
    payload: dict[str, object] = {
        "client_request_id": "6ed6232a-3fb3-4f93-868f-0be291142f31",
        "request_digest": _digest("request"),
        "audit_id": "audit-1",
        "run_id": "run-1",
        "project_id": "project-1",
        "engagement_id": "engagement-1",
        "contract_id": "contract-1",
        "contract_digest": _digest("contract"),
        "temporal_workflow_id": "riftx-code-audit-audit-1",
        "created_at": NOW,
    }
    payload.update(updates)
    return AuditClientRequest.model_validate(payload)


def test_client_request_freezes_only_digest_and_authoritative_bindings() -> None:
    request = _client_request()

    assert request.request_schema_version == AUDIT_CLIENT_REQUEST_SCHEMA_VERSION
    assert request.operation.value == "create_draft"
    assert "payload" not in AuditClientRequest.model_fields
    assert "repository_path" not in AuditClientRequest.model_fields


@pytest.mark.parametrize(
    "client_request_id",
    [
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",
        "6ED6232A-3FB3-4F93-868F-0BE291142F31",
    ],
)
def test_client_request_rejects_noncanonical_or_zero_uuid(client_request_id: str) -> None:
    with pytest.raises(ValidationError):
        _client_request(client_request_id=client_request_id)


def test_client_request_rejects_schema_digest_and_workflow_confusion() -> None:
    with pytest.raises(ValidationError, match="unsupported schema"):
        _client_request(request_schema_version="riftx.audit-create-draft-request/v99")
    with pytest.raises(ValidationError):
        _client_request(request_digest="not-a-digest")
    with pytest.raises(ValidationError, match="workflow binding"):
        _client_request(temporal_workflow_id="riftx-code-audit-other")


def _snapshot(**updates: object) -> SourceSnapshot:
    payload: dict[str, object] = {
        "id": "snapshot-1",
        "project_id": "project-1",
        "source_kind": SourceTargetKind.REVISION,
        "commit_sha": "a" * 40,
        "tree_digest": _digest("tree"),
        "capture_policy_digest": _digest("capture-policy"),
        "materializer_schema_version": "materializer/v1",
        "snapshot_store_version": "snapshot-store/v1",
        "content_storage_key": "cas/source-snapshot-1",
        "manifest_storage_key": "cas/manifest-1",
        "manifest_digest": _digest("manifest"),
        "file_count": 12,
        "total_bytes": 4_096,
        "created_at": NOW,
        "sealed_at": NOW + timedelta(seconds=1),
    }
    payload.update(updates)
    if "snapshot_digest" not in updates:
        payload["snapshot_digest"] = SourceSnapshot.compute_snapshot_digest(
            tree_digest=str(payload["tree_digest"]),
            capture_policy_digest=str(payload["capture_policy_digest"]),
            materializer_schema_version=str(payload["materializer_schema_version"]),
        )
    return SourceSnapshot.model_validate(payload)


def _intent(status: AuditStartIntentStatus = AuditStartIntentStatus.PENDING) -> AuditStartIntent:
    payload: dict[str, object] = {
        "id": "intent-1",
        "audit_id": "audit-1",
        "run_id": "run-1",
        "start_request_id": "request-1",
        "contract_digest": _digest("contract"),
        "workflow_id": "riftx-code-audit-audit-1",
        "task_queue": "riftx-audit",
        "status": status,
        "created_at": NOW,
        "updated_at": NOW,
    }
    if status is AuditStartIntentStatus.CLAIMED:
        payload.update(
            attempt=1,
            lease_owner="dispatcher-1",
            lease_expires_at=NOW + timedelta(minutes=1),
        )
    elif status is AuditStartIntentStatus.STARTED:
        payload.update(
            attempt=1,
            started_at=NOW + timedelta(seconds=1),
            updated_at=NOW + timedelta(seconds=1),
        )
    elif status is AuditStartIntentStatus.RETRYABLE:
        payload.update(attempt=1, next_attempt_at=NOW + timedelta(minutes=1))
    elif status is AuditStartIntentStatus.OUTCOME_UNKNOWN:
        payload.update(attempt=1)
    return AuditStartIntent.model_validate(payload)


def _phase_run(status: AuditPhaseRunStatus = AuditPhaseRunStatus.QUEUED) -> AuditPhaseRun:
    payload: dict[str, object] = {
        "id": "phase-run-1",
        "audit_id": "audit-1",
        "phase": AuditPhase.MAP_SCOPE,
        "attempt": 1,
        "idempotency_key": "scope-attempt-1",
        "input_digest": _digest("phase-input"),
        "config_digest": _digest("phase-config"),
        "status": status,
        "created_at": NOW,
        "updated_at": NOW,
    }
    if status is AuditPhaseRunStatus.RUNNING:
        payload["started_at"] = NOW
    elif status is AuditPhaseRunStatus.COMPLETED:
        payload.update(
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            updated_at=NOW + timedelta(seconds=1),
        )
    elif status in {
        AuditPhaseRunStatus.FAILED,
        AuditPhaseRunStatus.DEFERRED,
        AuditPhaseRunStatus.NOT_APPLICABLE,
    }:
        payload.update(
            finished_at=NOW + timedelta(seconds=1),
            error_code="phase_reason",
            error_summary="The phase has an explicit terminal reason.",
            updated_at=NOW + timedelta(seconds=1),
        )
        if status is AuditPhaseRunStatus.FAILED:
            payload["started_at"] = NOW
    elif status is AuditPhaseRunStatus.CANCELLED:
        payload.update(
            finished_at=NOW + timedelta(seconds=1),
            updated_at=NOW + timedelta(seconds=1),
        )
    return AuditPhaseRun.model_validate(payload)


def _scope(status: AuditScopeStatus = AuditScopeStatus.INCLUDED) -> AuditScopeUnit:
    payload: dict[str, object] = {
        "id": "scope-1",
        "audit_id": "audit-1",
        "snapshot_id": "snapshot-1",
        "kind": AuditScopeKind.FILE,
        "relative_path": "src/riftx/domain/audit.py",
        "blob_digest": _digest("blob"),
        "risk_tier": AuditRiskTier.MEDIUM,
        "required_analyses": ("agent_review", "native_rules"),
        "status": status,
        "stable_key": _digest("scope-stable-key"),
        "created_at": NOW,
        "updated_at": NOW,
    }
    if status is not AuditScopeStatus.INCLUDED:
        payload.update(
            closure_code=f"scope_{status.value}",
            closure_reason=f"Scope reached {status.value} under the frozen policy.",
            updated_at=NOW + timedelta(seconds=1),
        )
    return AuditScopeUnit.model_validate(payload)


def _work(status: AuditWorkStatus = AuditWorkStatus.QUEUED) -> AuditWorkItem:
    payload: dict[str, object] = {
        "id": "work-1",
        "audit_id": "audit-1",
        "phase": AuditPhase.AGENT_HUNT,
        "epoch": 1,
        "primary_scope_unit_id": "scope-1",
        "strategy": "hunter_review",
        "stable_key": _digest("work-stable-key"),
        "risk_tier": AuditRiskTier.HIGH,
        "status": status,
        "input_digest": _digest("work-input"),
        "required_coverage_plan_artifact_id": "coverage-plan-1",
        "required_coverage_plan_digest": _digest("coverage-plan"),
        "created_at": NOW,
        "updated_at": NOW,
    }
    if status in {AuditWorkStatus.LEASED, AuditWorkStatus.RUNNING}:
        payload.update(
            attempt=1,
            lease_owner="worker-1",
            lease_expires_at=NOW + timedelta(minutes=1),
        )
    elif status in {
        AuditWorkStatus.COMPLETED,
        AuditWorkStatus.FAILED,
        AuditWorkStatus.OUTCOME_UNKNOWN,
    }:
        payload["attempt"] = 1
    if status is AuditWorkStatus.COMPLETED:
        payload["receipt_id"] = "receipt-1"
    return AuditWorkItem.model_validate(payload)


def test_persistence_domain_enums_are_closed_contracts() -> None:
    assert tuple(AuditVcsKind) == (AuditVcsKind.GIT,)
    assert {value.value for value in AuditStartIntentStatus} == {
        "pending",
        "claimed",
        "started",
        "retryable",
        "outcome_unknown",
        "cancelled",
    }
    assert {value.value for value in AuditPhaseRunStatus} == {
        "queued",
        "running",
        "completed",
        "failed",
        "deferred",
        "cancelled",
        "not_applicable",
    }
    assert {value.value for value in AuditScopeKind} == {
        "file",
        "symbol",
        "diff_hunk",
        "dependency",
        "endpoint",
        "configuration",
        "trust_boundary",
    }
    assert {value.value for value in AuditRiskTier} == {"low", "medium", "high", "critical"}
    assert {value.value for value in AuditScopeStatus} == {
        "included",
        "analyzed",
        "excluded",
        "deferred",
        "failed",
    }
    assert {value.value for value in AuditWorkStatus} == {
        "queued",
        "leased",
        "running",
        "completed",
        "failed",
        "deferred",
        "cancelled",
        "outcome_unknown",
    }


def test_audit_project_is_strict_frozen_and_uses_a_global_repository_identity() -> None:
    identity = _digest("canonical repository identity")
    first = AuditProject(
        id="project-1",
        engagement_id="engagement-1",
        display_name="RiftX",
        repository_identity_digest=identity,
        default_branch="main",
        created_at=NOW,
        updated_at=NOW,
    )
    second = AuditProject(
        id="project-2",
        engagement_id="engagement-2",
        display_name="RiftX mirror",
        repository_identity_digest=identity,
        created_at=NOW,
        updated_at=NOW,
    )

    assert first.repository_identity_digest == second.repository_identity_digest == identity
    assert "repository_path" not in AuditProject.model_fields
    assert first.vcs_kind is AuditVcsKind.GIT
    with pytest.raises(ValidationError, match="frozen"):
        first.display_name = "Changed"
    with pytest.raises(TypeError, match="unvalidated model_copy"):
        first.model_copy(update={"display_name": "Changed"})
    with pytest.raises(ValidationError, match="Extra inputs"):
        AuditProject.model_validate({**first.model_dump(), "repository_path": "/sensitive"})
    with pytest.raises(ValidationError):
        AuditProject.model_validate({**first.model_dump(), "engagement_id": 1})


def test_audit_project_rejects_bad_text_and_timestamp_order() -> None:
    payload = {
        "id": "project-1",
        "engagement_id": "engagement-1",
        "display_name": "RiftX",
        "repository_identity_digest": _digest("repository"),
        "created_at": NOW,
        "updated_at": NOW,
    }
    with pytest.raises(ValidationError, match="surrounding whitespace"):
        AuditProject.model_validate({**payload, "display_name": " RiftX"})
    with pytest.raises(ValidationError, match="must not precede"):
        AuditProject.model_validate(
            {**payload, "updated_at": NOW - timedelta(microseconds=1)}
        )
    with pytest.raises(ValidationError):
        AuditProject.model_validate({**payload, "created_at": datetime(2026, 8, 3)})


def test_source_snapshot_digest_has_a_domain_separator_and_canonical_projection() -> None:
    tree = _digest("tree")
    policy = _digest("capture-policy")
    version = "materializer/v1"
    canonical = json.dumps(
        {
            "capture_policy_digest": policy,
            "materializer_schema_version": version,
            "tree_digest": tree,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    expected = hashlib.sha256(b"riftx.source-snapshot/v1\0" + canonical).hexdigest()

    snapshot = _snapshot(
        tree_digest=tree,
        capture_policy_digest=policy,
        materializer_schema_version=version,
    )

    assert snapshot.snapshot_digest == expected
    assert snapshot.sealed_at >= snapshot.created_at
    assert snapshot.model_config["frozen"] is True


def test_source_snapshot_rejects_identity_corruption_and_unsealed_shapes() -> None:
    with pytest.raises(ValidationError, match="snapshot_digest"):
        _snapshot(snapshot_digest=_digest("forged"))
    with pytest.raises(ValidationError, match="must appear together"):
        _snapshot(parent_snapshot_id="snapshot-parent")
    with pytest.raises(ValidationError, match="own parent"):
        _snapshot(
            parent_snapshot_id="snapshot-1",
            base_tree_digest=_digest("base-tree"),
            patch_digest=_digest("patch"),
        )
    with pytest.raises(ValidationError, match="must not precede"):
        _snapshot(sealed_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        _snapshot(commit_sha="HEAD")


def test_source_snapshot_binds_working_tree_digest_to_source_kind() -> None:
    with pytest.raises(ValidationError, match="revision Source Snapshot"):
        _snapshot(working_tree_digest=_digest("working-tree"))
    with pytest.raises(ValidationError, match="requires working_tree_digest"):
        _snapshot(source_kind=SourceTargetKind.WORKING_TREE)
    working_tree = _snapshot(
        source_kind=SourceTargetKind.WORKING_TREE,
        working_tree_digest=_digest("working-tree"),
    )
    assert working_tree.working_tree_digest == _digest("working-tree")


def test_retest_source_snapshot_requires_complete_parent_lineage() -> None:
    snapshot = _snapshot(
        id="snapshot-retest",
        parent_snapshot_id="snapshot-parent",
        base_tree_digest=_digest("base-tree"),
        patch_digest=_digest("patch"),
    )
    assert snapshot.parent_snapshot_id == "snapshot-parent"
    assert snapshot.base_tree_digest == _digest("base-tree")
    assert snapshot.patch_digest == _digest("patch")


_START_EDGES = {
    (AuditStartIntentStatus.PENDING, AuditStartIntentStatus.CLAIMED),
    (AuditStartIntentStatus.PENDING, AuditStartIntentStatus.CANCELLED),
    (AuditStartIntentStatus.CLAIMED, AuditStartIntentStatus.STARTED),
    (AuditStartIntentStatus.CLAIMED, AuditStartIntentStatus.RETRYABLE),
    (AuditStartIntentStatus.CLAIMED, AuditStartIntentStatus.OUTCOME_UNKNOWN),
    (AuditStartIntentStatus.CLAIMED, AuditStartIntentStatus.CANCELLED),
    (AuditStartIntentStatus.RETRYABLE, AuditStartIntentStatus.CLAIMED),
    (AuditStartIntentStatus.RETRYABLE, AuditStartIntentStatus.CANCELLED),
    (AuditStartIntentStatus.OUTCOME_UNKNOWN, AuditStartIntentStatus.STARTED),
    (AuditStartIntentStatus.OUTCOME_UNKNOWN, AuditStartIntentStatus.RETRYABLE),
    (AuditStartIntentStatus.OUTCOME_UNKNOWN, AuditStartIntentStatus.CANCELLED),
}


@pytest.mark.parametrize(("current", "target"), product(AuditStartIntentStatus, repeat=2))
def test_start_intent_transition_allowlist_is_exhaustive(
    current: AuditStartIntentStatus,
    target: AuditStartIntentStatus,
) -> None:
    assert _intent(current).can_transition_to(target) is ((current, target) in _START_EDGES)


def test_start_intent_claim_unknown_retry_and_start_are_validated() -> None:
    intent = _intent()
    claimed = intent.transition_to(
        AuditStartIntentStatus.CLAIMED,
        at=NOW + timedelta(seconds=1),
        lease_owner="dispatcher-1",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    unknown = claimed.transition_to(
        AuditStartIntentStatus.OUTCOME_UNKNOWN,
        at=NOW + timedelta(seconds=2),
        last_error_code="temporal_rpc_ambiguous",
    )
    retryable = unknown.transition_to(
        AuditStartIntentStatus.RETRYABLE,
        at=NOW + timedelta(seconds=3),
        next_attempt_at=NOW + timedelta(minutes=2),
        last_error_code="workflow_not_found",
    )
    reclaimed = retryable.transition_to(
        AuditStartIntentStatus.CLAIMED,
        at=NOW + timedelta(minutes=2),
        lease_owner="dispatcher-2",
        lease_expires_at=NOW + timedelta(minutes=3),
    )
    started = reclaimed.transition_to(
        AuditStartIntentStatus.STARTED,
        at=NOW + timedelta(minutes=2, seconds=1),
    )

    assert intent.status is AuditStartIntentStatus.PENDING
    assert claimed.attempt == 1
    assert unknown.lease_owner is None
    assert retryable.next_attempt_at == NOW + timedelta(minutes=2)
    assert reclaimed.attempt == 2
    assert started.started_at == NOW + timedelta(minutes=2, seconds=1)
    with pytest.raises(InvalidStateTransitionError):
        started.transition_to(AuditStartIntentStatus.RETRYABLE)


def test_start_intent_rejects_invalid_lease_retry_and_timestamp_shapes() -> None:
    with pytest.raises(ValidationError, match="requires a lease"):
        AuditStartIntent.model_validate(
            {**_intent().model_dump(), "status": AuditStartIntentStatus.CLAIMED, "attempt": 1}
        )
    with pytest.raises(ValueError, match="expire after"):
        _intent().transition_to(
            AuditStartIntentStatus.CLAIMED,
            at=NOW + timedelta(seconds=1),
            lease_owner="dispatcher",
            lease_expires_at=NOW,
        )
    claimed = _intent().transition_to(
        AuditStartIntentStatus.CLAIMED,
        at=NOW,
        lease_owner="dispatcher",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="future retry time"):
        claimed.transition_to(
            AuditStartIntentStatus.RETRYABLE,
            at=NOW + timedelta(seconds=2),
            next_attempt_at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="workflow_id must be deterministic"):
        AuditStartIntent.model_validate({**_intent().model_dump(), "workflow_id": "wrong"})
    with pytest.raises(ValidationError, match="updated_at must not precede"):
        AuditStartIntent.model_validate(
            {**_intent().model_dump(), "updated_at": NOW - timedelta(seconds=1)}
        )
    with pytest.raises(ValueError, match="transition time"):
        _intent().transition_to(
            AuditStartIntentStatus.CANCELLED,
            at=NOW - timedelta(seconds=1),
        )
    long_audit_id = "a" * 128
    long_workflow = f"riftx-code-audit-{long_audit_id}"
    intent = AuditStartIntent.model_validate(
        {
            **_intent().model_dump(),
            "audit_id": long_audit_id,
            "workflow_id": long_workflow,
        }
    )
    assert intent.workflow_id == long_workflow


_PHASE_EDGES = {
    (AuditPhaseRunStatus.QUEUED, AuditPhaseRunStatus.RUNNING),
    (AuditPhaseRunStatus.QUEUED, AuditPhaseRunStatus.DEFERRED),
    (AuditPhaseRunStatus.QUEUED, AuditPhaseRunStatus.CANCELLED),
    (AuditPhaseRunStatus.QUEUED, AuditPhaseRunStatus.NOT_APPLICABLE),
    (AuditPhaseRunStatus.RUNNING, AuditPhaseRunStatus.COMPLETED),
    (AuditPhaseRunStatus.RUNNING, AuditPhaseRunStatus.FAILED),
    (AuditPhaseRunStatus.RUNNING, AuditPhaseRunStatus.DEFERRED),
    (AuditPhaseRunStatus.RUNNING, AuditPhaseRunStatus.CANCELLED),
}


@pytest.mark.parametrize(("current", "target"), product(AuditPhaseRunStatus, repeat=2))
def test_phase_run_transition_allowlist_is_exhaustive(
    current: AuditPhaseRunStatus,
    target: AuditPhaseRunStatus,
) -> None:
    assert _phase_run(current).can_transition_to(target) is ((current, target) in _PHASE_EDGES)


def test_phase_run_transition_preserves_bounded_structured_results() -> None:
    queued = _phase_run()
    running = queued.transition_to(AuditPhaseRunStatus.RUNNING, at=NOW)
    completed = running.transition_to(
        AuditPhaseRunStatus.COMPLETED,
        at=NOW + timedelta(seconds=1),
        output_artifact_ids=("artifact-1", "artifact-2"),
        summary_counts=(
            AuditSummaryCount(key="files", count=12),
            AuditSummaryCount(key="signals", count=3),
        ),
    )
    assert completed.status is AuditPhaseRunStatus.COMPLETED
    assert completed.output_artifact_ids == ("artifact-1", "artifact-2")
    assert completed.summary_counts[1].count == 3
    with pytest.raises(InvalidStateTransitionError):
        completed.transition_to(AuditPhaseRunStatus.RUNNING)


def test_phase_run_enforces_result_bounds_order_and_terminal_shape() -> None:
    artifact_ids = tuple(f"artifact-{index:03d}" for index in range(256))
    bounded_artifacts = AuditPhaseRun.model_validate(
        {
            **_phase_run(AuditPhaseRunStatus.COMPLETED).model_dump(),
            "output_artifact_ids": artifact_ids,
        }
    )
    assert len(bounded_artifacts.output_artifact_ids) == 256
    with pytest.raises(ValidationError):
        AuditPhaseRun.model_validate(
            {
                **_phase_run(AuditPhaseRunStatus.COMPLETED).model_dump(),
                "output_artifact_ids": artifact_ids + ("artifact-overflow",),
            }
        )
    with pytest.raises(ValidationError, match="canonical sorted order"):
        AuditPhaseRun.model_validate(
            {
                **_phase_run(AuditPhaseRunStatus.COMPLETED).model_dump(),
                "output_artifact_ids": ("artifact-b", "artifact-a"),
            }
        )
    summary = tuple(
        AuditSummaryCount(key=f"key-{index:03d}", count=index) for index in range(128)
    )
    bounded_summary = AuditPhaseRun.model_validate(
        {
            **_phase_run(AuditPhaseRunStatus.COMPLETED).model_dump(),
            "summary_counts": summary,
        }
    )
    assert len(bounded_summary.summary_counts) == 128
    with pytest.raises(ValidationError):
        AuditPhaseRun.model_validate(
            {
                **_phase_run(AuditPhaseRunStatus.COMPLETED).model_dump(),
                "summary_counts": summary + (AuditSummaryCount(key="overflow", count=1),),
            }
        )
    for active_status in (
        AuditPhaseRunStatus.QUEUED,
        AuditPhaseRunStatus.RUNNING,
    ):
        with pytest.raises(ValidationError, match="active Phase Run"):
            AuditPhaseRun.model_validate(
                {
                    **_phase_run(active_status).model_dump(),
                    "output_artifact_ids": ("artifact-active",),
                }
            )
        with pytest.raises(ValidationError, match="active Phase Run"):
            AuditPhaseRun.model_validate(
                {
                    **_phase_run(active_status).model_dump(),
                    "summary_counts": (AuditSummaryCount(key="active", count=1),),
                }
            )
    with pytest.raises(ValidationError, match="requires a reason"):
        AuditPhaseRun.model_validate(
            {
                **_phase_run().model_dump(),
                "status": AuditPhaseRunStatus.DEFERRED,
                "finished_at": NOW,
            }
        )
    with pytest.raises(ValidationError, match="updated_at must not precede"):
        AuditPhaseRun.model_validate(
            {**_phase_run().model_dump(), "updated_at": NOW - timedelta(seconds=1)}
        )
    with pytest.raises(ValueError, match="transition time"):
        _phase_run().transition_to(
            AuditPhaseRunStatus.RUNNING,
            at=NOW - timedelta(seconds=1),
        )


_SCOPE_EDGES = {
    (AuditScopeStatus.INCLUDED, AuditScopeStatus.ANALYZED),
    (AuditScopeStatus.INCLUDED, AuditScopeStatus.DEFERRED),
    (AuditScopeStatus.INCLUDED, AuditScopeStatus.FAILED),
}


@pytest.mark.parametrize(("current", "target"), product(AuditScopeStatus, repeat=2))
def test_scope_transition_allowlist_is_exhaustive(
    current: AuditScopeStatus,
    target: AuditScopeStatus,
) -> None:
    assert _scope(current).can_transition_to(target) is ((current, target) in _SCOPE_EDGES)


def test_scope_transition_and_risk_elevation_are_monotonic() -> None:
    scope = _scope()
    high = scope.elevate_risk(AuditRiskTier.HIGH, at=NOW + timedelta(seconds=1))
    analyzed = high.transition_to(
        AuditScopeStatus.ANALYZED,
        closure_code="required_analysis_complete",
        closure_reason="All required analysis receipts passed the frozen predicate.",
        receipt_count=2,
        at=NOW + timedelta(seconds=2),
    )
    assert high.risk_tier is AuditRiskTier.HIGH
    assert analyzed.receipt_count == 2
    with pytest.raises(ValueError, match="strictly monotonic"):
        high.elevate_risk(AuditRiskTier.MEDIUM)
    with pytest.raises(InvalidStateTransitionError):
        analyzed.transition_to(
            AuditScopeStatus.FAILED,
            closure_code="late_failure",
            closure_reason="Terminal state cannot be reopened.",
        )
    with pytest.raises(ValueError, match="terminal Scope risk"):
        analyzed.elevate_risk(AuditRiskTier.CRITICAL)
    with pytest.raises(InvalidStateTransitionError):
        scope.transition_to(
            AuditScopeStatus.EXCLUDED,
            closure_code="late_exclusion",
            closure_reason="Exclusions must be frozen when the Scope Unit is created.",
        )
    with pytest.raises(ValueError, match="elevation time"):
        high.elevate_risk(AuditRiskTier.CRITICAL, at=NOW)


def test_scope_validates_paths_requirements_closure_and_bounds() -> None:
    with pytest.raises(ValidationError, match="dot segments"):
        AuditScopeUnit.model_validate({**_scope().model_dump(), "relative_path": "../escape.py"})
    with pytest.raises(ValidationError, match="requires symbol_anchor"):
        AuditScopeUnit.model_validate(
            {**_scope().model_dump(), "kind": AuditScopeKind.SYMBOL, "symbol_anchor": None}
        )
    dependency = AuditScopeUnit.model_validate(
        {
            **_scope().model_dump(),
            "kind": AuditScopeKind.DEPENDENCY,
            "relative_path": None,
        }
    )
    assert dependency.relative_path is None
    analyses = tuple(f"analysis-{index:02d}" for index in range(64))
    bounded_scope = AuditScopeUnit.model_validate(
        {**_scope().model_dump(), "required_analyses": analyses}
    )
    assert len(bounded_scope.required_analyses) == 64
    with pytest.raises(ValidationError):
        AuditScopeUnit.model_validate(
            {
                **_scope().model_dump(),
                "required_analyses": analyses + ("analysis-overflow",),
            }
        )
    with pytest.raises(ValidationError, match="terminal Scope Unit requires"):
        AuditScopeUnit.model_validate(
            {**_scope().model_dump(), "status": AuditScopeStatus.ANALYZED}
        )
    with pytest.raises(ValidationError):
        AuditScopeUnit.model_validate({**_scope().model_dump(), "stable_key": "not-a-digest"})


_WORK_EDGES = {
    (AuditWorkStatus.QUEUED, AuditWorkStatus.LEASED),
    (AuditWorkStatus.QUEUED, AuditWorkStatus.DEFERRED),
    (AuditWorkStatus.QUEUED, AuditWorkStatus.CANCELLED),
    (AuditWorkStatus.LEASED, AuditWorkStatus.QUEUED),
    (AuditWorkStatus.LEASED, AuditWorkStatus.RUNNING),
    (AuditWorkStatus.LEASED, AuditWorkStatus.FAILED),
    (AuditWorkStatus.LEASED, AuditWorkStatus.CANCELLED),
    (AuditWorkStatus.LEASED, AuditWorkStatus.OUTCOME_UNKNOWN),
    (AuditWorkStatus.RUNNING, AuditWorkStatus.COMPLETED),
    (AuditWorkStatus.RUNNING, AuditWorkStatus.FAILED),
    (AuditWorkStatus.RUNNING, AuditWorkStatus.DEFERRED),
    (AuditWorkStatus.RUNNING, AuditWorkStatus.CANCELLED),
    (AuditWorkStatus.RUNNING, AuditWorkStatus.OUTCOME_UNKNOWN),
    (AuditWorkStatus.OUTCOME_UNKNOWN, AuditWorkStatus.COMPLETED),
    (AuditWorkStatus.OUTCOME_UNKNOWN, AuditWorkStatus.FAILED),
    (AuditWorkStatus.OUTCOME_UNKNOWN, AuditWorkStatus.DEFERRED),
    (AuditWorkStatus.OUTCOME_UNKNOWN, AuditWorkStatus.CANCELLED),
}


@pytest.mark.parametrize(("current", "target"), product(AuditWorkStatus, repeat=2))
def test_work_item_transition_allowlist_is_exhaustive(
    current: AuditWorkStatus,
    target: AuditWorkStatus,
) -> None:
    assert _work(current).can_transition_to(target) is ((current, target) in _WORK_EDGES)


def test_work_item_lease_unknown_retry_and_receipt_lifecycle() -> None:
    queued = _work()
    leased = queued.transition_to(
        AuditWorkStatus.LEASED,
        at=NOW + timedelta(seconds=1),
        lease_owner="worker-1",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    running = leased.transition_to(AuditWorkStatus.RUNNING, at=NOW + timedelta(seconds=2))
    unknown = running.transition_to(
        AuditWorkStatus.OUTCOME_UNKNOWN,
        at=NOW + timedelta(seconds=3),
    )
    failed = unknown.transition_to(
        AuditWorkStatus.FAILED,
        at=NOW + timedelta(seconds=4),
    )
    retry = AuditWorkItem.model_validate(
        {
            **_work().model_dump(),
            "id": "work-retry",
            "stable_key": _digest("work-retry-stable-key"),
        }
    )
    leased_again = retry.transition_to(
        AuditWorkStatus.LEASED,
        at=NOW + timedelta(seconds=5),
        lease_owner="worker-2",
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    completed = leased_again.transition_to(
        AuditWorkStatus.RUNNING,
        at=NOW + timedelta(seconds=6),
    ).transition_to(
        AuditWorkStatus.COMPLETED,
        at=NOW + timedelta(seconds=7),
        receipt_id="receipt-1",
    )

    assert queued.status is AuditWorkStatus.QUEUED
    assert leased.attempt == 1
    assert unknown.lease_owner is None
    assert failed.status is AuditWorkStatus.FAILED
    assert leased_again.attempt == 1
    assert completed.receipt_id == "receipt-1"
    with pytest.raises(InvalidStateTransitionError):
        unknown.transition_to(AuditWorkStatus.QUEUED)
    with pytest.raises(InvalidStateTransitionError):
        completed.transition_to(AuditWorkStatus.QUEUED)


def test_work_item_rejects_bad_lease_receipt_time_and_stable_key() -> None:
    with pytest.raises(ValidationError, match="requires a lease"):
        AuditWorkItem.model_validate(
            {
                **_work().model_dump(),
                "status": AuditWorkStatus.RUNNING,
                "attempt": 1,
            }
        )
    with pytest.raises(ValueError, match="expire after"):
        _work().transition_to(
            AuditWorkStatus.LEASED,
            at=NOW + timedelta(seconds=1),
            lease_owner="worker",
            lease_expires_at=NOW,
        )
    with pytest.raises(ValidationError, match="requires receipt_id"):
        AuditWorkItem.model_validate(
            {
                **_work().model_dump(),
                "status": AuditWorkStatus.COMPLETED,
                "attempt": 1,
            }
        )
    with pytest.raises(ValidationError, match="must not precede"):
        AuditWorkItem.model_validate(
            {**_work().model_dump(), "updated_at": NOW - timedelta(seconds=1)}
        )
    with pytest.raises(ValidationError):
        AuditWorkItem.model_validate({**_work().model_dump(), "stable_key": "work-key"})
    future = AuditWorkItem.model_validate(
        {**_work().model_dump(), "updated_at": NOW + timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="transition time"):
        future.transition_to(AuditWorkStatus.CANCELLED, at=NOW)
