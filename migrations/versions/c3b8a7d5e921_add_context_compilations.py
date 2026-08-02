"""add durable Context compilation manifests and token usage

Revision ID: c3b8a7d5e921
Revises: a4d7e2c19b63
Create Date: 2026-07-30 14:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3b8a7d5e921"
down_revision: str | None = "a4d7e2c19b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_compilations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("model_profile", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("actual_input_tokens", sa.Integer(), nullable=True),
        sa.Column("actual_output_tokens", sa.Integer(), nullable=True),
        sa.Column("loaded_memory_ids_json", sa.JSON(), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_context_compilations_run_id",
        "context_compilations",
        ["run_id"],
    )
    op.create_index(
        "ix_context_compilations_session_id",
        "context_compilations",
        ["session_id"],
    )
    op.create_index(
        "ix_context_compilations_agent_id",
        "context_compilations",
        ["agent_id"],
    )
    op.create_index(
        "ix_context_compilations_purpose",
        "context_compilations",
        ["purpose"],
    )
    op.create_index(
        "ix_context_compilations_checkpoint_id",
        "context_compilations",
        ["checkpoint_id"],
    )
    op.create_index(
        "ix_context_compilations_created_at",
        "context_compilations",
        ["created_at"],
    )
    op.create_index(
        "ix_context_compilations_session_created",
        "context_compilations",
        ["session_id", "created_at"],
    )
    op.create_index(
        "ix_context_compilations_run_created",
        "context_compilations",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_context_compilations_run_created",
        table_name="context_compilations",
    )
    op.drop_index(
        "ix_context_compilations_session_created",
        table_name="context_compilations",
    )
    op.drop_index(
        "ix_context_compilations_created_at",
        table_name="context_compilations",
    )
    op.drop_index(
        "ix_context_compilations_checkpoint_id",
        table_name="context_compilations",
    )
    op.drop_index(
        "ix_context_compilations_purpose",
        table_name="context_compilations",
    )
    op.drop_index(
        "ix_context_compilations_agent_id",
        table_name="context_compilations",
    )
    op.drop_index(
        "ix_context_compilations_session_id",
        table_name="context_compilations",
    )
    op.drop_index(
        "ix_context_compilations_run_id",
        table_name="context_compilations",
    )
    op.drop_table("context_compilations")
