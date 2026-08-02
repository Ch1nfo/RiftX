"""add Runtime approval snapshots and user input requests

Revision ID: f8a2c4d6e910
Revises: b7e1d2c3f4a5
Create Date: 2026-07-30 17:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8a2c4d6e910"
down_revision: str | None = "b7e1d2c3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_call_intents",
        sa.Column("execution_spec_json", sa.JSON(), nullable=True),
    )
    op.create_table(
        "runtime_approval_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), nullable=False),
        sa.Column("tool_call_intent_id", sa.String(length=64), nullable=False),
        sa.Column("context_compilation_id", sa.String(length=64), nullable=True),
        sa.Column("working_memory_version", sa.Integer(), nullable=True),
        sa.Column("provider_state_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=64), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["cycle_id"], ["agent_cycles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tool_call_intent_id"], ["tool_call_intents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["context_compilation_id"],
            ["context_compilations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["provider_state_id"], ["provider_states.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tool_call_intent_id"),
    )
    for column in (
        "run_id",
        "session_id",
        "cycle_id",
        "tool_call_intent_id",
        "context_compilation_id",
        "provider_state_id",
        "status",
    ):
        op.create_index(
            f"ix_runtime_approval_requests_{column}",
            "runtime_approval_requests",
            [column],
        )

    op.create_table(
        "user_input_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("context_compilation_id", sa.String(length=64), nullable=True),
        sa.Column("working_memory_version", sa.Integer(), nullable=True),
        sa.Column("provider_state_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("response_message_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["cycle_id"], ["agent_cycles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["context_compilation_id"],
            ["context_compilations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["provider_state_id"], ["provider_states.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["response_message_id"], ["agent_messages.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cycle_id"),
    )
    for column in (
        "run_id",
        "session_id",
        "cycle_id",
        "context_compilation_id",
        "provider_state_id",
        "status",
        "response_message_id",
    ):
        op.create_index(
            f"ix_user_input_requests_{column}",
            "user_input_requests",
            [column],
        )


def downgrade() -> None:
    op.drop_table("user_input_requests")
    op.drop_table("runtime_approval_requests")
    op.drop_column("tool_call_intents", "execution_spec_json")
