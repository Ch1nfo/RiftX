from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.domain.test_audit_domain import (
    _converge_cleanup,
    _record,
    _running_scan,
    _scan,
)

from riftx.application.errors import RepositoryIntegrityError
from riftx.domain import (
    AuditClientRequest,
    AuditClosureStatus,
    AuditLifecycleStatus,
    AuditPhase,
    AuditPhaseRun,
    AuditPhaseRunStatus,
    AuditProject,
    AuditPublicationStatus,
    AuditRiskTier,
    AuditScopeKind,
    AuditScopeStatus,
    AuditScopeUnit,
    AuditStartIntent,
    AuditStartIntentStatus,
    AuditSummaryCount,
    AuditTerminalOutcome,
    AuditVcsKind,
    AuditWorkItem,
    AuditWorkStatus,
    RunKind,
    RunStatus,
    SourceSnapshot,
    SourceTargetKind,
)
from riftx.persistence.audit_mappers import (
    audit_client_request_from_record,
    audit_client_request_to_record,
    audit_contract_from_record,
    audit_contract_to_record,
    audit_phase_run_from_record,
    audit_phase_run_to_record,
    audit_project_from_record,
    audit_project_to_record,
    audit_scan_from_record,
    audit_scan_to_record,
    audit_scope_unit_from_record,
    audit_scope_unit_to_record,
    audit_start_intent_from_record,
    audit_start_intent_to_record,
    audit_work_item_from_record,
    audit_work_item_to_record,
    source_snapshot_from_record,
    source_snapshot_to_record,
)

NOW = datetime(2026, 8, 3, 9, tzinfo=UTC)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _project() -> AuditProject:
    return AuditProject(
        id="project-1",
        engagement_id="engagement-1",
        display_name="RiftX",
        vcs_kind=AuditVcsKind.GIT,
        repository_identity_digest=_digest("repository"),
        default_branch="main",
        created_at=NOW,
        updated_at=NOW,
    )


def _client_request() -> AuditClientRequest:
    return AuditClientRequest(
        client_request_id="6ed6232a-3fb3-4f93-868f-0be291142f31",
        request_digest=_digest("request"),
        audit_id="audit-1",
        run_id="run-1",
        project_id="project-1",
        engagement_id="engagement-1",
        contract_id="contract-1",
        contract_digest=_digest("contract"),
        temporal_workflow_id="riftx-code-audit-audit-1",
        created_at=NOW,
    )


def test_client_request_mapper_round_trip_and_redacted_corruption() -> None:
    request = _client_request()
    record = audit_client_request_to_record(request)

    assert audit_client_request_from_record(record) == request
    record.request_digest = "/private/operator/source/repository"
    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_client_request_from_record(record)
    assert "/private/operator/source" not in str(captured.value)


def _snapshot() -> SourceSnapshot:
    tree_digest = _digest("tree")
    capture_policy_digest = _digest("capture-policy")
    materializer_schema_version = "materializer/v1"
    return SourceSnapshot(
        id="snapshot-1",
        project_id="project-1",
        source_kind=SourceTargetKind.REVISION,
        commit_sha="a" * 40,
        tree_digest=tree_digest,
        capture_policy_digest=capture_policy_digest,
        materializer_schema_version=materializer_schema_version,
        snapshot_digest=SourceSnapshot.compute_snapshot_digest(
            tree_digest=tree_digest,
            capture_policy_digest=capture_policy_digest,
            materializer_schema_version=materializer_schema_version,
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


def _intent() -> AuditStartIntent:
    return AuditStartIntent(
        id="intent-1",
        audit_id="audit-1",
        run_id="run-1",
        start_request_id="request-1",
        contract_digest=_digest("contract"),
        workflow_id="riftx-code-audit-audit-1",
        task_queue="code-audit",
        created_at=NOW,
        updated_at=NOW,
    )


def _phase_run(
    status: AuditPhaseRunStatus = AuditPhaseRunStatus.COMPLETED,
) -> AuditPhaseRun:
    payload: dict[str, object] = {
        "status": status,
        "id": "phase-1",
        "audit_id": "audit-1",
        "phase": AuditPhase.MAP_SCOPE,
        "idempotency_key": "map-scope-1",
        "input_digest": _digest("input"),
        "config_digest": _digest("config"),
        "created_at": NOW,
        "updated_at": NOW,
    }
    if status is AuditPhaseRunStatus.COMPLETED:
        payload.update(
            output_artifact_ids=("artifact-1", "artifact-2"),
            summary_counts=(
                AuditSummaryCount(key="files", count=4),
                AuditSummaryCount(key="symbols", count=12),
            ),
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            updated_at=NOW + timedelta(seconds=1),
        )
    return AuditPhaseRun.model_validate(payload)


def _scope() -> AuditScopeUnit:
    return AuditScopeUnit(
        id="scope-1",
        audit_id="audit-1",
        snapshot_id="snapshot-1",
        kind=AuditScopeKind.FILE,
        relative_path="src/riftx/audit.py",
        blob_digest=_digest("blob"),
        risk_tier=AuditRiskTier.HIGH,
        required_analyses=("agent_hunt", "deterministic_probe"),
        stable_key=_digest("scope-key"),
        created_at=NOW,
        updated_at=NOW,
    )


def _work_item() -> AuditWorkItem:
    return AuditWorkItem(
        id="work-1",
        audit_id="audit-1",
        phase=AuditPhase.AGENT_HUNT,
        epoch=1,
        primary_scope_unit_id="scope-1",
        strategy="hunter_review",
        stable_key=_digest("work-key"),
        risk_tier=AuditRiskTier.HIGH,
        input_digest=_digest("work-input"),
        required_coverage_plan_artifact_id="coverage-plan-1",
        required_coverage_plan_digest=_digest("coverage-plan"),
        created_at=NOW,
        updated_at=NOW,
    )


def test_project_round_trip_preserves_state_version() -> None:
    project = _project()
    record = audit_project_to_record(project, state_version=7)

    assert record.state_version == 7
    assert audit_project_from_record(record) == project


def test_snapshot_round_trip_recomputes_domain_separated_digest() -> None:
    snapshot = _snapshot()
    record = source_snapshot_to_record(snapshot)

    assert source_snapshot_from_record(record) == snapshot

    record.snapshot_digest = _digest("tampered")
    with pytest.raises(RepositoryIntegrityError) as captured:
        source_snapshot_from_record(record)
    assert captured.value.reason_code == "invalid_persisted_state"
    assert "secret-source" not in str(captured.value)


def test_contract_round_trip_reparses_canonical_json_and_checks_redundancy() -> None:
    contract = _record().seal(at=NOW)
    record = audit_contract_to_record(contract, state_version=3)

    assert audit_contract_from_record(record) == contract
    assert record.state_version == 3

    record.selected_node_id = "attacker-node"
    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_contract_from_record(record)
    assert captured.value.reason_code == "invalid_persisted_state"
    assert "/srv/authorized/repository" not in str(captured.value)


def test_contract_mapper_rejects_noncanonical_json_without_leaking_it() -> None:
    record = audit_contract_to_record(_record())
    record.canonical_contract_json = '{"repository_path":"/private/secret/source"}'

    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_contract_from_record(record)

    message = str(captured.value)
    assert "repository_path" not in message
    assert "/private/secret/source" not in message


def test_scan_round_trip_validates_contract_and_owner_bindings() -> None:
    contract = _record()
    scan = _scan(record=contract)
    contract_record = audit_contract_to_record(contract)
    scan_record = audit_scan_to_record(scan, engagement_id="engagement-1", state_version=4)

    rebuilt = audit_scan_from_record(
        scan_record,
        contract_record,
        run_engagement_id="engagement-1",
        run_kind=RunKind.CODE_AUDIT,
        project_engagement_id="engagement-1",
    )

    assert rebuilt == scan
    assert scan_record.run_kind == RunKind.CODE_AUDIT.value
    assert scan_record.state_version == 4


@pytest.mark.parametrize(
    ("run_engagement_id", "run_kind", "project_engagement_id"),
    [
        ("other-engagement", RunKind.CODE_AUDIT, "engagement-1"),
        ("engagement-1", RunKind.GENERAL, "engagement-1"),
        ("engagement-1", RunKind.CODE_AUDIT, "other-engagement"),
    ],
)
def test_scan_mapper_fails_closed_on_owner_mismatch(
    run_engagement_id: str,
    run_kind: RunKind,
    project_engagement_id: str,
) -> None:
    contract = _record()
    record = audit_scan_to_record(_scan(record=contract), engagement_id="engagement-1")

    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_scan_from_record(
            record,
            audit_contract_to_record(contract),
            run_engagement_id=run_engagement_id,
            run_kind=run_kind,
            project_engagement_id=project_engagement_id,
        )

    assert captured.value.reason_code == "owner_binding_mismatch"


def test_scan_mapper_fails_closed_on_contract_mismatch() -> None:
    contract = _record()
    record = audit_scan_to_record(_scan(record=contract), engagement_id="engagement-1")
    record.selected_node_id = "different-node"

    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_scan_from_record(
            record,
            audit_contract_to_record(contract),
            run_engagement_id="engagement-1",
            run_kind=RunKind.CODE_AUDIT,
            project_engagement_id="engagement-1",
        )

    assert captured.value.reason_code == "contract_binding_mismatch"


def test_scan_mapper_classifies_scan_corruption_independently_from_contract() -> None:
    contract = _record()
    record = audit_scan_to_record(_scan(record=contract), engagement_id="engagement-1")
    record.lifecycle_status = "corrupted"

    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_scan_from_record(
            record,
            audit_contract_to_record(contract),
            run_engagement_id="engagement-1",
            run_kind=RunKind.CODE_AUDIT,
            project_engagement_id="engagement-1",
        )

    assert captured.value.reason_code == "invalid_persisted_state"


def test_scan_mapper_enforces_aud506_distribution_fence_on_read_and_write() -> None:
    contract = _record()
    record = audit_scan_to_record(_scan(record=contract), engagement_id="engagement-1")
    record.publication_status = AuditPublicationStatus.PUBLISHED.value
    record.initial_distribution_revision_id = "sensitive-revision"
    record.latest_distribution_revision_id = "sensitive-revision"
    record.publication_finished_at = NOW + timedelta(minutes=1)

    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_scan_from_record(
            record,
            audit_contract_to_record(contract),
            run_engagement_id="engagement-1",
            run_kind=RunKind.CODE_AUDIT,
            project_engagement_id="engagement-1",
        )
    assert captured.value.reason_code == "unsupported_publication_facts"
    assert "sensitive-revision" not in str(captured.value)

    published = _running_scan()
    published = published.transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    published = published.transition_to(AuditLifecycleStatus.CLEANING)
    published = _converge_cleanup(published)
    published = published.record_closure(
        AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE
    )
    published = published.transition_to(AuditLifecycleStatus.SEALING_CORE)
    published = published.record_core_seal(core_seal_root=_digest("core-seal"))
    published = published.transition_to(AuditLifecycleStatus.REPORTING)
    published = published.transition_to(AuditLifecycleStatus.PACKAGING)
    published = published.record_distribution_revision(revision_id="revision-1")

    with pytest.raises(ValueError, match="AUD-506"):
        audit_scan_to_record(published, engagement_id="engagement-1")


def test_start_intent_round_trip_for_claimed_state() -> None:
    intent = _intent()
    claimed = intent.transition_to(
        AuditStartIntentStatus.CLAIMED,
        at=NOW + timedelta(seconds=1),
        lease_owner="dispatcher-1",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    record = audit_start_intent_to_record(claimed, state_version=5)

    assert audit_start_intent_from_record(record) == claimed
    assert record.state_version == 5


def test_start_intent_mapper_rejects_nondeterministic_workflow_id() -> None:
    record = audit_start_intent_to_record(_intent())
    record.workflow_id = "other-workflow"

    with pytest.raises(RepositoryIntegrityError):
        audit_start_intent_from_record(record)


def test_phase_run_round_trip_preserves_strict_json_shape() -> None:
    phase_run = _phase_run()
    record = audit_phase_run_to_record(phase_run, state_version=2)

    assert audit_phase_run_from_record(record) == phase_run

    record.output_artifact_ids_json = "artifact-1"  # type: ignore[assignment]
    with pytest.raises(RepositoryIntegrityError):
        audit_phase_run_from_record(record)


def test_phase_run_round_trip_for_running_state() -> None:
    phase_run = _phase_run(AuditPhaseRunStatus.QUEUED)
    running = phase_run.transition_to(
        AuditPhaseRunStatus.RUNNING,
        at=NOW + timedelta(seconds=1),
    )
    record = audit_phase_run_to_record(running, state_version=8)

    assert audit_phase_run_from_record(record) == running
    assert record.state_version == 8


def test_scope_round_trip_requires_matching_project_binding() -> None:
    scope = _scope()
    record = audit_scope_unit_to_record(scope, project_id="project-1", state_version=3)

    assert audit_scope_unit_from_record(record, project_id="project-1") == scope

    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_scope_unit_from_record(record, project_id="other-project")
    assert captured.value.reason_code == "owner_binding_mismatch"


def test_scope_round_trip_for_terminal_facts() -> None:
    scope = _scope()
    terminal = scope.transition_to(
        AuditScopeStatus.ANALYZED,
        closure_code="covered",
        closure_reason="Covered by both required analyses.",
        receipt_count=2,
        at=NOW + timedelta(seconds=1),
    )
    record = audit_scope_unit_to_record(
        terminal,
        project_id="project-1",
        state_version=4,
    )

    assert audit_scope_unit_from_record(record, project_id="project-1") == terminal
    assert record.state_version == 4


def test_work_item_round_trip_for_leased_state() -> None:
    work_item = _work_item()
    leased = work_item.transition_to(
        AuditWorkStatus.LEASED,
        at=NOW + timedelta(seconds=1),
        lease_owner="worker-1",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    record = audit_work_item_to_record(leased, state_version=11)

    assert audit_work_item_from_record(record) == leased
    assert record.state_version == 11


@pytest.mark.parametrize(
    ("record_factory", "reader"),
    [
        (lambda: audit_project_to_record(_project()), audit_project_from_record),
        (lambda: audit_contract_to_record(_record()), audit_contract_from_record),
        (lambda: audit_start_intent_to_record(_intent()), audit_start_intent_from_record),
        (lambda: audit_phase_run_to_record(_phase_run()), audit_phase_run_from_record),
        (lambda: audit_work_item_to_record(_work_item()), audit_work_item_from_record),
    ],
)
def test_mutable_mapper_rejects_invalid_state_version(
    record_factory: object,
    reader: object,
) -> None:
    record = record_factory()  # type: ignore[operator]
    record.state_version = 0  # type: ignore[attr-defined]

    with pytest.raises(RepositoryIntegrityError):
        reader(record)  # type: ignore[operator]


@pytest.mark.parametrize("state_version", [0, -1, True])
def test_writers_reject_invalid_state_version(state_version: int) -> None:
    with pytest.raises(ValueError, match="state_version"):
        audit_project_to_record(_project(), state_version=state_version)


def test_mapper_integrity_error_redacts_invalid_relative_path() -> None:
    record = audit_scope_unit_to_record(_scope(), project_id="project-1")
    record.relative_path = "/private/operator/secret/repository.py"

    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_scope_unit_from_record(record, project_id="project-1")

    assert "/private/operator/secret" not in str(captured.value)


def test_scope_mapper_rejects_invalid_state_version() -> None:
    record = audit_scope_unit_to_record(_scope(), project_id="project-1")
    record.state_version = 0

    with pytest.raises(RepositoryIntegrityError):
        audit_scope_unit_from_record(record, project_id="project-1")


def test_work_mapper_rejects_unknown_enum_and_invalid_lease_pair() -> None:
    record = audit_work_item_to_record(_work_item())
    record.status = "unknown"
    record.lease_owner = "sensitive-worker"

    with pytest.raises(RepositoryIntegrityError) as captured:
        audit_work_item_from_record(record)

    assert "sensitive-worker" not in str(captured.value)


def test_scan_mapper_rejects_invalid_persisted_state_version() -> None:
    contract = _record()
    record = audit_scan_to_record(_scan(record=contract), engagement_id="engagement-1")
    record.state_version = 0

    with pytest.raises(RepositoryIntegrityError):
        audit_scan_from_record(
            record,
            audit_contract_to_record(contract),
            run_engagement_id="engagement-1",
            run_kind=RunKind.CODE_AUDIT,
            project_engagement_id="engagement-1",
        )


def test_phase_summary_corruption_fails_closed() -> None:
    record = audit_phase_run_to_record(_phase_run())
    record.summary_counts_json = [{"key": "files", "count": -1}]

    with pytest.raises(RepositoryIntegrityError):
        audit_phase_run_from_record(record)


def test_aud506_fence_does_not_reject_prepublication_core_facts() -> None:
    scan = _running_scan()
    scan = scan.transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.PARTIAL,
    )
    scan = scan.transition_to(AuditLifecycleStatus.CLEANING)
    scan = scan.record_cleanup_convergence(
        cleanup_proof_digest=_digest("cleanup"),
        run_terminal_status=RunStatus.COMPLETED,
    )
    scan = scan.record_closure(AuditClosureStatus.PARTIAL_BUDGET)
    scan = scan.transition_to(AuditLifecycleStatus.SEALING_CORE)
    scan = scan.record_core_seal(core_seal_root=_digest("core"))

    record = audit_scan_to_record(scan, engagement_id="engagement-1")

    assert record.publication_status == AuditPublicationStatus.REPORT_PENDING.value
