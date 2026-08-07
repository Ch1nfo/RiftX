from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.domain.test_audit_domain import _record, _scan

from riftx.application.errors import RepositoryIntegrityError
from riftx.domain import (
    AuditProject,
    AuditVcsKind,
    RunKind,
    SourceSnapshot,
    SourceTargetKind,
)
from riftx.persistence.audit_mappers import (
    audit_contract_from_record,
    audit_project_from_record,
    audit_scan_from_record,
    audit_scan_to_record,
    source_snapshot_from_record,
    source_snapshot_to_record,
)
from riftx.persistence.orm import (
    AuditContractRecord as AuditContractORMRecord,
)
from riftx.persistence.orm import AuditProjectRecord

NOW = datetime(2026, 8, 3, 9, tzinfo=UTC)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _contract_record(*, state_version: int = 1) -> AuditContractORMRecord:
    contract = _record()
    return AuditContractORMRecord(
        contract_id=contract.contract_id,
        audit_id=contract.audit_id,
        schema_version=contract.schema_version,
        canonical_contract_json=contract.canonical_contract_json,
        contract_digest=contract.contract_digest,
        source_target_digest=contract.source_target_digest,
        source_node_id=contract.source_node_id,
        source_ingest_backend_digest=contract.source_ingest_backend_digest,
        source_prepare_proof_digest=contract.source_prepare_proof_digest,
        selected_node_id=contract.selected_node_id,
        required_backend_id=contract.required_backend_id,
        snapshot_hydration_policy_digest=contract.snapshot_hydration_policy_digest,
        preflight_plan_id=None,
        preflight_plan_digest=None,
        security_context_bundle_id=None,
        security_context_bundle_digest=None,
        state_version=state_version,
        created_at=contract.created_at,
        sealed_at=contract.sealed_at,
    )


def test_historical_project_mapper_reads_and_validates_state_version() -> None:
    project = AuditProject(
        id="project-1",
        engagement_id="engagement-1",
        display_name="RiftX",
        vcs_kind=AuditVcsKind.GIT,
        repository_identity_digest=_digest("repository"),
        default_branch="main",
        created_at=NOW,
        updated_at=NOW,
    )
    record = AuditProjectRecord(
        id=project.id,
        engagement_id=project.engagement_id,
        display_name=project.display_name,
        vcs_kind=project.vcs_kind.value,
        repository_identity_digest=project.repository_identity_digest,
        default_branch=project.default_branch,
        state_version=7,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )

    assert audit_project_from_record(record) == project
    record.state_version = 0
    with pytest.raises(RepositoryIntegrityError):
        audit_project_from_record(record)


def test_historical_snapshot_mapper_round_trips_and_redacts_corruption() -> None:
    tree_digest = _digest("tree")
    policy_digest = _digest("policy")
    snapshot = SourceSnapshot(
        id="snapshot-1",
        project_id="project-1",
        source_kind=SourceTargetKind.REVISION,
        commit_sha="a" * 40,
        tree_digest=tree_digest,
        capture_policy_digest=policy_digest,
        materializer_schema_version="materializer/v1",
        snapshot_digest=SourceSnapshot.compute_snapshot_digest(
            tree_digest=tree_digest,
            capture_policy_digest=policy_digest,
            materializer_schema_version="materializer/v1",
        ),
        snapshot_store_version="snapshot-store/v1",
        content_storage_key="cas/snapshots/secret-source",
        manifest_storage_key="cas/manifests/secret-source",
        manifest_digest=_digest("manifest"),
        file_count=10,
        total_bytes=4096,
        created_at=NOW,
        sealed_at=NOW + timedelta(seconds=1),
    )
    record = source_snapshot_to_record(snapshot)

    assert source_snapshot_from_record(record) == snapshot
    record.snapshot_digest = _digest("tampered")
    with pytest.raises(RepositoryIntegrityError) as captured:
        source_snapshot_from_record(record)
    assert "secret-source" not in str(captured.value)


def test_historical_contract_mapper_rejects_noncanonical_json() -> None:
    record = _contract_record(state_version=3)
    assert audit_contract_from_record(record) == _record()

    record.canonical_contract_json = '{"repository_path":"/private/secret/source"}'
    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_contract_from_record(record)
    assert "/private/secret/source" not in str(captured.value)


def test_historical_scan_mapper_validates_owner_and_contract_bindings() -> None:
    contract = _record()
    scan = _scan(record=contract)
    scan_record = audit_scan_to_record(scan, engagement_id="engagement-1", state_version=4)
    contract_record = _contract_record()

    assert audit_scan_from_record(
        scan_record,
        contract_record,
        run_engagement_id="engagement-1",
        run_kind=RunKind.CODE_AUDIT,
        project_engagement_id="engagement-1",
    ) == scan

    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_scan_from_record(
            scan_record,
            contract_record,
            run_engagement_id="other-engagement",
            run_kind=RunKind.CODE_AUDIT,
            project_engagement_id="engagement-1",
        )
    assert captured.value.reason_code == "owner_binding_mismatch"
