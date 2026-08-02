"""persist durable Runtime Cycle outcomes for Temporal Activity retry

Revision ID: b7e1d2c3f4a5
Revises: d9f4a6c2b731
Create Date: 2026-07-30 17:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e1d2c3f4a5"
down_revision: str | None = "d9f4a6c2b731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_cycles",
        sa.Column("waiting_object_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_cycles",
        sa.Column("checkpoint_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_agent_cycles_waiting_object_id",
        "agent_cycles",
        ["waiting_object_id"],
    )
    op.create_index(
        "ix_agent_cycles_checkpoint_id",
        "agent_cycles",
        ["checkpoint_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_cycles_checkpoint_id", table_name="agent_cycles")
    op.drop_index("ix_agent_cycles_waiting_object_id", table_name="agent_cycles")
    op.drop_column("agent_cycles", "checkpoint_id")
    op.drop_column("agent_cycles", "waiting_object_id")
