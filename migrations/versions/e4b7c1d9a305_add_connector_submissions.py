"""add unified connector submissions

Revision ID: e4b7c1d9a305
Revises: d1a4c7e9b205
Create Date: 2026-07-31 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4b7c1d9a305"
down_revision: str | None = "d1a4c7e9b205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connector_submissions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("capture_id", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_artifact_id", sa.String(length=64), nullable=False),
        sa.Column("response_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("manifest_artifact_id", sa.String(length=64), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["request_artifact_id"], ["artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["response_artifact_id"], ["artifacts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["manifest_artifact_id"], ["artifacts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source", "capture_id", name="uq_connector_submissions_source_capture"
        ),
    )
    for column in (
        "run_id",
        "source",
        "capture_id",
        "fingerprint",
        "request_artifact_id",
        "response_artifact_id",
        "manifest_artifact_id",
        "created_at",
    ):
        op.create_index(
            f"ix_connector_submissions_{column}", "connector_submissions", [column]
        )


def downgrade() -> None:
    op.drop_table("connector_submissions")
