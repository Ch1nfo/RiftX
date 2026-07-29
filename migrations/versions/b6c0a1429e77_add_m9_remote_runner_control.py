"""add M9 authenticated remote Runner control state

Revision ID: b6c0a1429e77
Revises: 84de2f2cc1a0
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6c0a1429e77"
down_revision: str | Sequence[str] | None = "84de2f2cc1a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runner_credentials",
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_table(
        "runner_commands",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "node_id",
            "idempotency_key",
            name="uq_runner_commands_node_idempotency",
        ),
    )
    op.create_index(op.f("ix_runner_commands_kind"), "runner_commands", ["kind"])
    op.create_index(
        op.f("ix_runner_commands_lease_expires_at"),
        "runner_commands",
        ["lease_expires_at"],
    )
    op.create_index(op.f("ix_runner_commands_node_id"), "runner_commands", ["node_id"])
    op.create_index(op.f("ix_runner_commands_status"), "runner_commands", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_runner_commands_status"), table_name="runner_commands")
    op.drop_index(op.f("ix_runner_commands_node_id"), table_name="runner_commands")
    op.drop_index(
        op.f("ix_runner_commands_lease_expires_at"),
        table_name="runner_commands",
    )
    op.drop_index(op.f("ix_runner_commands_kind"), table_name="runner_commands")
    op.drop_table("runner_commands")
    op.drop_table("runner_credentials")
