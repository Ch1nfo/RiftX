"""add M9 node lifecycle metadata

Revision ID: 84de2f2cc1a0
Revises: 6b5e4f7a8c91
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "84de2f2cc1a0"
down_revision: str | Sequence[str] | None = "6b5e4f7a8c91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column(
            "runner_version",
            sa.String(length=64),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "nodes",
        sa.Column(
            "capabilities_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "nodes",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(sa.text("UPDATE nodes SET updated_at = created_at WHERE updated_at IS NULL"))
    with op.batch_alter_table("nodes") as batch_op:
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.alter_column(
            "runner_version",
            existing_type=sa.String(length=64),
            nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "capabilities_json",
            existing_type=sa.JSON(),
            nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    op.drop_column("nodes", "updated_at")
    op.drop_column("nodes", "capabilities_json")
    op.drop_column("nodes", "runner_version")
