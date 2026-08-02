"""add scope-aware long-term memories

Revision ID: e2f5a7c9d104
Revises: c1d4e6f8a203
Create Date: 2026-07-30 22:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f5a7c9d104"
down_revision: str | None = "c1d4e6f8a203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("scope_type", sa.String(length=64), nullable=False),
        sa.Column("scope_id", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("retrieval_keywords_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("source_refs_json", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["supersedes"], ["memories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "memory_type",
        "scope_type",
        "scope_id",
        "valid_from",
        "valid_until",
        "supersedes",
        "status",
        "pinned",
        "created_at",
        "updated_at",
    ):
        op.create_index(f"ix_memories_{column}", "memories", [column])
    op.create_index(
        "ix_memories_scope_status",
        "memories",
        ["scope_type", "scope_id", "status"],
    )
    op.create_index(
        "ix_memories_type_status",
        "memories",
        ["memory_type", "status"],
    )


def downgrade() -> None:
    op.drop_table("memories")
