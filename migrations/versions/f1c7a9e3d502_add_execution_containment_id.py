"""add execution containment identity

Revision ID: f1c7a9e3d502
Revises: e4b7c1d9a305
Create Date: 2026-08-01 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1c7a9e3d502"
down_revision: str | None = "e4b7c1d9a305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("containment_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executions", "containment_id")
