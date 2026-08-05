"""add simplified single-machine local Audit jobs

Revision ID: 6e4a2c9f1b30
Revises: d0b4e6f8a102
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "6e4a2c9f1b30"
down_revision: str | Sequence[str] | None = "d0b4e6f8a102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _optional_lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"{column} IS NULL OR (length({column}) = 64 AND length({remainder}) = 0)"


def upgrade() -> None:
    op.create_table(
        "local_audit_jobs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("include_paths_json", sa.JSON(), nullable=False),
        sa.Column("exclude_paths_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("source_identity_digest", sa.String(length=64), nullable=True),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=True),
        sa.Column("manifest_digest", sa.String(length=64), nullable=True),
        sa.Column("inventory_digest", sa.String(length=64), nullable=True),
        sa.Column("detector_run_digest", sa.String(length=64), nullable=True),
        sa.Column("report_digest", sa.String(length=64), nullable=True),
        sa.Column("total_files", sa.Integer(), nullable=False),
        sa.Column("scanned_files", sa.Integer(), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("findings_json", sa.JSON(), nullable=False),
        sa.Column("json_report", sa.Text(), nullable=True),
        sa.Column("markdown_report", sa.Text(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'queued', 'scanning', 'completed', 'failed', 'cancelled')",
            name="ck_local_audit_jobs_status",
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name="ck_local_audit_jobs_state_version",
        ),
        sa.CheckConstraint(
            "total_files >= 0 AND scanned_files >= 0 AND finding_count >= 0 "
            "AND scanned_files <= total_files",
            name="ck_local_audit_jobs_nonnegative_counts",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("source_identity_digest"),
            name="ck_local_audit_jobs_source_identity_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("snapshot_digest"),
            name="ck_local_audit_jobs_snapshot_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("manifest_digest"),
            name="ck_local_audit_jobs_manifest_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("inventory_digest"),
            name="ck_local_audit_jobs_inventory_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("detector_run_digest"),
            name="ck_local_audit_jobs_detector_run_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("report_digest"),
            name="ck_local_audit_jobs_report_digest",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND source_identity_digest IS NOT NULL "
            "AND snapshot_digest IS NOT NULL AND manifest_digest IS NOT NULL "
            "AND inventory_digest IS NOT NULL AND detector_run_digest IS NOT NULL "
            "AND report_digest IS NOT NULL AND json_report IS NOT NULL "
            "AND markdown_report IS NOT NULL AND scanned_files = total_files "
            "AND failure_code IS NULL AND finished_at IS NOT NULL) OR status <> 'completed'",
            name="ck_local_audit_jobs_completed_result",
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL AND finished_at IS NOT NULL) "
            "OR status <> 'failed'",
            name="ck_local_audit_jobs_failed_result",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND finished_at IS NOT NULL) OR status <> 'cancelled'",
            name="ck_local_audit_jobs_cancelled_result",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_local_audit_jobs_timestamp_order",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_local_audit_jobs_status",
        "local_audit_jobs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_local_audit_jobs_status_created_id",
        "local_audit_jobs",
        ["status", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot prove that local Audit Job facts are absent"
        )
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM local_audit_jobs LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while durable local Audit Job facts exist")
    _require_safe_cross_boundary_downgrade(connection)
    op.drop_index(
        "ix_local_audit_jobs_status_created_id",
        table_name="local_audit_jobs",
    )
    op.drop_index("ix_local_audit_jobs_status", table_name="local_audit_jobs")
    op.drop_table("local_audit_jobs")


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
