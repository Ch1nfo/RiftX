"""add provider-neutral Context Checkpoints

Revision ID: c1d4e6f8a203
Revises: a9c3e5f7b102
Create Date: 2026-07-30 20:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d4e6f8a203"
down_revision: str | None = "a9c3e5f7b102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_checkpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_type", sa.String(length=64), nullable=False),
        sa.Column("compaction_stage", sa.String(length=64), nullable=False),
        sa.Column("model_profile", sa.String(length=255), nullable=False),
        sa.Column("working_memory_version", sa.Integer(), nullable=True),
        sa.Column("provider_state_id", sa.String(length=64), nullable=True),
        sa.Column("context_compilation_id", sa.String(length=64), nullable=True),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["context_compilation_id"],
            ["context_compilations.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "run_id",
        "session_id",
        "checkpoint_type",
        "compaction_stage",
        "model_profile",
        "provider_state_id",
        "context_compilation_id",
        "created_at",
    ):
        op.create_index(
            f"ix_context_checkpoints_{column}",
            "context_checkpoints",
            [column],
        )
    op.create_index(
        "ix_context_checkpoints_session_created",
        "context_checkpoints",
        ["session_id", "created_at"],
    )
    op.create_index(
        "ix_context_checkpoints_run_created",
        "context_checkpoints",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("context_checkpoints")
