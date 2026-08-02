"""Add optional per-Run model profile.

Revision ID: d4f26a8b7c10
Revises: c81f0a3d2e19
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f26a8b7c10"
down_revision: str | Sequence[str] | None = "c81f0a3d2e19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("model_profile", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "model_profile")
