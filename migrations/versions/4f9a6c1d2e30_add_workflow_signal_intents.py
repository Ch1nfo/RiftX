"""add durable owner-bound Workflow signal intents

Revision ID: 4f9a6c1d2e30
Revises: 8d7c2e4f1a90
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f9a6c1d2e30"
down_revision: str | Sequence[str] | None = "8d7c2e4f1a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workflow_signal_intents"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_identity", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("run_kind", sa.String(length=32), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=True),
        sa.Column("workflow_protocol_version", sa.String(length=128), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("signal_kind", sa.String(length=64), nullable=False),
        sa.Column("source_event_kind", sa.String(length=64), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=False),
        sa.Column("source_state_version", sa.Integer(), nullable=False),
        sa.Column("identity_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("delivery_state", sa.String(length=32), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_receipt_digest", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.workflow-signal-intent/v1'",
            name="ck_workflow_signal_intents_schema",
        ),
        sa.CheckConstraint(
            "owner_kind IN ('general_run', 'code_audit')",
            name="ck_workflow_signal_intents_owner_kind",
        ),
        sa.CheckConstraint(
            "run_kind IN ('general', 'code_audit')",
            name="ck_workflow_signal_intents_run_kind",
        ),
        sa.CheckConstraint(
            "(owner_kind = 'general_run' AND run_kind = 'general' "
            "AND audit_id IS NULL AND owner_identity = 'general_run:' || run_id "
            "AND workflow_protocol_version = 'riftx.general-run-workflow/v1' "
            "AND workflow_id NOT LIKE 'riftx-code-audit-%') OR "
            "(owner_kind = 'code_audit' AND run_kind = 'code_audit' "
            "AND audit_id IS NOT NULL AND owner_identity = 'code_audit:' || audit_id "
            "AND workflow_protocol_version = 'riftx.code-audit-workflow/v1' "
            "AND workflow_id = 'riftx-code-audit-' || audit_id)",
            name="ck_workflow_signal_intents_owner_binding",
        ),
        sa.CheckConstraint(
            "signal_kind IN ('pause', 'resume', 'cancel', 'approve', 'reject', "
            "'execution_completed', 'safety_reconcile')",
            name="ck_workflow_signal_intents_signal_kind",
        ),
        sa.CheckConstraint(
            "source_event_kind IN ('control_intent', 'approval_decision', "
            "'execution_terminal', 'safety_reconciliation')",
            name="ck_workflow_signal_intents_source_kind",
        ),
        sa.CheckConstraint(
            "(source_event_kind = 'control_intent' "
            "AND signal_kind IN ('pause', 'resume', 'cancel')) OR "
            "(source_event_kind = 'approval_decision' "
            "AND signal_kind IN ('approve', 'reject')) OR "
            "(source_event_kind = 'execution_terminal' "
            "AND signal_kind = 'execution_completed') OR "
            "(source_event_kind = 'safety_reconciliation' "
            "AND signal_kind = 'safety_reconcile')",
            name="ck_workflow_signal_intents_source_signal",
        ),
        sa.CheckConstraint(
            "delivery_state IN ('pending', 'claimed', 'delivered', "
            "'observed_delivered', 'retryable', 'outcome_unknown', 'superseded')",
            name="ck_workflow_signal_intents_delivery_state",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_workflow_signal_intents_lease_pair",
        ),
        sa.CheckConstraint(
            "(delivery_state = 'claimed' AND lease_owner IS NOT NULL) OR "
            "(delivery_state = 'outcome_unknown') OR "
            "(delivery_state NOT IN ('claimed', 'outcome_unknown') "
            "AND lease_owner IS NULL)",
            name="ck_workflow_signal_intents_lease_state",
        ),
        sa.CheckConstraint(
            "(delivery_state IN ('delivered', 'observed_delivered') "
            "AND delivery_receipt_digest IS NOT NULL AND delivered_at IS NOT NULL "
            "AND next_attempt_at IS NULL AND last_error_code IS NULL) OR "
            "(delivery_state NOT IN ('delivered', 'observed_delivered') "
            "AND delivery_receipt_digest IS NULL AND delivered_at IS NULL)",
            name="ck_workflow_signal_intents_receipt_state",
        ),
        sa.CheckConstraint(
            "(delivery_state IN ('pending', 'retryable', 'outcome_unknown') "
            "AND next_attempt_at IS NOT NULL) OR "
            "(delivery_state NOT IN ('pending', 'retryable', 'outcome_unknown') "
            "AND next_attempt_at IS NULL)",
            name="ck_workflow_signal_intents_schedule_state",
        ),
        sa.CheckConstraint(
            "(delivery_state = 'pending' AND attempt = 0) OR "
            "(delivery_state <> 'pending' AND attempt >= 1)",
            name="ck_workflow_signal_intents_attempt_state",
        ),
        sa.CheckConstraint(
            "delivery_state NOT IN ('retryable', 'outcome_unknown', 'superseded') "
            "OR last_error_code IS NOT NULL",
            name="ck_workflow_signal_intents_error_state",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("identity_digest"),
            name="ck_workflow_signal_intents_identity_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("payload_digest"),
            name="ck_workflow_signal_intents_payload_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("delivery_receipt_digest"),
            name="ck_workflow_signal_intents_receipt_digest",
        ),
        sa.CheckConstraint(
            "source_state_version >= 1 AND state_version >= 1 AND attempt >= 0",
            name="ck_workflow_signal_intents_versions",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at AND "
            "(delivered_at IS NULL OR delivered_at >= created_at) AND "
            "(lease_expires_at IS NULL OR lease_expires_at > updated_at)",
            name="ck_workflow_signal_intents_timestamp_order",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_workflow_signal_intents_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audit_scans.id"],
            name="fk_workflow_signal_intents_audit",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "identity_digest",
            name="uq_workflow_signal_intents_identity_digest",
        ),
        sa.UniqueConstraint(
            "owner_identity",
            "workflow_protocol_version",
            "workflow_id",
            "signal_kind",
            "source_event_kind",
            "source_event_id",
            "source_state_version",
            name="uq_workflow_signal_intents_source_identity",
        ),
    )
    op.create_index(
        "ix_workflow_signal_intents_delivery_schedule",
        _TABLE,
        ["delivery_state", "next_attempt_at", "created_at", "id"],
    )
    op.create_index(
        "ix_workflow_signal_intents_lease",
        _TABLE,
        ["delivery_state", "lease_expires_at", "id"],
    )
    op.create_index(
        "ix_workflow_signal_intents_run_owner",
        _TABLE,
        ["run_id", "owner_kind", "created_at", "id"],
    )
    op.create_index(
        "ix_workflow_signal_intents_audit_owner",
        _TABLE,
        ["audit_id", "created_at", "id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM workflow_signal_intents LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while durable Workflow signal intents exist"
        )
    op.drop_table(_TABLE)


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def _optional_lower_hex_digest_check(column: str) -> str:
    return f"{column} IS NULL OR ({_lower_hex_digest_check(column)})"
