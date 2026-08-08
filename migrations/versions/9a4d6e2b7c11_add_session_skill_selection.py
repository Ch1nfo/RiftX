"""Add durable Session-scoped Progressive Skill selection.

Revision ID: 9a4d6e2b7c11
Revises: 7f2c8a1d4e90
Create Date: 2026-08-05 18:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "9a4d6e2b7c11"
down_revision = "7f2c8a1d4e90"
branch_labels = None
depends_on = None


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def upgrade() -> None:
    op.create_table(
        "agent_skill_scopes",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("allowed_skill_ids_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_agent_skill_scopes_run_id",
        "agent_skill_scopes",
        ["run_id"],
        unique=False,
    )
    op.create_table(
        "agent_skill_selections",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("skill_digest", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("document_json", sa.JSON(), nullable=False),
        sa.Column("reference_json", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("references_loaded", sa.Boolean(), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('official', 'operator', 'organization', 'engagement')",
            name="ck_agent_skill_selections_source",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("skill_digest"),
            name="ck_agent_skill_selections_digest",
        ),
        sa.CheckConstraint(
            "(active = 1 AND unloaded_at IS NULL) OR "
            "(active = 0 AND unloaded_at IS NOT NULL)",
            name="ck_agent_skill_selections_active_shape",
        ),
        sa.CheckConstraint(
            "references_loaded = 0 OR reference_json IS NOT NULL",
            name="ck_agent_skill_selections_reference_shape",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id", "skill_id"),
    )
    op.create_index(
        "ix_agent_skill_selections_run_active",
        "agent_skill_selections",
        ["run_id", "active", "session_id"],
        unique=False,
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("offline downgrade cannot prove Agent Skill tables are empty")
    connection = op.get_bind()
    for table_name in ("agent_skill_selections", "agent_skill_scopes"):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError(
                "cannot downgrade while durable Agent Skill Session facts exist"
            )
    _require_safe_cross_boundary_downgrade(connection)
    op.drop_index(
        "ix_agent_skill_selections_run_active",
        table_name="agent_skill_selections",
    )
    op.drop_table("agent_skill_selections")
    op.drop_index("ix_agent_skill_scopes_run_id", table_name="agent_skill_scopes")
    op.drop_table("agent_skill_scopes")


def _require_safe_cross_boundary_downgrade(connection: sa.Connection) -> None:
    """Run every lower-revision loss guard before this revision performs DDL."""

    target = context.get_revision_argument()
    if target == down_revision:
        return
    for table_name in (
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
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError(
                "cannot downgrade while durable Capability catalog facts exist"
            )
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
