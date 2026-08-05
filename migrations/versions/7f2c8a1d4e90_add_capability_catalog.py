"""add versioned Capability catalog and learning candidates

Revision ID: 7f2c8a1d4e90
Revises: 6e4a2c9f1b30
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "7f2c8a1d4e90"
down_revision: str | Sequence[str] | None = "6e4a2c9f1b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def upgrade() -> None:
    op.create_table(
        "capabilities",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('tool', 'technique', 'skill', 'playbook', 'knowledge', 'eval_case')",
            name="ck_capabilities_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "capability_versions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("publisher", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "schema_version = 'riftx.capability/v1'",
            name="ck_capability_versions_schema",
        ),
        sa.CheckConstraint(
            "status IN ('approved', 'active', 'disabled', 'degraded', "
            "'deprecated', 'archived')",
            name="ck_capability_versions_status",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("manifest_digest"),
            name="ck_capability_versions_manifest_digest",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND activated_at IS NOT NULL) OR status <> 'active'",
            name="ck_capability_versions_active_time",
        ),
        sa.CheckConstraint(
            "retired_at IS NULL OR activated_at IS NOT NULL",
            name="ck_capability_versions_retired_requires_activation",
        ),
        sa.ForeignKeyConstraint(
            ["capability_id"],
            ["capabilities.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "capability_id",
            "version",
            name="uq_capability_versions_capability_version",
        ),
        sa.UniqueConstraint("manifest_digest"),
    )
    op.create_index(
        "ix_capability_versions_status",
        "capability_versions",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_capability_versions_capability_status",
        "capability_versions",
        ["capability_id", "status"],
        unique=False,
    )
    op.create_table(
        "capability_dependencies",
        sa.Column("version_id", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("reference", sa.String(length=256), nullable=False),
        sa.Column("version_constraint", sa.String(length=128), nullable=True),
        sa.Column("optional", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('tool', 'skill', 'capability', 'platform')",
            name="ck_capability_dependencies_kind",
        ),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_capability_dependencies_position",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["capability_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("version_id", "position"),
        sa.UniqueConstraint(
            "version_id",
            "kind",
            "reference",
            name="uq_capability_dependencies_identity",
        ),
    )
    op.create_table(
        "capability_permissions",
        sa.Column("version_id", sa.String(length=128), nullable=False),
        sa.Column("effect_class", sa.String(length=32), nullable=False),
        sa.Column("approval_level", sa.String(length=32), nullable=False),
        sa.Column("requires_scope", sa.Boolean(), nullable=False),
        sa.Column("credential_references_json", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "effect_class IN ('read_only', 'local_mutation', 'target_interaction', "
            "'code_execution', 'credential_access', 'external_service')",
            name="ck_capability_permissions_effect_class",
        ),
        sa.CheckConstraint(
            "approval_level IN ('never', 'sensitive', 'always')",
            name="ck_capability_permissions_approval_level",
        ),
        sa.CheckConstraint(
            "effect_class <> 'target_interaction' OR requires_scope = true",
            name="ck_capability_permissions_target_scope",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["capability_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("version_id"),
    )
    op.create_table(
        "capability_evidence_contracts",
        sa.Column("version_id", sa.String(length=128), nullable=False),
        sa.Column("required_refs_json", sa.JSON(), nullable=False),
        sa.Column("minimum_independent_sources", sa.Integer(), nullable=False),
        sa.Column("confirmation_policy", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "minimum_independent_sources >= 0",
            name="ck_capability_evidence_contracts_minimum_sources",
        ),
        sa.CheckConstraint(
            "confirmation_policy IN ('explicit_verification', 'independent_sources', "
            "'manual_review')",
            name="ck_capability_evidence_contracts_confirmation_policy",
        ),
        sa.CheckConstraint(
            "confirmation_policy <> 'independent_sources' "
            "OR minimum_independent_sources >= 2",
            name="ck_capability_evidence_contracts_independent_sources",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["capability_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("version_id"),
    )
    op.create_table(
        "capability_candidates",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("proposed_version", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("candidate_digest", sa.String(length=64), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("proposed_by", sa.String(length=256), nullable=False),
        sa.Column("source_run_id", sa.String(length=128), nullable=True),
        sa.Column("promoted_version_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('tool', 'technique', 'skill', 'playbook', 'knowledge', 'eval_case')",
            name="ck_capability_candidates_kind",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'tested', 'approved', 'rejected', 'promoted')",
            name="ck_capability_candidates_status",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("candidate_digest"),
            name="ck_capability_candidates_digest",
        ),
        sa.CheckConstraint(
            "(status = 'promoted' AND promoted_version_id IS NOT NULL) OR "
            "(status <> 'promoted' AND promoted_version_id IS NULL)",
            name="ck_capability_candidates_promotion_shape",
        ),
        sa.ForeignKeyConstraint(
            ["promoted_version_id"],
            ["capability_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "capability_id",
            "proposed_version",
            "candidate_digest",
            name="uq_capability_candidates_proposal",
        ),
    )
    op.create_index(
        "ix_capability_candidates_status_created",
        "capability_candidates",
        ["status", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "capability_promotion_runs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=256), nullable=False),
        sa.Column("approval_reference", sa.String(length=256), nullable=True),
        sa.Column("promoted_version_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'evaluating', 'waiting_approval', 'approved', "
            "'rejected', 'promoted', 'failed')",
            name="ck_capability_promotion_runs_status",
        ),
        sa.CheckConstraint(
            "status <> 'promoted' OR promoted_version_id IS NOT NULL",
            name="ck_capability_promotion_runs_promoted_version",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["capability_candidates.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["promoted_version_id"],
            ["capability_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_capability_promotion_runs_candidate",
        "capability_promotion_runs",
        ["candidate_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "capability_evaluation_results",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("promotion_id", sa.String(length=128), nullable=False),
        sa.Column("evaluator", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scenario_ids_json", sa.JSON(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("report_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'inconclusive')",
            name="ck_capability_evaluation_results_status",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("report_digest"),
            name="ck_capability_evaluation_results_digest",
        ),
        sa.ForeignKeyConstraint(
            ["promotion_id"],
            ["capability_promotion_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_capability_evaluation_results_promotion",
        "capability_evaluation_results",
        ["promotion_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "capability_packs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("pack_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("publisher", sa.String(length=256), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.capability-pack/v1'",
            name="ck_capability_packs_schema",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deprecated', 'archived')",
            name="ck_capability_packs_status",
        ),
        sa.CheckConstraint(
            "source IN ('official', 'operator', 'organization', 'engagement')",
            name="ck_capability_packs_source",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("manifest_digest"),
            name="ck_capability_packs_manifest_digest",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "pack_id",
            "version",
            name="uq_capability_packs_pack_version",
        ),
        sa.UniqueConstraint("manifest_digest"),
    )
    op.create_index(
        "ix_capability_packs_pack_status",
        "capability_packs",
        ["pack_id", "status"],
        unique=False,
    )
    op.create_table(
        "capability_pack_members",
        sa.Column("pack_version_id", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("capability_version_id", sa.String(length=128), nullable=False),
        sa.Column("capability_version", sa.String(length=128), nullable=False),
        sa.Column("capability_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            _lower_hex_digest_check("capability_digest"),
            name="ck_capability_pack_members_digest",
        ),
        sa.ForeignKeyConstraint(
            ["pack_version_id"],
            ["capability_packs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["capability_version_id"],
            ["capability_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("pack_version_id", "position"),
        sa.UniqueConstraint(
            "pack_version_id",
            "capability_id",
            name="uq_capability_pack_members_capability",
        ),
    )
    op.create_table(
        "capability_pack_installs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("pack_id", sa.String(length=128), nullable=False),
        sa.Column("pack_version_id", sa.String(length=128), nullable=False),
        sa.Column("pack_version", sa.String(length=128), nullable=False),
        sa.Column("pack_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("previous_pack_version_id", sa.String(length=128), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope_type IN ('official', 'operator', 'organization', 'engagement')",
            name="ck_capability_pack_installs_scope_type",
        ),
        sa.CheckConstraint(
            "status IN ('installed', 'disabled')",
            name="ck_capability_pack_installs_status",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("pack_digest"),
            name="ck_capability_pack_installs_pack_digest",
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name="ck_capability_pack_installs_state",
        ),
        sa.CheckConstraint(
            "(status = 'disabled' AND disabled_at IS NOT NULL) OR "
            "(status = 'installed' AND disabled_at IS NULL)",
            name="ck_capability_pack_installs_disabled_shape",
        ),
        sa.ForeignKeyConstraint(
            ["pack_version_id"],
            ["capability_packs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_pack_version_id"],
            ["capability_packs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "pack_id",
            name="uq_capability_pack_installs_scope_pack",
        ),
    )
    op.create_index(
        "ix_capability_pack_installs_scope_status",
        "capability_pack_installs",
        ["scope_type", "scope_id", "status"],
        unique=False,
    )
    op.create_table(
        "capability_pack_locks",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("capability_version_id", sa.String(length=128), nullable=False),
        sa.Column("capability_version", sa.String(length=128), nullable=False),
        sa.Column("capability_digest", sa.String(length=64), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "owner_kind IN ('pack_install', 'run_session')",
            name="ck_capability_pack_locks_owner_kind",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("capability_digest"),
            name="ck_capability_pack_locks_capability_digest",
        ),
        sa.CheckConstraint(
            "released_at IS NULL OR released_at >= acquired_at",
            name="ck_capability_pack_locks_release_time",
        ),
        sa.ForeignKeyConstraint(
            ["capability_version_id"],
            ["capability_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_capability_pack_locks_owner_active",
        "capability_pack_locks",
        ["owner_kind", "owner_id", "released_at"],
        unique=False,
    )
    op.create_index(
        "ix_capability_pack_locks_version_active",
        "capability_pack_locks",
        ["capability_version_id", "released_at"],
        unique=False,
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("offline downgrade cannot prove Capability tables are empty")
    connection = op.get_bind()
    tables = (
        "capability_pack_locks",
        "capability_pack_installs",
        "capability_pack_members",
        "capability_packs",
        "capability_evaluation_results",
        "capability_promotion_runs",
        "capability_candidates",
        "capability_evidence_contracts",
        "capability_permissions",
        "capability_dependencies",
        "capability_versions",
        "capabilities",
    )
    for table_name in tables:
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError(
                "cannot downgrade while durable Capability catalog facts exist"
            )
    _require_safe_cross_boundary_downgrade(connection)

    op.drop_index(
        "ix_capability_pack_locks_version_active",
        table_name="capability_pack_locks",
    )
    op.drop_index(
        "ix_capability_pack_locks_owner_active",
        table_name="capability_pack_locks",
    )
    op.drop_table("capability_pack_locks")
    op.drop_index(
        "ix_capability_pack_installs_scope_status",
        table_name="capability_pack_installs",
    )
    op.drop_table("capability_pack_installs")
    op.drop_table("capability_pack_members")
    op.drop_index("ix_capability_packs_pack_status", table_name="capability_packs")
    op.drop_table("capability_packs")
    op.drop_index(
        "ix_capability_evaluation_results_promotion",
        table_name="capability_evaluation_results",
    )
    op.drop_table("capability_evaluation_results")
    op.drop_index(
        "ix_capability_promotion_runs_candidate",
        table_name="capability_promotion_runs",
    )
    op.drop_table("capability_promotion_runs")
    op.drop_index(
        "ix_capability_candidates_status_created",
        table_name="capability_candidates",
    )
    op.drop_table("capability_candidates")
    op.drop_table("capability_evidence_contracts")
    op.drop_table("capability_permissions")
    op.drop_table("capability_dependencies")
    op.drop_index(
        "ix_capability_versions_capability_status",
        table_name="capability_versions",
    )
    op.drop_index("ix_capability_versions_status", table_name="capability_versions")
    op.drop_table("capability_versions")
    op.drop_table("capabilities")


def _require_safe_cross_boundary_downgrade(connection: sa.Connection) -> None:
    """Run lower-revision loss guards before this revision performs any DDL."""

    target = context.get_revision_argument()
    if target == down_revision:
        return
    for table_name in (
        "audit_static_effect_plans",
        "snapshot_mount_leases",
        "snapshot_mount_pins",
        "snapshot_mount_stop_proofs",
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError(
                "cannot downgrade while durable static effect authority facts exist"
            )
    if target == "8a1f3c5e7b90":
        return
    if connection.execute(sa.text("SELECT 1 FROM snapshot_references LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while durable Snapshot reference facts exist")
    if target == "5d8c1a7e3b24":
        return
    if connection.execute(sa.text("SELECT 1 FROM audit_preflight_plans LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while durable Audit Preflight Plan facts exist"
        )
    if connection.execute(
        sa.text(
            "SELECT 1 FROM audit_preflight_jobs "
            "WHERE plan_issuance_schema_version = "
            "'riftx.audit-preflight-plan-issuance/v1' LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "cannot downgrade while durable Audit Preflight facts exist: "
            "Plan-eligible Jobs remain"
        )
    if target == "2b7d9e4a6c10":
        return
    capability = connection.execute(
        sa.text(
            "SELECT 1 FROM runner_credentials "
            "WHERE CAST(protocol_capabilities_json AS TEXT) "
            "LIKE '%\"preflight\\_job\\_owner\\_v1\"%' ESCAPE '\\' LIMIT 1"
        )
    ).first()
    if capability is not None:
        raise RuntimeError(
            "cannot downgrade while Audit Preflight Runner capability facts exist"
        )
    for table_name in (
        "audit_preflight_stop_receipts",
        "audit_preflight_exit_receipts",
        "audit_preflight_results",
        "audit_preflight_job_requests",
        "audit_preflight_jobs",
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError("cannot downgrade while durable Audit Preflight facts exist")
    if target == "4f9a6c1d2e30":
        return
    if connection.execute(
        sa.text("SELECT 1 FROM workflow_signal_intents LIMIT 1")
    ).first():
        raise RuntimeError("cannot downgrade while durable Workflow signal intents exist")
    if target == "8d7c2e4f1a90":
        return
    _require_safe_runner_ownership_downgrade(connection)


def _require_safe_runner_ownership_downgrade(connection: sa.Connection) -> None:
    unsafe = connection.execute(
        sa.text(
            "SELECT 1 FROM runner_command_ownerships WHERE "
            "verification_state <> 'quarantined' OR schema_version IS NOT NULL OR "
            "effect_binding_id IS NOT NULL OR operation IS NOT NULL OR "
            "operation_family IS NOT NULL OR payload_digest IS NOT NULL OR "
            "output_contract_json IS NOT NULL OR output_contract_digest IS NOT NULL OR "
            "envelope_digest IS NOT NULL OR reconciliation_state <> 'untouched' OR "
            "replacement_command_id IS NOT NULL LIMIT 1"
        )
    ).first()
    if unsafe is not None:
        raise RuntimeError(
            "cannot downgrade Runner ownership after verified or reconciled ownership facts exist"
        )
    if connection.execute(
        sa.text("SELECT 1 FROM runner_commands WHERE state_version <> 0 LIMIT 1")
    ).first():
        raise RuntimeError(
            "cannot downgrade Runner ownership while post-migration command state exists"
        )
    if connection.execute(
        sa.text(
            "SELECT 1 FROM runner_credentials "
            "WHERE protocol_capabilities_json IS NOT NULL "
            "AND CAST(protocol_capabilities_json AS TEXT) NOT IN ('[]', 'null') LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "cannot downgrade Runner ownership while protocol capability facts exist"
        )
    for table_name in (
        "runner_effect_bindings",
        "runner_stop_receipts",
        "runner_stop_projections",
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError(
                "cannot downgrade Runner ownership while effect or stop proof facts exist"
            )
    if connection.execute(
        sa.text(
            "SELECT 1 FROM executions "
            "WHERE audit_id IS NOT NULL OR plan_digest IS NOT NULL OR "
            "runner_command_id IS NOT NULL OR runner_effect_binding_id IS NOT NULL OR "
            "runner_binding_digest IS NOT NULL OR runner_envelope_digest IS NOT NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "cannot downgrade Runner ownership while Audit Execution bindings exist"
        )
