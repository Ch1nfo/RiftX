"""add RiftX Code Audit persistence foundations

Revision ID: 3b7f1d9e5a02
Revises: 0d3a8b7c4e21
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "3b7f1d9e5a02"
down_revision: str | Sequence[str] | None = "0d3a8b7c4e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT_TABLES = (
    "audit_work_items",
    "audit_scope_units",
    "audit_phase_runs",
    "audit_start_intents",
    "audit_scans",
    "audit_contracts",
    "source_snapshots",
    "audit_projects",
)


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def _optional_lower_hex_digest_check(column: str) -> str:
    return f"{column} IS NULL OR ({_lower_hex_digest_check(column)})"


def _git_object_id_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) IN (40, 64) AND length({remainder}) = 0"


def _optional_git_object_id_check(column: str) -> str:
    return f"{column} IS NULL OR ({_git_object_id_check(column)})"


def upgrade() -> None:
    op.create_index(
        "uq_runs_id_engagement_kind",
        "runs",
        ["id", "engagement_id", "kind"],
        unique=True,
    )
    op.create_index(
        "uq_runs_id_engagement_kind_node",
        "runs",
        ["id", "engagement_id", "kind", "node_id"],
        unique=True,
    )
    op.create_index(
        "uq_runs_id_status",
        "runs",
        ["id", "status"],
        unique=True,
    )

    op.create_table(
        "audit_projects",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("engagement_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("vcs_kind", sa.String(length=32), nullable=False),
        sa.Column("repository_identity_digest", sa.String(length=64), nullable=False),
        sa.Column("default_branch", sa.String(length=1024), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("vcs_kind = 'git'", name="ck_audit_projects_vcs_kind"),
        sa.CheckConstraint(
            _lower_hex_digest_check("repository_identity_digest"),
            name="ck_audit_projects_repository_digest",
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name="ck_audit_projects_state_version",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_projects_timestamp_order",
        ),
        sa.ForeignKeyConstraint(
            ["engagement_id"],
            ["engagements.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repository_identity_digest",
            name="uq_audit_projects_repository_digest",
        ),
        sa.UniqueConstraint(
            "id",
            "engagement_id",
            name="uq_audit_projects_id_engagement",
        ),
    )
    op.create_index(
        "ix_audit_projects_engagement_created_id",
        "audit_projects",
        ["engagement_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "source_snapshots",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("parent_snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("base_tree_digest", sa.String(length=64), nullable=True),
        sa.Column("patch_digest", sa.String(length=64), nullable=True),
        sa.Column("commit_sha", sa.String(length=128), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=128), nullable=True),
        sa.Column("working_tree_digest", sa.String(length=64), nullable=True),
        sa.Column("tree_digest", sa.String(length=64), nullable=False),
        sa.Column("capture_policy_digest", sa.String(length=64), nullable=False),
        sa.Column("materializer_schema_version", sa.String(length=128), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("snapshot_store_version", sa.String(length=128), nullable=False),
        sa.Column("content_storage_key", sa.Text(), nullable=False),
        sa.Column("manifest_storage_key", sa.Text(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('revision', 'working_tree')",
            name="ck_source_snapshots_source_kind",
        ),
        sa.CheckConstraint(
            "(source_kind = 'revision' AND working_tree_digest IS NULL) OR "
            "(source_kind = 'working_tree' AND working_tree_digest IS NOT NULL)",
            name="ck_source_snapshots_working_tree_digest",
        ),
        sa.CheckConstraint(
            "(parent_snapshot_id IS NULL AND base_tree_digest IS NULL "
            "AND patch_digest IS NULL) OR "
            "(parent_snapshot_id IS NOT NULL AND base_tree_digest IS NOT NULL "
            "AND patch_digest IS NOT NULL)",
            name="ck_source_snapshots_retest_fields",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("base_tree_digest"),
            name="ck_source_snapshots_base_tree_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("patch_digest"),
            name="ck_source_snapshots_patch_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("working_tree_digest"),
            name="ck_source_snapshots_working_tree_sha256",
        ),
        sa.CheckConstraint(
            _git_object_id_check("commit_sha"),
            name="ck_source_snapshots_commit_sha",
        ),
        sa.CheckConstraint(
            _optional_git_object_id_check("base_commit_sha"),
            name="ck_source_snapshots_base_commit_sha",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("tree_digest"),
            name="ck_source_snapshots_tree_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("capture_policy_digest"),
            name="ck_source_snapshots_capture_policy_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("snapshot_digest"),
            name="ck_source_snapshots_snapshot_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("manifest_digest"),
            name="ck_source_snapshots_manifest_digest",
        ),
        sa.CheckConstraint(
            "file_count >= 0 AND total_bytes >= 0",
            name="ck_source_snapshots_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "sealed_at >= created_at",
            name="ck_source_snapshots_timestamp_order",
        ),
        sa.CheckConstraint(
            "parent_snapshot_id IS NULL OR parent_snapshot_id <> id",
            name="ck_source_snapshots_distinct_parent",
        ),
        sa.CheckConstraint(
            "length(content_storage_key) BETWEEN 1 AND 4096 AND "
            "length(manifest_storage_key) BETWEEN 1 AND 4096",
            name="ck_source_snapshots_storage_keys",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["audit_projects.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_source_snapshots_parent_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "project_id",
            name="uq_source_snapshots_id_project",
        ),
        sa.UniqueConstraint(
            "project_id",
            "snapshot_digest",
            name="uq_source_snapshots_project_digest",
        ),
    )
    op.create_index(
        "ix_source_snapshots_project_created_id",
        "source_snapshots",
        ["project_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_source_snapshots_parent",
        "source_snapshots",
        ["parent_snapshot_id"],
        unique=False,
    )

    op.create_table(
        "audit_contracts",
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_contract_json", sa.Text(), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("source_target_digest", sa.String(length=64), nullable=False),
        sa.Column("source_node_id", sa.String(length=64), nullable=False),
        sa.Column("source_ingest_backend_digest", sa.String(length=64), nullable=False),
        sa.Column("source_prepare_proof_digest", sa.String(length=64), nullable=False),
        sa.Column("selected_node_id", sa.String(length=64), nullable=False),
        sa.Column("required_backend_id", sa.String(length=256), nullable=False),
        sa.Column(
            "snapshot_hydration_policy_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "schema_version = 'riftx.audit-contract/v1'",
            name="ck_audit_contracts_schema_version",
        ),
        sa.CheckConstraint(
            "length(canonical_contract_json) BETWEEN 2 AND 262144",
            name="ck_audit_contracts_canonical_size",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("contract_digest"),
            name="ck_audit_contracts_contract_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("source_target_digest"),
            name="ck_audit_contracts_source_target_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("source_ingest_backend_digest"),
            name="ck_audit_contracts_source_backend_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("source_prepare_proof_digest"),
            name="ck_audit_contracts_source_proof_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("snapshot_hydration_policy_digest"),
            name="ck_audit_contracts_hydration_digest",
        ),
        sa.CheckConstraint(
            "sealed_at IS NULL OR sealed_at >= created_at",
            name="ck_audit_contracts_timestamp_order",
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name="ck_audit_contracts_state_version",
        ),
        sa.PrimaryKeyConstraint("contract_id"),
        sa.UniqueConstraint("audit_id", name="uq_audit_contracts_audit"),
        sa.UniqueConstraint(
            "contract_id",
            "audit_id",
            "contract_digest",
            name="uq_audit_contracts_binding",
        ),
    )
    op.create_index(
        "ix_audit_contracts_digest",
        "audit_contracts",
        ["contract_digest"],
        unique=False,
    )

    _create_audit_scans()
    _create_audit_start_intents()
    _create_audit_phase_runs()
    _create_audit_scope_units()
    _create_audit_work_items()


def _create_audit_scans() -> None:
    op.create_table(
        "audit_scans",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("engagement_id", sa.String(length=64), nullable=False),
        sa.Column("run_kind", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("base_snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("baseline_audit_id", sa.String(length=128), nullable=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("parent_audit_id", sa.String(length=128), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("analysis_profile", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("current_phase", sa.String(length=32), nullable=False),
        sa.Column("terminal_outcome", sa.String(length=32), nullable=True),
        sa.Column("cleanup_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("run_terminal_status", sa.String(length=32), nullable=True),
        sa.Column("closure_status", sa.String(length=64), nullable=True),
        sa.Column("publication_status", sa.String(length=32), nullable=False),
        sa.Column("core_seal_root", sa.String(length=64), nullable=True),
        sa.Column(
            "initial_distribution_revision_id",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column(
            "latest_distribution_revision_id",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column("model_profile", sa.String(length=255), nullable=True),
        sa.Column("selected_node_id", sa.String(length=64), nullable=False),
        sa.Column("required_backend_id", sa.String(length=256), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("budget_digest", sa.String(length=64), nullable=False),
        sa.Column("config_digest", sa.String(length=64), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("temporal_workflow_id", sa.String(length=256), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analysis_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publication_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id", "engagement_id", "run_kind", "selected_node_id"],
            ["runs.id", "runs.engagement_id", "runs.kind", "runs.node_id"],
            name="fk_audit_scans_run_owner_kind_node",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "run_terminal_status"],
            ["runs.id", "runs.status"],
            name="fk_audit_scans_run_terminal_status",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "engagement_id"],
            ["audit_projects.id", "audit_projects.engagement_id"],
            name="fk_audit_scans_project_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["contract_id", "id", "contract_digest"],
            [
                "audit_contracts.contract_id",
                "audit_contracts.audit_id",
                "audit_contracts.contract_digest",
            ],
            name="fk_audit_scans_contract_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_audit_scans_snapshot_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_audit_scans_base_snapshot_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_scans_baseline_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_scans_parent_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_audit_scans_run"),
        sa.UniqueConstraint(
            "temporal_workflow_id",
            name="uq_audit_scans_temporal_workflow",
        ),
        sa.UniqueConstraint(
            "id",
            "project_id",
            name="uq_audit_scans_id_project",
        ),
        sa.UniqueConstraint(
            "id",
            "run_id",
            "contract_digest",
            "temporal_workflow_id",
            name="uq_audit_scans_start_binding",
        ),
        *_audit_scan_checks(),
    )
    op.create_index(
        "ix_audit_scans_project_lifecycle_created_id",
        "audit_scans",
        ["project_id", "lifecycle_status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_scans_lifecycle_phase_created_id",
        "audit_scans",
        ["lifecycle_status", "current_phase", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_scans_publication_created_id",
        "audit_scans",
        ["publication_status", "created_at", "id"],
        unique=False,
    )


def _audit_scan_checks() -> tuple[sa.CheckConstraint, ...]:
    return (
        sa.CheckConstraint("run_kind = 'code_audit'", name="ck_audit_scans_run_kind"),
        sa.CheckConstraint(
            "purpose IN ('primary', 'validation_followup', 'retest')",
            name="ck_audit_scans_purpose",
        ),
        sa.CheckConstraint(
            "mode IN ('standard', 'deep', 'diff')",
            name="ck_audit_scans_mode",
        ),
        sa.CheckConstraint(
            "analysis_profile IN ('deterministic', 'hybrid')",
            name="ck_audit_scans_analysis_profile",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('draft', 'queued', 'preflighting', 'snapshotting', "
            "'running', 'waiting_approval', 'pausing', 'paused', 'finalizing', "
            "'cancelling', 'failing', 'cleaning', 'sealing_core', 'reporting', "
            "'packaging', 'completed', 'completed_partial', 'failed', 'cancelled')",
            name="ck_audit_scans_lifecycle_status",
        ),
        sa.CheckConstraint(
            "current_phase IN ('authorize_and_freeze', 'map_scope', "
            "'deterministic_probe', 'threat_model', 'agent_hunt', 'reconcile', "
            "'prove', 'compose_risk', 'compare_baseline', 'validate_closure', "
            "'cleanup', 'seal_core', 'generate_reports', 'package_and_publish')",
            name="ck_audit_scans_current_phase",
        ),
        sa.CheckConstraint(
            "terminal_outcome IS NULL OR terminal_outcome IN "
            "('complete', 'partial', 'failed', 'cancelled')",
            name="ck_audit_scans_terminal_outcome",
        ),
        sa.CheckConstraint(
            "closure_status IS NULL OR closure_status IN "
            "('complete_under_declared_scope', 'complete_with_policy_exclusions', "
            "'partial_capability', 'partial_budget', 'failed', 'cancelled')",
            name="ck_audit_scans_closure_status",
        ),
        sa.CheckConstraint(
            "publication_status IN ('not_started', 'sealing_core', 'report_pending', "
            "'reporting', 'packaging', 'published', 'seal_failed', 'report_failed', "
            "'package_failed')",
            name="ck_audit_scans_publication_status",
        ),
        sa.CheckConstraint(
            "run_terminal_status IS NULL OR run_terminal_status IN "
            "('completed', 'failed', 'cancelled')",
            name="ck_audit_scans_run_terminal_status",
        ),
        sa.CheckConstraint(
            "mode <> 'deep' OR analysis_profile = 'hybrid'",
            name="ck_audit_scans_deep_profile",
        ),
        sa.CheckConstraint(
            "(purpose = 'primary' AND parent_audit_id IS NULL) OR "
            "(purpose <> 'primary' AND parent_audit_id IS NOT NULL "
            "AND parent_audit_id <> id)",
            name="ck_audit_scans_parent_purpose",
        ),
        sa.CheckConstraint(
            "baseline_audit_id IS NULL OR baseline_audit_id <> id",
            name="ck_audit_scans_distinct_baseline",
        ),
        sa.CheckConstraint(
            "(mode = 'diff' AND ((snapshot_id IS NULL AND base_snapshot_id IS NULL) OR "
            "(snapshot_id IS NOT NULL AND base_snapshot_id IS NOT NULL "
            "AND snapshot_id <> base_snapshot_id))) OR "
            "(mode <> 'diff' AND base_snapshot_id IS NULL)",
            name="ck_audit_scans_snapshot_mode",
        ),
        sa.CheckConstraint(
            "terminal_outcome NOT IN ('complete', 'partial') OR snapshot_id IS NOT NULL",
            name="ck_audit_scans_outcome_snapshot",
        ),
        sa.CheckConstraint(
            "(cleanup_proof_digest IS NULL AND run_terminal_status IS NULL) OR "
            "(cleanup_proof_digest IS NOT NULL AND run_terminal_status IS NOT NULL)",
            name="ck_audit_scans_cleanup_pair",
        ),
        sa.CheckConstraint(
            "cleanup_proof_digest IS NULL OR "
            "(terminal_outcome IN ('complete', 'partial') "
            "AND run_terminal_status = 'completed') OR "
            "(terminal_outcome = 'failed' AND run_terminal_status = 'failed') OR "
            "(terminal_outcome = 'cancelled' AND run_terminal_status = 'cancelled')",
            name="ck_audit_scans_cleanup_outcome",
        ),
        sa.CheckConstraint(
            "closure_status IS NULL OR cleanup_proof_digest IS NOT NULL",
            name="ck_audit_scans_closure_cleanup",
        ),
        sa.CheckConstraint(
            "(core_seal_root IS NULL AND sealed_at IS NULL) OR "
            "(core_seal_root IS NOT NULL AND sealed_at IS NOT NULL)",
            name="ck_audit_scans_core_seal_pair",
        ),
        sa.CheckConstraint(
            "(publication_status IN ('not_started', 'sealing_core', 'seal_failed') "
            "AND core_seal_root IS NULL) OR "
            "(publication_status NOT IN ('not_started', 'sealing_core', 'seal_failed') "
            "AND core_seal_root IS NOT NULL)",
            name="ck_audit_scans_publication_core",
        ),
        sa.CheckConstraint(
            "(initial_distribution_revision_id IS NULL "
            "AND latest_distribution_revision_id IS NULL "
            "AND publication_finished_at IS NULL) OR "
            "(initial_distribution_revision_id IS NOT NULL "
            "AND latest_distribution_revision_id IS NOT NULL "
            "AND publication_finished_at IS NOT NULL)",
            name="ck_audit_scans_distribution_pair",
        ),
        sa.CheckConstraint(
            "(publication_status = 'published' "
            "AND initial_distribution_revision_id IS NOT NULL) OR "
            "(publication_status <> 'published' "
            "AND initial_distribution_revision_id IS NULL)",
            name="ck_audit_scans_published_revision",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("cleanup_proof_digest"),
            name="ck_audit_scans_cleanup_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("core_seal_root"),
            name="ck_audit_scans_core_seal_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("policy_digest"),
            name="ck_audit_scans_policy_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("budget_digest"),
            name="ck_audit_scans_budget_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("config_digest"),
            name="ck_audit_scans_config_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("contract_digest"),
            name="ck_audit_scans_contract_digest",
        ),
        sa.CheckConstraint("state_version >= 1", name="ck_audit_scans_state_version"),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="ck_audit_scans_started_order",
        ),
        sa.CheckConstraint(
            "analysis_finished_at IS NULL OR analysis_finished_at >= created_at",
            name="ck_audit_scans_analysis_order",
        ),
        sa.CheckConstraint(
            "analysis_finished_at IS NULL OR started_at IS NULL "
            "OR analysis_finished_at >= started_at",
            name="ck_audit_scans_analysis_started_order",
        ),
        sa.CheckConstraint(
            "sealed_at IS NULL OR analysis_finished_at IS NULL "
            "OR sealed_at >= analysis_finished_at",
            name="ck_audit_scans_sealed_order",
        ),
        sa.CheckConstraint(
            "publication_finished_at IS NULL OR sealed_at IS NULL "
            "OR publication_finished_at >= sealed_at",
            name="ck_audit_scans_publication_order",
        ),
    )


def _create_audit_start_intents() -> None:
    op.create_table(
        "audit_start_intents",
        sa.Column("intent_id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("start_request_id", sa.String(length=256), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=256), nullable=False),
        sa.Column("task_queue", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=256), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=256), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'started', 'retryable', "
            "'outcome_unknown', 'cancelled')",
            name="ck_audit_start_intents_status",
        ),
        sa.CheckConstraint(
            "attempt >= 0 AND state_version >= 1",
            name="ck_audit_start_intents_counters",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_audit_start_intents_lease_pair",
        ),
        sa.CheckConstraint(
            "(status = 'claimed' AND lease_owner IS NOT NULL) OR "
            "(status <> 'claimed' AND lease_owner IS NULL)",
            name="ck_audit_start_intents_claim_lease",
        ),
        sa.CheckConstraint(
            "(status = 'retryable' AND next_attempt_at IS NOT NULL) OR "
            "status IN ('pending', 'outcome_unknown') OR "
            "(status NOT IN ('retryable', 'pending', 'outcome_unknown') "
            "AND next_attempt_at IS NULL)",
            name="ck_audit_start_intents_retry_time",
        ),
        sa.CheckConstraint(
            "(status = 'started' AND started_at IS NOT NULL) OR "
            "(status <> 'started' AND started_at IS NULL)",
            name="ck_audit_start_intents_started_time",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("contract_digest"),
            name="ck_audit_start_intents_contract_digest",
        ),
        sa.CheckConstraint(
            "status NOT IN ('claimed', 'started', 'retryable', 'outcome_unknown') "
            "OR attempt >= 1",
            name="ck_audit_start_intents_attempted_status",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_start_intents_timestamp_order",
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > updated_at",
            name="ck_audit_start_intents_lease_order",
        ),
        sa.CheckConstraint(
            "next_attempt_at IS NULL OR next_attempt_at >= updated_at",
            name="ck_audit_start_intents_retry_order",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR "
            "(started_at >= created_at AND started_at <= updated_at)",
            name="ck_audit_start_intents_started_order",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id", "run_id", "contract_digest", "workflow_id"],
            [
                "audit_scans.id",
                "audit_scans.run_id",
                "audit_scans.contract_digest",
                "audit_scans.temporal_workflow_id",
            ],
            name="fk_audit_start_intents_scan_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("intent_id"),
        sa.UniqueConstraint("audit_id", name="uq_audit_start_intents_audit"),
        sa.UniqueConstraint(
            "start_request_id",
            name="uq_audit_start_intents_start_request",
        ),
        sa.UniqueConstraint(
            "workflow_id",
            name="uq_audit_start_intents_workflow",
        ),
    )
    op.create_index(
        "ix_audit_start_intents_dispatch",
        "audit_start_intents",
        [
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "created_at",
            "intent_id",
        ],
        unique=False,
    )


def _create_audit_phase_runs() -> None:
    op.create_table(
        "audit_phase_runs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("config_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("output_artifact_ids_json", sa.JSON(), nullable=False),
        sa.Column("summary_counts_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=256), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "phase IN ('authorize_and_freeze', 'map_scope', 'deterministic_probe', "
            "'threat_model', 'agent_hunt', 'reconcile', 'prove', 'compose_risk', "
            "'compare_baseline', 'validate_closure', 'cleanup', 'seal_core', "
            "'generate_reports', 'package_and_publish')",
            name="ck_audit_phase_runs_phase",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'deferred', "
            "'cancelled', 'not_applicable')",
            name="ck_audit_phase_runs_status",
        ),
        sa.CheckConstraint(
            "attempt >= 1 AND state_version >= 1",
            name="ck_audit_phase_runs_counters",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("input_digest"),
            name="ck_audit_phase_runs_input_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("config_digest"),
            name="ck_audit_phase_runs_config_digest",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_phase_runs_timestamp_order",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR "
            "(started_at >= created_at AND started_at <= updated_at)",
            name="ck_audit_phase_runs_started_order",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR "
            "(finished_at >= created_at AND finished_at <= updated_at)",
            name="ck_audit_phase_runs_finished_order",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="ck_audit_phase_runs_runtime_order",
        ),
        sa.CheckConstraint(
            "(error_code IS NULL AND error_summary IS NULL) OR "
            "(error_code IS NOT NULL AND error_summary IS NOT NULL)",
            name="ck_audit_phase_runs_error_pair",
        ),
        sa.CheckConstraint(
            "error_summary IS NULL OR length(error_summary) BETWEEN 1 AND 4096",
            name="ck_audit_phase_runs_error_summary_size",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status NOT IN ('queued', 'running') AND finished_at IS NOT NULL)",
            name="ck_audit_phase_runs_status_time",
        ),
        sa.CheckConstraint(
            "status NOT IN ('queued', 'running') OR "
            "(json_array_length(output_artifact_ids_json) = 0 AND "
            "json_array_length(summary_counts_json) = 0)",
            name="ck_audit_phase_runs_active_outputs",
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR started_at IS NOT NULL",
            name="ck_audit_phase_runs_completed_start",
        ),
        sa.CheckConstraint(
            "(status IN ('failed', 'deferred', 'not_applicable') "
            "AND error_code IS NOT NULL) OR "
            "(status NOT IN ('failed', 'deferred', 'not_applicable'))",
            name="ck_audit_phase_runs_error_status",
        ),
        sa.CheckConstraint(
            "status NOT IN ('queued', 'running', 'completed') OR error_code IS NULL",
            name="ck_audit_phase_runs_nonerror_status",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audit_scans.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "audit_id",
            "phase",
            "idempotency_key",
            name="uq_audit_phase_runs_idempotency",
        ),
    )
    op.create_index(
        "ix_audit_phase_runs_audit_phase_status_created_id",
        "audit_phase_runs",
        ["audit_id", "phase", "status", "created_at", "id"],
        unique=False,
    )


def _create_audit_scope_units() -> None:
    op.create_table(
        "audit_scope_units",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("stable_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("blob_digest", sa.String(length=64), nullable=True),
        sa.Column("symbol_anchor", sa.String(length=2048), nullable=True),
        sa.Column("risk_tier", sa.String(length=32), nullable=False),
        sa.Column("required_analyses_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("closure_code", sa.String(length=256), nullable=True),
        sa.Column("closure_reason", sa.Text(), nullable=True),
        sa.Column("receipt_count", sa.Integer(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('file', 'symbol', 'diff_hunk', 'dependency', 'endpoint', "
            "'configuration', 'trust_boundary')",
            name="ck_audit_scope_units_kind",
        ),
        sa.CheckConstraint(
            "risk_tier IN ('low', 'medium', 'high', 'critical')",
            name="ck_audit_scope_units_risk_tier",
        ),
        sa.CheckConstraint(
            "status IN ('included', 'analyzed', 'excluded', 'deferred', 'failed')",
            name="ck_audit_scope_units_status",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("stable_key"),
            name="ck_audit_scope_units_stable_key",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("blob_digest"),
            name="ck_audit_scope_units_blob_digest",
        ),
        sa.CheckConstraint(
            "receipt_count >= 0 AND state_version >= 1",
            name="ck_audit_scope_units_counters",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_scope_units_timestamp_order",
        ),
        sa.CheckConstraint(
            "(closure_code IS NULL AND closure_reason IS NULL) OR "
            "(closure_code IS NOT NULL AND closure_reason IS NOT NULL)",
            name="ck_audit_scope_units_closure_pair",
        ),
        sa.CheckConstraint(
            "relative_path IS NULL OR length(relative_path) BETWEEN 1 AND 4096",
            name="ck_audit_scope_units_relative_path_size",
        ),
        sa.CheckConstraint(
            "closure_reason IS NULL OR length(closure_reason) BETWEEN 1 AND 4096",
            name="ck_audit_scope_units_closure_reason_size",
        ),
        sa.CheckConstraint(
            "(status = 'included' AND closure_code IS NULL) OR "
            "(status <> 'included' AND closure_code IS NOT NULL)",
            name="ck_audit_scope_units_status_closure",
        ),
        sa.CheckConstraint(
            "kind NOT IN ('file', 'symbol', 'diff_hunk', 'configuration') "
            "OR relative_path IS NOT NULL",
            name="ck_audit_scope_units_path_kind",
        ),
        sa.CheckConstraint(
            "kind <> 'symbol' OR symbol_anchor IS NOT NULL",
            name="ck_audit_scope_units_symbol_anchor",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_scope_units_audit_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_audit_scope_units_snapshot_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "audit_id",
            name="uq_audit_scope_units_id_audit",
        ),
        sa.UniqueConstraint(
            "audit_id",
            "snapshot_id",
            "kind",
            "stable_key",
            name="uq_audit_scope_units_stable_key",
        ),
    )
    op.create_index(
        "ix_audit_scope_units_audit_kind_status_risk_id",
        "audit_scope_units",
        ["audit_id", "kind", "status", "risk_tier", "id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_scope_units_audit_snapshot_path",
        "audit_scope_units",
        ["audit_id", "snapshot_id", "relative_path"],
        unique=False,
    )


def _create_audit_work_items() -> None:
    op.create_table(
        "audit_work_items",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("primary_scope_unit_id", sa.String(length=128), nullable=False),
        sa.Column("strategy", sa.String(length=256), nullable=False),
        sa.Column("stable_key", sa.String(length=64), nullable=False),
        sa.Column("risk_tier", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_owner", sa.String(length=256), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "required_coverage_plan_artifact_id",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "required_coverage_plan_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("receipt_id", sa.String(length=128), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "phase IN ('authorize_and_freeze', 'map_scope', 'deterministic_probe', "
            "'threat_model', 'agent_hunt', 'reconcile', 'prove', 'compose_risk', "
            "'compare_baseline', 'validate_closure', 'cleanup', 'seal_core', "
            "'generate_reports', 'package_and_publish')",
            name="ck_audit_work_items_phase",
        ),
        sa.CheckConstraint(
            "risk_tier IN ('low', 'medium', 'high', 'critical')",
            name="ck_audit_work_items_risk_tier",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'completed', 'failed', "
            "'deferred', 'cancelled', 'outcome_unknown')",
            name="ck_audit_work_items_status",
        ),
        sa.CheckConstraint(
            "epoch >= 0 AND attempt >= 0 AND state_version >= 1",
            name="ck_audit_work_items_counters",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_audit_work_items_lease_pair",
        ),
        sa.CheckConstraint(
            "(status IN ('leased', 'running') AND lease_owner IS NOT NULL) OR "
            "(status NOT IN ('leased', 'running') AND lease_owner IS NULL)",
            name="ck_audit_work_items_active_lease",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("input_digest"),
            name="ck_audit_work_items_input_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("required_coverage_plan_digest"),
            name="ck_audit_work_items_plan_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("stable_key"),
            name="ck_audit_work_items_stable_key",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_audit_work_items_timestamp_order",
        ),
        sa.CheckConstraint(
            "status NOT IN ('leased', 'running', 'completed', 'failed', "
            "'outcome_unknown') OR attempt >= 1",
            name="ck_audit_work_items_attempted_status",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND receipt_id IS NOT NULL) OR "
            "(status <> 'completed' AND receipt_id IS NULL)",
            name="ck_audit_work_items_receipt_status",
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > updated_at",
            name="ck_audit_work_items_lease_order",
        ),
        sa.ForeignKeyConstraint(
            ["primary_scope_unit_id", "audit_id"],
            ["audit_scope_units.id", "audit_scope_units.audit_id"],
            name="fk_audit_work_items_primary_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "audit_id",
            "phase",
            "epoch",
            "stable_key",
            name="uq_audit_work_items_stable_key",
        ),
    )
    op.create_index(
        "ix_audit_work_items_audit_phase_status_lease_epoch_id",
        "audit_work_items",
        ["audit_id", "phase", "status", "lease_expires_at", "epoch", "id"],
        unique=False,
    )


def downgrade() -> None:
    _require_empty_audit_tables()
    for table_name in _AUDIT_TABLES:
        op.drop_table(table_name)
    op.drop_index("uq_runs_id_status", table_name="runs")
    op.drop_index("uq_runs_id_engagement_kind_node", table_name="runs")
    op.drop_index("uq_runs_id_engagement_kind", table_name="runs")


def _require_empty_audit_tables() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "Code Audit persistence downgrade requires an online database to "
            "prove that no Audit facts would be lost"
        )
    connection = op.get_bind()
    _serialize_audit_downgrade(connection)
    nonempty = [
        table_name
        for table_name in _AUDIT_TABLES
        if connection.execute(
            sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')
        ).first()
        is not None
    ]
    if nonempty:
        raise RuntimeError(
            "cannot downgrade Code Audit persistence while Audit facts exist in: "
            + ", ".join(nonempty)
        )


def _serialize_audit_downgrade(connection: Connection) -> None:
    if not connection.in_transaction():
        raise RuntimeError(
            "Code Audit persistence downgrade requires one active transaction "
            "for serialization, verification, and DDL"
        )

    dialect_name = connection.dialect.name
    if dialect_name == "postgresql":
        # ACCESS EXCLUSIVE conflicts with every concurrent access mode that can
        # introduce an Audit fact.  Acquire every lock in one fixed order before
        # checking any table so concurrent downgrade attempts cannot invert the
        # lock order, and keep the locks in Alembic's migration transaction.
        for table_name in _AUDIT_TABLES:
            connection.exec_driver_sql(
                f'LOCK TABLE "{table_name}" IN ACCESS EXCLUSIVE MODE'
            )
        return

    if dialect_name == "sqlite":
        # Alembic already owns the per-migration transaction, so issuing BEGIN
        # IMMEDIATE/EXCLUSIVE here can become an invalid nested BEGIN.  A write
        # statement with an impossible predicate changes no row but still makes
        # SQLite acquire its database-wide writer lock in that same transaction.
        # Consequently no connection can insert into any of the eight tables
        # between the emptiness proof and the final DROP.
        connection.exec_driver_sql(
            'UPDATE "audit_projects" '
            'SET "state_version" = "state_version" WHERE 1 = 0'
        )
        return

    raise RuntimeError(
        "Code Audit persistence downgrade cannot safely serialize writes for "
        f"database dialect {dialect_name!r}"
    )
