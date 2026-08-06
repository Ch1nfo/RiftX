"""Add the immutable Evidence Ledger.

Revision ID: 5e8a2c4d7f10
Revises: 4d7f1a8c2e90
Create Date: 2026-08-06 18:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "5e8a2c4d7f10"
down_revision = "4d7f1a8c2e90"
branch_labels = None
depends_on = None


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def upgrade() -> None:
    op.create_table(
        "evidence_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("ledger_digest", sa.String(length=64), nullable=False),
        sa.Column("creator_type", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("trust_class", sa.String(length=32), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("redaction_status", sa.String(length=32), nullable=False),
        sa.Column("redaction_policy_ref", sa.Text(), nullable=True),
        sa.Column("replay_json", sa.JSON(), nullable=False),
        sa.Column("locator_json", sa.JSON(), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.evidence/v1'",
            name="ck_evidence_schema_version",
        ),
        sa.CheckConstraint(
            "kind IN ('execution_output', 'artifact_span', 'http_request_response', "
            "'browser_observation', 'code_location', 'code_flow', 'scanner_signal', "
            "'user_decision', 'deterministic_parser_result', 'external_research_source')",
            name="ck_evidence_kind",
        ),
        sa.CheckConstraint(
            "creator_type IN ('agent', 'operator', 'system', 'tool', 'parser', 'scanner')",
            name="ck_evidence_creator_type",
        ),
        sa.CheckConstraint(
            "trust_class IN ('generated', 'user_provided', 'untrusted_source', "
            "'untrusted_tool_output')",
            name="ck_evidence_trust_class",
        ),
        sa.CheckConstraint(
            "redaction_status IN ('not_required', 'redacted', 'restricted', 'metadata_only')",
            name="ck_evidence_redaction_status",
        ),
        sa.CheckConstraint(
            "(redaction_status = 'redacted' AND redaction_policy_ref IS NOT NULL) OR "
            "(redaction_status = 'not_required' AND redaction_policy_ref IS NULL) OR "
            "redaction_status IN ('restricted', 'metadata_only')",
            name="ck_evidence_redaction_shape",
        ),
        sa.CheckConstraint(_lower_hex_digest_check("digest"), name="ck_evidence_digest"),
        sa.CheckConstraint(
            _lower_hex_digest_check("ledger_digest"),
            name="ck_evidence_ledger_digest",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ledger_digest", name="uq_evidence_ledger_digest"),
    )
    op.create_index("ix_evidence_digest", "evidence_ledger", ["digest"], unique=False)
    op.create_index(
        "ix_evidence_session_id",
        "evidence_ledger",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_artifact_id",
        "evidence_ledger",
        ["artifact_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_run_created_id",
        "evidence_ledger",
        ["run_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_run_task_created",
        "evidence_ledger",
        ["run_id", "task_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_run_kind_created",
        "evidence_ledger",
        ["run_id", "kind", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("offline downgrade cannot prove durable Evidence state is empty")
    connection = op.get_bind()
    _require_safe_cross_boundary_downgrade(connection)
    if connection.execute(sa.text("SELECT 1 FROM evidence_ledger LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while durable Evidence facts exist")
    op.drop_index("ix_evidence_run_kind_created", table_name="evidence_ledger")
    op.drop_index("ix_evidence_run_task_created", table_name="evidence_ledger")
    op.drop_index("ix_evidence_run_created_id", table_name="evidence_ledger")
    op.drop_index("ix_evidence_artifact_id", table_name="evidence_ledger")
    op.drop_index("ix_evidence_session_id", table_name="evidence_ledger")
    op.drop_index("ix_evidence_digest", table_name="evidence_ledger")
    op.drop_table("evidence_ledger")


def _require_safe_cross_boundary_downgrade(connection: sa.Connection) -> None:
    """Run every lower-revision loss guard before this revision performs DDL."""

    target = context.get_revision_argument()
    if target == down_revision:
        return
    for table_name in (
        "task_evidence_requirements",
        "task_budgets",
        "task_attempts",
        "task_dependencies",
        "tasks",
        "task_graphs",
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError("cannot downgrade while durable Task Graph facts exist")
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
