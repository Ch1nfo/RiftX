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
    AuditProject,
    AuditRiskTier,
    AuditScopeKind,
    AuditScopeStatus,
    AuditScopeUnit,
    AuditVcsKind,
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


def test_persistence_domain_enums_are_closed_contracts() -> None:
    assert tuple(AuditVcsKind) == (AuditVcsKind.DIRECTORY, AuditVcsKind.GIT)
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


def test_directory_project_and_snapshot_reject_git_state() -> None:
    project = AuditProject(
        id="project-directory",
        engagement_id="engagement-1",
        display_name="Directory project",
        vcs_kind=AuditVcsKind.DIRECTORY,
        repository_identity_digest=_digest("directory-source"),
        created_at=NOW,
        updated_at=NOW,
    )
    assert project.default_branch is None
    with pytest.raises(ValidationError, match="default_branch"):
        AuditProject.model_validate(
            {**project.model_dump(mode="python"), "default_branch": "main"}
        )

    snapshot = _snapshot(
        source_kind=SourceTargetKind.DIRECTORY,
        commit_sha=None,
    )
    assert snapshot.commit_sha is None
    assert snapshot.working_tree_digest is None
    with pytest.raises(ValidationError, match="cannot carry Git"):
        _snapshot(source_kind=SourceTargetKind.DIRECTORY)
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
