"""add durable structured Working Memory with optimistic versioning

Revision ID: d9f4a6c2b731
Revises: c3b8a7d5e921
Create Date: 2026-07-30 15:25:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9f4a6c2b731"
down_revision: str | None = "c3b8a7d5e921"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "working_memories",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_working_memories_run_id"),
    )
    op.create_index(
        "ix_working_memories_run_id",
        "working_memories",
        ["run_id"],
        unique=True,
    )
    op.create_index(
        "ix_working_memories_updated_at",
        "working_memories",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_working_memories_updated_at", table_name="working_memories")
    op.drop_index("ix_working_memories_run_id", table_name="working_memories")
    op.drop_table("working_memories")
