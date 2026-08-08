"""Add the durable Task Graph aggregate.

Revision ID: 4d7f1a8c2e90
Revises: 6c8e4a2f1b70
Create Date: 2026-08-06 07:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "4d7f1a8c2e90"
down_revision = "6c8e4a2f1b70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_graphs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_task_graphs_version"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_task_graphs_updated_at", "task_graphs", ["updated_at"], unique=False)

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("parent_task_id", sa.String(length=64), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_scope_json", sa.JSON(), nullable=False),
        sa.Column("expected_output_schema_json", sa.JSON(), nullable=False),
        sa.Column("required_capability_ids_json", sa.JSON(), nullable=False),
        sa.Column("workspace_owner", sa.String(length=128), nullable=True),
        sa.Column("session_owner_id", sa.String(length=64), nullable=True),
        sa.Column("stop_condition", sa.Text(), nullable=True),
        sa.Column("completion_summary", sa.Text(), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("reopen_history_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'blocked', 'completed', 'failed', 'cancelled')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_tasks_sequence"),
        sa.CheckConstraint("version >= 1", name="ck_tasks_version"),
        sa.CheckConstraint(
            "parent_task_id IS NULL OR parent_task_id <> id",
            name="ck_tasks_parent",
        ),
        sa.CheckConstraint(
            "(status = 'blocked' AND blocked_reason IS NOT NULL) OR status <> 'blocked'",
            name="ck_tasks_blocked_reason",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completion_summary IS NOT NULL AND completed_at IS NOT NULL) "
            "OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_tasks_completion_shape",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["task_graphs.run_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "parent_task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "id", name="uq_tasks_run_id_id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_tasks_run_sequence"),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"], unique=False)
    op.create_index("ix_tasks_updated_at", "tasks", ["updated_at"], unique=False)
    op.create_index(
        "ix_tasks_run_status_sequence",
        "tasks",
        ["run_id", "status", "sequence"],
        unique=False,
    )

    op.create_table(
        "task_dependencies",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("depends_on_task_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "task_id <> depends_on_task_id",
            name="ck_task_dependencies_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "depends_on_task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "task_id", "depends_on_task_id"),
    )

    op.create_table(
        "task_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("retry_of_attempt_id", sa.String(length=64), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="ck_task_attempts_status",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_task_attempts_sequence"),
        sa.CheckConstraint(
            "retry_of_attempt_id IS NULL OR retry_of_attempt_id <> id",
            name="ck_task_attempts_retry",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND failure_summary IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND failure_summary IS NOT NULL) OR "
            "(status IN ('succeeded', 'cancelled') AND finished_at IS NOT NULL "
            "AND failure_summary IS NULL)",
            name="ck_task_attempts_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "task_id", "retry_of_attempt_id"],
            ["task_attempts.run_id", "task_attempts.task_id", "task_attempts.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "task_id", "id", name="uq_task_attempts_owner_id"),
        sa.UniqueConstraint(
            "run_id",
            "task_id",
            "sequence",
            name="uq_task_attempts_owner_sequence",
        ),
    )
    op.create_index("ix_task_attempts_task_id", "task_attempts", ["task_id"], unique=False)
    op.create_index("ix_task_attempts_status", "task_attempts", ["status"], unique=False)
    op.create_index("ix_task_attempts_session_id", "task_attempts", ["session_id"], unique=False)
    op.create_index("ix_task_attempts_worker_id", "task_attempts", ["worker_id"], unique=False)
    op.create_index(
        "ix_task_attempts_run_status",
        "task_attempts",
        ["run_id", "status"],
        unique=False,
    )

    op.create_table(
        "task_budgets",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("max_model_calls", sa.Integer(), nullable=True),
        sa.Column("max_tool_calls", sa.Integer(), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("max_duration_seconds", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_model_calls IS NOT NULL OR max_tool_calls IS NOT NULL OR "
            "max_tokens IS NOT NULL OR max_duration_seconds IS NOT NULL",
            name="ck_task_budgets_nonempty",
        ),
        sa.CheckConstraint(
            "max_model_calls IS NULL OR max_model_calls >= 1",
            name="ck_task_budgets_model_calls",
        ),
        sa.CheckConstraint(
            "max_tool_calls IS NULL OR max_tool_calls >= 1",
            name="ck_task_budgets_tool_calls",
        ),
        sa.CheckConstraint(
            "max_tokens IS NULL OR max_tokens >= 1",
            name="ck_task_budgets_tokens",
        ),
        sa.CheckConstraint(
            "max_duration_seconds IS NULL OR max_duration_seconds > 0",
            name="ck_task_budgets_duration",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "task_id"),
    )

    op.create_table(
        "task_evidence_requirements",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_type", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("minimum_count", sa.Integer(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "minimum_count >= 1",
            name="ck_task_evidence_minimum_count",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["tasks.run_id", "tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "task_id",
            "id",
            name="uq_task_evidence_requirements_owner_id",
        ),
    )
    op.create_index(
        "ix_task_evidence_requirements_run_task",
        "task_evidence_requirements",
        ["run_id", "task_id"],
        unique=False,
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("offline downgrade cannot prove durable Task Graph state is empty")
    connection = op.get_bind()
    _require_safe_cross_boundary_downgrade(connection)
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

    op.drop_index(
        "ix_task_evidence_requirements_run_task",
        table_name="task_evidence_requirements",
    )
    op.drop_table("task_evidence_requirements")
    op.drop_table("task_budgets")
    op.drop_index("ix_task_attempts_run_status", table_name="task_attempts")
    op.drop_index("ix_task_attempts_worker_id", table_name="task_attempts")
    op.drop_index("ix_task_attempts_session_id", table_name="task_attempts")
    op.drop_index("ix_task_attempts_status", table_name="task_attempts")
    op.drop_index("ix_task_attempts_task_id", table_name="task_attempts")
    op.drop_table("task_attempts")
    op.drop_table("task_dependencies")
    op.drop_index("ix_tasks_run_status_sequence", table_name="tasks")
    op.drop_index("ix_tasks_updated_at", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_task_graphs_updated_at", table_name="task_graphs")
    op.drop_table("task_graphs")


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
