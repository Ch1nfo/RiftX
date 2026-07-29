"""add M7 approval request snapshots and grants

Revision ID: 6b5e4f7a8c91
Revises: 2f14cbcea74b
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6b5e4f7a8c91"
down_revision: str | Sequence[str] | None = "2f14cbcea74b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("sdk_call_id", sa.String(length=255), nullable=True))
    op.execute(sa.text("UPDATE tool_calls SET sdk_call_id = id WHERE sdk_call_id IS NULL"))
    with op.batch_alter_table("tool_calls") as batch_op:
        batch_op.alter_column(
            "sdk_call_id",
            existing_type=sa.String(length=255),
            nullable=False,
        )
    op.create_index(
        op.f("ix_tool_calls_sdk_call_id"),
        "tool_calls",
        ["sdk_call_id"],
        unique=False,
    )
    with op.batch_alter_table("tool_calls") as batch_op:
        batch_op.create_unique_constraint(
            "uq_tool_calls_run_sdk_call",
            ["run_id", "sdk_call_id"],
        )

    op.add_column(
        "approvals",
        sa.Column("tool_name", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "approvals",
        sa.Column("command_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "approvals",
        sa.Column("cwd", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "approvals",
        sa.Column("target_summary", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "approvals",
        sa.Column("env_diff_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.alter_column(
            "command_json",
            existing_type=sa.JSON(),
            nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "env_diff_json",
            existing_type=sa.JSON(),
            nullable=False,
            server_default=None,
        )

    op.create_table(
        "approval_grants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tool_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "tool_id", name="uq_approval_grants_run_tool"),
    )
    op.create_index(
        op.f("ix_approval_grants_run_id"),
        "approval_grants",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_approval_grants_tool_id"),
        "approval_grants",
        ["tool_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_approval_grants_tool_id"), table_name="approval_grants")
    op.drop_index(op.f("ix_approval_grants_run_id"), table_name="approval_grants")
    op.drop_table("approval_grants")

    op.drop_column("approvals", "env_diff_json")
    op.drop_column("approvals", "target_summary")
    op.drop_column("approvals", "cwd")
    op.drop_column("approvals", "command_json")
    op.drop_column("approvals", "tool_name")

    with op.batch_alter_table("tool_calls") as batch_op:
        batch_op.drop_constraint("uq_tool_calls_run_sdk_call", type_="unique")
    op.drop_index(op.f("ix_tool_calls_sdk_call_id"), table_name="tool_calls")
    op.drop_column("tool_calls", "sdk_call_id")
