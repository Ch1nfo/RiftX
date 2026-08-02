"""Strict mappings for the RiftX Code Audit persistence boundary.

Database rows are not trusted merely because they passed database constraints.  Every
read reconstructs the authoritative, frozen Pydantic domain model and turns malformed
state into a redacted :class:`RepositoryIntegrityError`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from riftx.application.errors import RepositoryIntegrityError
from riftx.domain import (
    AnalysisProfile,
    AuditClientRequest,
    AuditClientRequestOperation,
    AuditClosureStatus,
    AuditLifecycleStatus,
    AuditMode,
    AuditPhase,
    AuditPhaseRun,
    AuditPhaseRunStatus,
    AuditProject,
    AuditPublicationStatus,
    AuditPurpose,
    AuditRiskTier,
    AuditScan,
    AuditScopeKind,
    AuditScopeStatus,
    AuditScopeUnit,
    AuditStartIntent,
    AuditStartIntentStatus,
    AuditTerminalOutcome,
    AuditVcsKind,
    AuditWorkItem,
    AuditWorkStatus,
    RunKind,
    RunStatus,
    SourceSnapshot,
    SourceTargetKind,
)
from riftx.domain import (
    AuditContractRecord as DomainAuditContractRecord,
)

from .orm import (
    AuditClientRequestRecord,
    AuditPhaseRunRecord,
    AuditProjectRecord,
    AuditScanRecord,
    AuditScopeUnitRecord,
    AuditStartIntentRecord,
    AuditWorkItemRecord,
    SourceSnapshotRecord,
)
from .orm import (
    AuditContractRecord as AuditContractORMRecord,
)

_INVALID_PERSISTED_STATE = "invalid_persisted_state"
_CONTRACT_BINDING_MISMATCH = "contract_binding_mismatch"
_OWNER_BINDING_MISMATCH = "owner_binding_mismatch"
_UNSUPPORTED_PUBLICATION_FACTS = "unsupported_publication_facts"


class _VersionedRecord(Protocol):
    state_version: int


def _opaque_id(record: object, attribute: str) -> str:
    """Return only a syntactically opaque identifier for an integrity error."""

    value = getattr(record, attribute, None)
    if not isinstance(value, str) or not value or len(value) > 128:
        return "invalid-id"
    allowed = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@+~-")
    return value if all(character in allowed for character in value) else "invalid-id"


def _read_strict[T](
    *,
    entity: str,
    entity_id: str,
    build: Callable[[], T],
    reason_code: str = _INVALID_PERSISTED_STATE,
) -> T:
    try:
        return build()
    except RepositoryIntegrityError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        # The original exception may quote canonical JSON or a sensitive source path.
        # Suppress it at the Repository boundary rather than exposing exception context.
        raise RepositoryIntegrityError(
            entity,
            entity_id,
            reason_code=reason_code,
        ) from None


def _state_version(record: _VersionedRecord, *, entity: str, entity_id: str) -> int:
    def validate() -> int:
        value = record.state_version
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("invalid state version")
        return value

    return _read_strict(entity=entity, entity_id=entity_id, build=validate)


def _validate_write_state_version(state_version: int) -> int:
    if isinstance(state_version, bool) or not isinstance(state_version, int) or state_version < 1:
        raise ValueError("state_version must be an integer greater than or equal to one")
    return state_version


def _json_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("persisted JSON collection must be a list")
    return value


def _reject_aud506_publication_facts(scan: AuditScan) -> None:
    """Keep distribution facts fenced until AUD-506 installs their owning table/FKs."""

    if (
        scan.publication_status is AuditPublicationStatus.PUBLISHED
        or scan.initial_distribution_revision_id is not None
        or scan.latest_distribution_revision_id is not None
        or scan.publication_finished_at is not None
    ):
        raise ValueError("distribution publication facts require AUD-506")


def audit_project_to_record(
    project: AuditProject,
    *,
    state_version: int = 1,
) -> AuditProjectRecord:
    project = AuditProject.model_validate(project)
    return AuditProjectRecord(
        id=project.id,
        engagement_id=project.engagement_id,
        display_name=project.display_name,
        vcs_kind=project.vcs_kind.value,
        repository_identity_digest=project.repository_identity_digest,
        default_branch=project.default_branch,
        state_version=_validate_write_state_version(state_version),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def audit_project_from_record(record: AuditProjectRecord) -> AuditProject:
    entity_id = _opaque_id(record, "id")
    _state_version(record, entity="AuditProject", entity_id=entity_id)
    return _read_strict(
        entity="AuditProject",
        entity_id=entity_id,
        build=lambda: AuditProject.model_validate(
            {
                "id": record.id,
                "engagement_id": record.engagement_id,
                "display_name": record.display_name,
                "vcs_kind": AuditVcsKind(record.vcs_kind),
                "repository_identity_digest": record.repository_identity_digest,
                "default_branch": record.default_branch,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
        ),
    )


def audit_client_request_to_record(
    request: AuditClientRequest,
) -> AuditClientRequestRecord:
    request = AuditClientRequest.model_validate(request)
    return AuditClientRequestRecord(
        client_request_id=request.client_request_id,
        operation=request.operation.value,
        request_schema_version=request.request_schema_version,
        request_digest=request.request_digest,
        audit_id=request.audit_id,
        run_id=request.run_id,
        project_id=request.project_id,
        engagement_id=request.engagement_id,
        contract_id=request.contract_id,
        contract_digest=request.contract_digest,
        temporal_workflow_id=request.temporal_workflow_id,
        created_at=request.created_at,
    )


def audit_client_request_from_record(
    record: AuditClientRequestRecord,
) -> AuditClientRequest:
    entity_id = _opaque_id(record, "client_request_id")
    return _read_strict(
        entity="AuditClientRequest",
        entity_id=entity_id,
        build=lambda: AuditClientRequest.model_validate(
            {
                "client_request_id": record.client_request_id,
                "operation": AuditClientRequestOperation(record.operation),
                "request_schema_version": record.request_schema_version,
                "request_digest": record.request_digest,
                "audit_id": record.audit_id,
                "run_id": record.run_id,
                "project_id": record.project_id,
                "engagement_id": record.engagement_id,
                "contract_id": record.contract_id,
                "contract_digest": record.contract_digest,
                "temporal_workflow_id": record.temporal_workflow_id,
                "created_at": record.created_at,
            }
        ),
    )


def source_snapshot_to_record(snapshot: SourceSnapshot) -> SourceSnapshotRecord:
    snapshot = SourceSnapshot.model_validate(snapshot)
    return SourceSnapshotRecord(
        id=snapshot.id,
        project_id=snapshot.project_id,
        source_kind=snapshot.source_kind.value,
        parent_snapshot_id=snapshot.parent_snapshot_id,
        base_tree_digest=snapshot.base_tree_digest,
        patch_digest=snapshot.patch_digest,
        commit_sha=snapshot.commit_sha,
        base_commit_sha=snapshot.base_commit_sha,
        working_tree_digest=snapshot.working_tree_digest,
        tree_digest=snapshot.tree_digest,
        capture_policy_digest=snapshot.capture_policy_digest,
        materializer_schema_version=snapshot.materializer_schema_version,
        snapshot_digest=snapshot.snapshot_digest,
        snapshot_store_version=snapshot.snapshot_store_version,
        content_storage_key=snapshot.content_storage_key,
        manifest_storage_key=snapshot.manifest_storage_key,
        manifest_digest=snapshot.manifest_digest,
        file_count=snapshot.file_count,
        total_bytes=snapshot.total_bytes,
        created_at=snapshot.created_at,
        sealed_at=snapshot.sealed_at,
    )


def source_snapshot_from_record(record: SourceSnapshotRecord) -> SourceSnapshot:
    entity_id = _opaque_id(record, "id")
    return _read_strict(
        entity="SourceSnapshot",
        entity_id=entity_id,
        build=lambda: SourceSnapshot.model_validate(
            {
                "id": record.id,
                "project_id": record.project_id,
                "source_kind": SourceTargetKind(record.source_kind),
                "parent_snapshot_id": record.parent_snapshot_id,
                "base_tree_digest": record.base_tree_digest,
                "patch_digest": record.patch_digest,
                "commit_sha": record.commit_sha,
                "base_commit_sha": record.base_commit_sha,
                "working_tree_digest": record.working_tree_digest,
                "tree_digest": record.tree_digest,
                "capture_policy_digest": record.capture_policy_digest,
                "materializer_schema_version": record.materializer_schema_version,
                "snapshot_digest": record.snapshot_digest,
                "snapshot_store_version": record.snapshot_store_version,
                "content_storage_key": record.content_storage_key,
                "manifest_storage_key": record.manifest_storage_key,
                "manifest_digest": record.manifest_digest,
                "file_count": record.file_count,
                "total_bytes": record.total_bytes,
                "created_at": record.created_at,
                "sealed_at": record.sealed_at,
            }
        ),
    )


def audit_contract_to_record(
    contract: DomainAuditContractRecord,
    *,
    state_version: int = 1,
) -> AuditContractORMRecord:
    contract = DomainAuditContractRecord.model_validate(contract)
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
        state_version=_validate_write_state_version(state_version),
        created_at=contract.created_at,
        sealed_at=contract.sealed_at,
    )


def audit_contract_from_record(
    record: AuditContractORMRecord,
) -> DomainAuditContractRecord:
    entity_id = _opaque_id(record, "contract_id")
    _state_version(record, entity="AuditContractRecord", entity_id=entity_id)
    return _read_strict(
        entity="AuditContractRecord",
        entity_id=entity_id,
        build=lambda: DomainAuditContractRecord.model_validate(
            {
                "contract_id": record.contract_id,
                "audit_id": record.audit_id,
                "schema_version": record.schema_version,
                "canonical_contract_json": record.canonical_contract_json,
                "contract_digest": record.contract_digest,
                "source_target_digest": record.source_target_digest,
                "source_node_id": record.source_node_id,
                "source_ingest_backend_digest": record.source_ingest_backend_digest,
                "source_prepare_proof_digest": record.source_prepare_proof_digest,
                "selected_node_id": record.selected_node_id,
                "required_backend_id": record.required_backend_id,
                "snapshot_hydration_policy_digest": (record.snapshot_hydration_policy_digest),
                "created_at": record.created_at,
                "sealed_at": record.sealed_at,
            }
        ),
    )


def audit_scan_to_record(
    scan: AuditScan,
    *,
    engagement_id: str,
    state_version: int = 1,
) -> AuditScanRecord:
    scan = AuditScan.model_validate(scan)
    _reject_aud506_publication_facts(scan)
    return AuditScanRecord(
        id=scan.id,
        run_id=scan.run_id,
        engagement_id=engagement_id,
        run_kind=RunKind.CODE_AUDIT.value,
        project_id=scan.project_id,
        contract_id=scan.contract_id,
        snapshot_id=scan.snapshot_id,
        base_snapshot_id=scan.base_snapshot_id,
        baseline_audit_id=scan.baseline_audit_id,
        purpose=scan.purpose.value,
        parent_audit_id=scan.parent_audit_id,
        mode=scan.mode.value,
        analysis_profile=scan.analysis_profile.value,
        lifecycle_status=scan.lifecycle_status.value,
        current_phase=scan.current_phase.value,
        terminal_outcome=(scan.terminal_outcome.value if scan.terminal_outcome else None),
        cleanup_proof_digest=scan.cleanup_proof_digest,
        run_terminal_status=(scan.run_terminal_status.value if scan.run_terminal_status else None),
        closure_status=scan.closure_status.value if scan.closure_status else None,
        publication_status=scan.publication_status.value,
        core_seal_root=scan.core_seal_root,
        initial_distribution_revision_id=scan.initial_distribution_revision_id,
        latest_distribution_revision_id=scan.latest_distribution_revision_id,
        model_profile=scan.model_profile,
        selected_node_id=scan.selected_node_id,
        required_backend_id=scan.required_backend_id,
        policy_digest=scan.policy_digest,
        budget_digest=scan.budget_digest,
        config_digest=scan.config_digest,
        contract_digest=scan.contract_digest,
        temporal_workflow_id=scan.temporal_workflow_id,
        state_version=_validate_write_state_version(state_version),
        created_at=scan.created_at,
        started_at=scan.started_at,
        analysis_finished_at=scan.analysis_finished_at,
        publication_finished_at=scan.publication_finished_at,
        sealed_at=scan.sealed_at,
    )


def audit_scan_from_record(
    record: AuditScanRecord,
    contract_record: AuditContractORMRecord,
    *,
    run_engagement_id: str,
    run_kind: str | RunKind,
    project_engagement_id: str,
) -> AuditScan:
    """Rebuild one Scan and prove every DB-only owner/contract redundancy."""

    entity_id = _opaque_id(record, "id")
    _state_version(record, entity="AuditScan", entity_id=entity_id)
    contract = audit_contract_from_record(contract_record)

    def validate_owner_binding() -> None:
        persisted_run_kind = RunKind(record.run_kind)
        authoritative_run_kind = RunKind(run_kind)
        if (
            persisted_run_kind is not RunKind.CODE_AUDIT
            or authoritative_run_kind is not RunKind.CODE_AUDIT
            or record.engagement_id != run_engagement_id
            or record.engagement_id != project_engagement_id
        ):
            raise ValueError("owner binding mismatch")

    _read_strict(
        entity="AuditScan",
        entity_id=entity_id,
        build=validate_owner_binding,
        reason_code=_OWNER_BINDING_MISMATCH,
    )
    if (
        record.publication_status == AuditPublicationStatus.PUBLISHED.value
        or record.initial_distribution_revision_id is not None
        or record.latest_distribution_revision_id is not None
        or record.publication_finished_at is not None
    ):
        raise RepositoryIntegrityError(
            "AuditScan",
            entity_id,
            reason_code=_UNSUPPORTED_PUBLICATION_FACTS,
        )

    scan = _read_strict(
        entity="AuditScan",
        entity_id=entity_id,
        build=lambda: AuditScan.model_validate(
            {
                "id": record.id,
                "run_id": record.run_id,
                "project_id": record.project_id,
                "contract_id": record.contract_id,
                "snapshot_id": record.snapshot_id,
                "base_snapshot_id": record.base_snapshot_id,
                "baseline_audit_id": record.baseline_audit_id,
                "purpose": AuditPurpose(record.purpose),
                "parent_audit_id": record.parent_audit_id,
                "mode": AuditMode(record.mode),
                "analysis_profile": AnalysisProfile(record.analysis_profile),
                "lifecycle_status": AuditLifecycleStatus(record.lifecycle_status),
                "current_phase": AuditPhase(record.current_phase),
                "terminal_outcome": (
                    AuditTerminalOutcome(record.terminal_outcome)
                    if record.terminal_outcome is not None
                    else None
                ),
                "cleanup_proof_digest": record.cleanup_proof_digest,
                "run_terminal_status": (
                    RunStatus(record.run_terminal_status)
                    if record.run_terminal_status is not None
                    else None
                ),
                "closure_status": (
                    AuditClosureStatus(record.closure_status)
                    if record.closure_status is not None
                    else None
                ),
                "publication_status": AuditPublicationStatus(record.publication_status),
                "core_seal_root": record.core_seal_root,
                "initial_distribution_revision_id": (record.initial_distribution_revision_id),
                "latest_distribution_revision_id": record.latest_distribution_revision_id,
                "model_profile": record.model_profile,
                "selected_node_id": record.selected_node_id,
                "required_backend_id": record.required_backend_id,
                "policy_digest": record.policy_digest,
                "budget_digest": record.budget_digest,
                "config_digest": record.config_digest,
                "contract_digest": record.contract_digest,
                "temporal_workflow_id": record.temporal_workflow_id,
                "created_at": record.created_at,
                "started_at": record.started_at,
                "analysis_finished_at": record.analysis_finished_at,
                "publication_finished_at": record.publication_finished_at,
                "sealed_at": record.sealed_at,
            }
        ),
    )

    def validate_contract_binding() -> AuditScan:
        scan.validate_contract_record(contract)
        return scan

    return _read_strict(
        entity="AuditScan",
        entity_id=entity_id,
        build=validate_contract_binding,
        reason_code=_CONTRACT_BINDING_MISMATCH,
    )


def audit_start_intent_to_record(
    intent: AuditStartIntent,
    *,
    state_version: int = 1,
) -> AuditStartIntentRecord:
    intent = AuditStartIntent.model_validate(intent)
    return AuditStartIntentRecord(
        intent_id=intent.id,
        audit_id=intent.audit_id,
        run_id=intent.run_id,
        start_request_id=intent.start_request_id,
        contract_digest=intent.contract_digest,
        workflow_id=intent.workflow_id,
        task_queue=intent.task_queue,
        status=intent.status.value,
        attempt=intent.attempt,
        lease_owner=intent.lease_owner,
        lease_expires_at=intent.lease_expires_at,
        next_attempt_at=intent.next_attempt_at,
        last_error_code=intent.last_error_code,
        state_version=_validate_write_state_version(state_version),
        created_at=intent.created_at,
        updated_at=intent.updated_at,
        started_at=intent.started_at,
    )


def audit_start_intent_from_record(record: AuditStartIntentRecord) -> AuditStartIntent:
    entity_id = _opaque_id(record, "intent_id")
    _state_version(record, entity="AuditStartIntent", entity_id=entity_id)
    return _read_strict(
        entity="AuditStartIntent",
        entity_id=entity_id,
        build=lambda: AuditStartIntent.model_validate(
            {
                "id": record.intent_id,
                "audit_id": record.audit_id,
                "run_id": record.run_id,
                "start_request_id": record.start_request_id,
                "contract_digest": record.contract_digest,
                "workflow_id": record.workflow_id,
                "task_queue": record.task_queue,
                "status": AuditStartIntentStatus(record.status),
                "attempt": record.attempt,
                "lease_owner": record.lease_owner,
                "lease_expires_at": record.lease_expires_at,
                "next_attempt_at": record.next_attempt_at,
                "last_error_code": record.last_error_code,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "started_at": record.started_at,
            }
        ),
    )


def audit_phase_run_to_record(
    phase_run: AuditPhaseRun,
    *,
    state_version: int = 1,
) -> AuditPhaseRunRecord:
    phase_run = AuditPhaseRun.model_validate(phase_run)
    return AuditPhaseRunRecord(
        id=phase_run.id,
        audit_id=phase_run.audit_id,
        phase=phase_run.phase.value,
        attempt=phase_run.attempt,
        idempotency_key=phase_run.idempotency_key,
        input_digest=phase_run.input_digest,
        config_digest=phase_run.config_digest,
        status=phase_run.status.value,
        output_artifact_ids_json=list(phase_run.output_artifact_ids),
        summary_counts_json=[count.model_dump(mode="json") for count in phase_run.summary_counts],
        error_code=phase_run.error_code,
        error_summary=phase_run.error_summary,
        state_version=_validate_write_state_version(state_version),
        created_at=phase_run.created_at,
        updated_at=phase_run.updated_at,
        started_at=phase_run.started_at,
        finished_at=phase_run.finished_at,
    )


def audit_phase_run_from_record(record: AuditPhaseRunRecord) -> AuditPhaseRun:
    entity_id = _opaque_id(record, "id")
    _state_version(record, entity="AuditPhaseRun", entity_id=entity_id)

    def build() -> AuditPhaseRun:
        output_ids = _json_list(record.output_artifact_ids_json)
        summary_counts = _json_list(record.summary_counts_json)
        if any(not isinstance(value, dict) for value in summary_counts):
            raise TypeError("summary count must be an object")
        return AuditPhaseRun.model_validate(
            {
                "id": record.id,
                "audit_id": record.audit_id,
                "phase": AuditPhase(record.phase),
                "attempt": record.attempt,
                "idempotency_key": record.idempotency_key,
                "input_digest": record.input_digest,
                "config_digest": record.config_digest,
                "status": AuditPhaseRunStatus(record.status),
                "output_artifact_ids": tuple(output_ids),
                "summary_counts": tuple(summary_counts),
                "error_code": record.error_code,
                "error_summary": record.error_summary,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
            }
        )

    return _read_strict(entity="AuditPhaseRun", entity_id=entity_id, build=build)


def audit_scope_unit_to_record(
    scope_unit: AuditScopeUnit,
    *,
    project_id: str,
    state_version: int = 1,
) -> AuditScopeUnitRecord:
    scope_unit = AuditScopeUnit.model_validate(scope_unit)
    return AuditScopeUnitRecord(
        id=scope_unit.id,
        audit_id=scope_unit.audit_id,
        project_id=project_id,
        snapshot_id=scope_unit.snapshot_id,
        stable_key=scope_unit.stable_key,
        kind=scope_unit.kind.value,
        relative_path=scope_unit.relative_path,
        blob_digest=scope_unit.blob_digest,
        symbol_anchor=scope_unit.symbol_anchor,
        risk_tier=scope_unit.risk_tier.value,
        required_analyses_json=list(scope_unit.required_analyses),
        status=scope_unit.status.value,
        closure_code=scope_unit.closure_code,
        closure_reason=scope_unit.closure_reason,
        receipt_count=scope_unit.receipt_count,
        state_version=_validate_write_state_version(state_version),
        created_at=scope_unit.created_at,
        updated_at=scope_unit.updated_at,
    )


def audit_scope_unit_from_record(
    record: AuditScopeUnitRecord,
    *,
    project_id: str,
) -> AuditScopeUnit:
    entity_id = _opaque_id(record, "id")
    _state_version(record, entity="AuditScopeUnit", entity_id=entity_id)

    def build() -> AuditScopeUnit:
        if record.project_id != project_id:
            raise ValueError("owner binding mismatch")
        required_analyses = _json_list(record.required_analyses_json)
        return AuditScopeUnit.model_validate(
            {
                "id": record.id,
                "audit_id": record.audit_id,
                "snapshot_id": record.snapshot_id,
                "kind": AuditScopeKind(record.kind),
                "relative_path": record.relative_path,
                "blob_digest": record.blob_digest,
                "symbol_anchor": record.symbol_anchor,
                "risk_tier": AuditRiskTier(record.risk_tier),
                "required_analyses": tuple(required_analyses),
                "status": AuditScopeStatus(record.status),
                "closure_code": record.closure_code,
                "closure_reason": record.closure_reason,
                "receipt_count": record.receipt_count,
                "stable_key": record.stable_key,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
        )

    reason = (
        _OWNER_BINDING_MISMATCH if record.project_id != project_id else _INVALID_PERSISTED_STATE
    )
    return _read_strict(
        entity="AuditScopeUnit",
        entity_id=entity_id,
        build=build,
        reason_code=reason,
    )


def audit_work_item_to_record(
    work_item: AuditWorkItem,
    *,
    state_version: int = 1,
) -> AuditWorkItemRecord:
    work_item = AuditWorkItem.model_validate(work_item)
    return AuditWorkItemRecord(
        id=work_item.id,
        audit_id=work_item.audit_id,
        phase=work_item.phase.value,
        epoch=work_item.epoch,
        primary_scope_unit_id=work_item.primary_scope_unit_id,
        strategy=work_item.strategy,
        stable_key=work_item.stable_key,
        risk_tier=work_item.risk_tier.value,
        status=work_item.status.value,
        lease_owner=work_item.lease_owner,
        lease_expires_at=work_item.lease_expires_at,
        attempt=work_item.attempt,
        input_digest=work_item.input_digest,
        required_coverage_plan_artifact_id=(work_item.required_coverage_plan_artifact_id),
        required_coverage_plan_digest=work_item.required_coverage_plan_digest,
        receipt_id=work_item.receipt_id,
        state_version=_validate_write_state_version(state_version),
        created_at=work_item.created_at,
        updated_at=work_item.updated_at,
    )


def audit_work_item_from_record(record: AuditWorkItemRecord) -> AuditWorkItem:
    entity_id = _opaque_id(record, "id")
    _state_version(record, entity="AuditWorkItem", entity_id=entity_id)
    return _read_strict(
        entity="AuditWorkItem",
        entity_id=entity_id,
        build=lambda: AuditWorkItem.model_validate(
            {
                "id": record.id,
                "audit_id": record.audit_id,
                "phase": AuditPhase(record.phase),
                "epoch": record.epoch,
                "primary_scope_unit_id": record.primary_scope_unit_id,
                "strategy": record.strategy,
                "stable_key": record.stable_key,
                "risk_tier": AuditRiskTier(record.risk_tier),
                "status": AuditWorkStatus(record.status),
                "lease_owner": record.lease_owner,
                "lease_expires_at": record.lease_expires_at,
                "attempt": record.attempt,
                "input_digest": record.input_digest,
                "required_coverage_plan_artifact_id": (record.required_coverage_plan_artifact_id),
                "required_coverage_plan_digest": record.required_coverage_plan_digest,
                "receipt_id": record.receipt_id,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
        ),
    )


__all__ = [
    "audit_client_request_from_record",
    "audit_client_request_to_record",
    "audit_contract_from_record",
    "audit_contract_to_record",
    "audit_phase_run_from_record",
    "audit_phase_run_to_record",
    "audit_project_from_record",
    "audit_project_to_record",
    "audit_scan_from_record",
    "audit_scan_to_record",
    "audit_scope_unit_from_record",
    "audit_scope_unit_to_record",
    "audit_start_intent_from_record",
    "audit_start_intent_to_record",
    "audit_work_item_from_record",
    "audit_work_item_to_record",
    "source_snapshot_from_record",
    "source_snapshot_to_record",
]
