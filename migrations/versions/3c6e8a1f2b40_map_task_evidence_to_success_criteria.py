"""Map Task Evidence Requirements to Run Success Criteria.

Revision ID: 3c6e8a1f2b40
Revises: 8b1d3f5a7c20
Create Date: 2026-08-06 23:45:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "3c6e8a1f2b40"
down_revision = "8b1d3f5a7c20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_evidence_requirements") as batch:
        batch.add_column(sa.Column("success_criterion_index", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_task_evidence_success_criterion_index",
            "success_criterion_index IS NULL OR success_criterion_index >= 0",
        )
    op.create_index(
        "ix_task_evidence_requirements_run_criterion",
        "task_evidence_requirements",
        ["run_id", "success_criterion_index"],
        unique=False,
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("offline downgrade cannot prove criterion mappings are empty")
    connection = op.get_bind()
    _require_safe_cross_boundary_downgrade(connection)
    if connection.execute(
        sa.text(
            "SELECT 1 FROM task_evidence_requirements "
            "WHERE success_criterion_index IS NOT NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError("cannot downgrade while Success Criterion mappings exist")
    op.drop_index(
        "ix_task_evidence_requirements_run_criterion",
        table_name="task_evidence_requirements",
    )
    with op.batch_alter_table("task_evidence_requirements") as batch:
        batch.drop_constraint("ck_task_evidence_success_criterion_index", type_="check")
        batch.drop_column("success_criterion_index")


def _require_safe_cross_boundary_downgrade(connection: sa.Connection) -> None:
    target = context.get_revision_argument()
    if target == down_revision:
        return
    if connection.execute(sa.text("SELECT 1 FROM reasoning_graphs LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while durable Reasoning Graph facts exist")
    if connection.execute(sa.text("SELECT 1 FROM evidence_ledger LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while durable Evidence facts exist")
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
            raise RuntimeError("cannot downgrade while durable Capability catalog facts exist")
    for table_name in (
        "audit_static_effect_plans",
        "snapshot_mount_leases",
        "snapshot_mount_pins",
        "snapshot_mount_stop_proofs",
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError("cannot downgrade while durable static effect authority facts exist")
    if target == "8a1f3c5e7b90":
        return
    if connection.execute(sa.text("SELECT 1 FROM snapshot_references LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while durable Snapshot reference facts exist")
    if target == "5d8c1a7e3b24":
        return
    if connection.execute(sa.text("SELECT 1 FROM audit_preflight_plans LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while durable Audit Preflight Plan facts exist")
    if connection.execute(
        sa.text(
            "SELECT 1 FROM audit_preflight_jobs "
            "WHERE plan_issuance_schema_version = "
            "'riftx.audit-preflight-plan-issuance/v1' LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "cannot downgrade while durable Audit Preflight facts exist: Plan-eligible Jobs remain"
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
        raise RuntimeError("cannot downgrade while Audit Preflight Runner capability facts exist")
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
    if connection.execute(sa.text("SELECT 1 FROM workflow_signal_intents LIMIT 1")).first():
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
        raise RuntimeError("cannot downgrade Runner ownership while Audit Execution bindings exist")
