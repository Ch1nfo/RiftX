"""add execution provenance metadata

Revision ID: c81f0a3d2e19
Revises: b6c0a1429e77
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c81f0a3d2e19"
down_revision: str | Sequence[str] | None = "b6c0a1429e77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("tool_id", sa.String(length=64), nullable=True))
    op.add_column("executions", sa.Column("tool_version", sa.Text(), nullable=True))
    op.add_column("executions", sa.Column("executable_path", sa.Text(), nullable=True))
    op.add_column(
        "executions",
        sa.Column("platform_system", sa.String(length=64), server_default="", nullable=False),
    )
    op.add_column(
        "executions",
        sa.Column("platform_release", sa.String(length=255), server_default="", nullable=False),
    )
    op.add_column(
        "executions",
        sa.Column("platform_architecture", sa.String(length=64), server_default="", nullable=False),
    )
    op.add_column(
        "executions",
        sa.Column("process_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_executions_tool_id"), "executions", ["tool_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_executions_tool_id"), table_name="executions")
    op.drop_column("executions", "process_created_at")
    op.drop_column("executions", "platform_architecture")
    op.drop_column("executions", "platform_release")
    op.drop_column("executions", "platform_system")
    op.drop_column("executions", "executable_path")
    op.drop_column("executions", "tool_version")
    op.drop_column("executions", "tool_id")
