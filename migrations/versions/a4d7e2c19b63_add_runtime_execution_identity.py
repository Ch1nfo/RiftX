"""add runtime execution identity

Revision ID: a4d7e2c19b63
Revises: f2a6c8d91e04
Create Date: 2026-07-30 12:36:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d7e2c19b63"
down_revision: str | None = "f2a6c8d91e04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("executions") as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("tool_call_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("attempt_group", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_executions_session_id_agent_sessions",
            "agent_sessions",
            ["session_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_executions_session_id", ["session_id"])
        batch_op.create_index("ix_executions_tool_call_id", ["tool_call_id"])
        batch_op.create_index("ix_executions_attempt_group", ["attempt_group"])


def downgrade() -> None:
    with op.batch_alter_table("executions") as batch_op:
        batch_op.drop_index("ix_executions_attempt_group")
        batch_op.drop_index("ix_executions_tool_call_id")
        batch_op.drop_index("ix_executions_session_id")
        batch_op.drop_constraint(
            "fk_executions_session_id_agent_sessions", type_="foreignkey"
        )
        batch_op.drop_column("attempt_group")
        batch_op.drop_column("tool_call_id")
        batch_op.drop_column("session_id")
