"""add durable target HTTP request records

Revision ID: c9f1a3b5e407
Revises: b8e0f2a4d306
Create Date: 2026-07-30 21:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9f1a3b5e407"
down_revision: str | None = "b8e0f2a4d306"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "target_http_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("execution_key", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("tool_call_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("request_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("response_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_key"),
    )
    for column in (
        "execution_key",
        "run_id",
        "session_id",
        "tool_call_id",
        "node_id",
        "method",
        "request_artifact_id",
        "response_artifact_id",
        "created_at",
    ):
        op.create_index(f"ix_target_http_requests_{column}", "target_http_requests", [column])


def downgrade() -> None:
    op.drop_table("target_http_requests")
