from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from riftx.domain import (
    AuditProject,
    Engagement,
    Objective,
    Run,
    RunKind,
    SourceSnapshot,
    SourceTargetKind,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.orm import (
    AuditContractRecord,
    AuditProjectRecord,
    AuditScanRecord,
)

NOW = datetime(2026, 8, 3, 9, tzinfo=UTC)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _domain_digest(domain: str, payload: str) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + payload.encode()).hexdigest()


def _project(
    project_id: str = "project-1",
    *,
    engagement_id: str = "engagement-1",
    created_at: datetime = NOW,
) -> AuditProject:
    return AuditProject(
        id=project_id,
        engagement_id=engagement_id,
        display_name=f"Project {project_id}",
        repository_identity_digest=_digest(f"repository:{project_id}"),
        default_branch="main",
        created_at=created_at,
        updated_at=created_at,
    )


def _snapshot(
    snapshot_id: str = "snapshot-1",
    *,
    project_id: str = "project-1",
    seed: str | None = None,
    parent_snapshot_id: str | None = None,
    created_at: datetime = NOW,
) -> SourceSnapshot:
    seed = seed or snapshot_id
    tree_digest = _digest(f"tree:{seed}")
    capture_policy_digest = _digest(f"capture:{seed}")
    materializer_version = "materializer/v1"
    return SourceSnapshot(
        id=snapshot_id,
        project_id=project_id,
        source_kind=SourceTargetKind.REVISION,
        parent_snapshot_id=parent_snapshot_id,
        base_tree_digest=(
            _digest(f"base-tree:{seed}") if parent_snapshot_id is not None else None
        ),
        patch_digest=_digest(f"patch:{seed}") if parent_snapshot_id is not None else None,
        commit_sha=_digest(f"commit:{seed}"),
        base_commit_sha=(
            _digest(f"base-commit:{seed}") if parent_snapshot_id is not None else None
        ),
        tree_digest=tree_digest,
        capture_policy_digest=capture_policy_digest,
        materializer_schema_version=materializer_version,
        snapshot_digest=SourceSnapshot.compute_snapshot_digest(
            tree_digest=tree_digest,
            capture_policy_digest=capture_policy_digest,
            materializer_schema_version=materializer_version,
        ),
        snapshot_store_version="snapshot-store/v1",
        content_storage_key=f"cas/source/{seed}",
        manifest_storage_key=f"cas/manifest/{seed}",
        manifest_digest=_digest(f"manifest:{seed}"),
        file_count=12,
        total_bytes=4_096,
        created_at=created_at,
        sealed_at=created_at + timedelta(seconds=1),
    )


async def _create_engagement(database: Database, engagement_id: str) -> None:
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id=engagement_id, name=f"Engagement {engagement_id}")
    )


async def _create_project(database: Database, project: AuditProject) -> None:
    async with database.session_factory() as session, session.begin():
        session.add(
            AuditProjectRecord(
                id=project.id,
                engagement_id=project.engagement_id,
                display_name=project.display_name,
                vcs_kind=project.vcs_kind.value,
                repository_identity_digest=project.repository_identity_digest,
                default_branch=project.default_branch,
                state_version=1,
                created_at=project.created_at,
                updated_at=project.updated_at,
            )
        )


async def _create_audit(
    database: Database,
    *,
    audit_id: str = "audit-1",
    run_id: str = "run-1",
    project_id: str = "project-1",
    snapshot_id: str | None = "snapshot-1",
    created_at: datetime = NOW,
) -> None:
    workflow_id = f"riftx-code-audit-{audit_id}"
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id=run_id,
            engagement_id="engagement-1",
            kind=RunKind.CODE_AUDIT,
            node_id="local",
            objective=Objective(description=f"Historical {audit_id}"),
            workspace_path=f"/tmp/riftx/{run_id}",
            temporal_workflow_id=workflow_id,
            created_at=created_at,
        )
    )
    schema_version = "riftx.audit-contract/v2"
    budget_digest = _digest(f"budget:{audit_id}")
    target_digest = _digest(f"target:{audit_id}")
    backend_digest = _digest(f"backend:{audit_id}")
    prepare_digest = _digest(f"prepare:{audit_id}")
    plan_digest = _digest(f"plan:{audit_id}")
    context_digest = _digest(f"context:{audit_id}")
    payload = json.dumps(
        {
            "analysis_profile": "deterministic",
            "audit_id": audit_id,
            "baseline_audit_id": None,
            "budget": {"budget_digest": budget_digest},
            "execution_selection": {
                "source_ingest_backend_component_digest": backend_digest,
                "source_node_id": "local",
                "source_prepare_proof_digest": prepare_digest,
            },
            "mode": "standard",
            "model_profile": None,
            "preflight_plan_digest": plan_digest,
            "preflight_plan_id": f"plan-{audit_id}",
            "project_id": project_id,
            "schema_version": schema_version,
            "security_context_bundle_digest": context_digest,
            "security_context_bundle_id": "riftx.audit-empty-security-context/v1",
            "source_binding": {"source_node_id": "local"},
            "source_target": {"target_digest": target_digest},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    contract_digest = _domain_digest(schema_version, payload)
    async with database.session_factory() as session, session.begin():
        session.add(
            AuditContractRecord(
                contract_id=f"contract-{audit_id}",
                audit_id=audit_id,
                schema_version=schema_version,
                canonical_contract_json=payload,
                contract_digest=contract_digest,
                source_target_digest=target_digest,
                source_node_id="local",
                source_ingest_backend_digest=backend_digest,
                source_prepare_proof_digest=prepare_digest,
                selected_node_id=None,
                required_backend_id=None,
                snapshot_hydration_policy_digest=None,
                preflight_plan_id=f"plan-{audit_id}",
                preflight_plan_digest=plan_digest,
                security_context_bundle_id="riftx.audit-empty-security-context/v1",
                security_context_bundle_digest=context_digest,
                state_version=1,
                created_at=created_at,
                sealed_at=None,
            )
        )
        session.add(
            AuditScanRecord(
                id=audit_id,
                run_id=run_id,
                engagement_id="engagement-1",
                run_kind=RunKind.CODE_AUDIT.value,
                project_id=project_id,
                contract_id=f"contract-{audit_id}",
                snapshot_id=snapshot_id,
                base_snapshot_id=None,
                baseline_audit_id=None,
                purpose="primary",
                parent_audit_id=None,
                mode="standard",
                analysis_profile="deterministic",
                lifecycle_status="draft",
                current_phase="authorize_and_freeze",
                terminal_outcome=None,
                cleanup_proof_digest=None,
                run_terminal_status=None,
                closure_status=None,
                publication_status="not_started",
                core_seal_root=None,
                initial_distribution_revision_id=None,
                latest_distribution_revision_id=None,
                model_profile=None,
                selected_node_id="local",
                required_backend_id=None,
                policy_digest=None,
                budget_digest=budget_digest,
                config_digest=None,
                contract_digest=contract_digest,
                temporal_workflow_id=workflow_id,
                state_version=1,
                created_at=created_at,
                started_at=None,
                analysis_finished_at=None,
                publication_finished_at=None,
                sealed_at=None,
            )
        )
