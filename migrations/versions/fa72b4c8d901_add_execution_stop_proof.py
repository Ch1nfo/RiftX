"""add durable physical stop proof to executions

Revision ID: fa72b4c8d901
Revises: f6a1d9c3e805
Create Date: 2026-08-01 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa72b4c8d901"
down_revision: str | None = "f6a1d9c3e805"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("physical_stop_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executions", "physical_stop_confirmed_at")
