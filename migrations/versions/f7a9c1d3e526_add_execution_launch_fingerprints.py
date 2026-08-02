"""add execution launch fingerprints

Revision ID: f7a9c1d3e526
Revises: e6f8a0b2c415
Create Date: 2026-08-01 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a9c1d3e526"
down_revision: str | None = "e6f8a0b2c415"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Historical rows cannot reconstruct timeout, shell mode/path, or
    # environment mode. Keep them NULL and validate every persisted logical
    # field on replay; all new admissions persist a complete v1 fingerprint.
    op.add_column(
        "executions",
        sa.Column("launch_fingerprint", sa.String(80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executions", "launch_fingerprint")
