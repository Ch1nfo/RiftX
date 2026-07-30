"""add durable terminal Runtime state

Revision ID: a9c3e5f7b102
Revises: f8a2c4d6e910
Create Date: 2026-07-30 20:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c3e5f7b102"
down_revision: str | None = "f8a2c4d6e910"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("terminal_sessions") as batch:
        batch.add_column(
            sa.Column("runner_id", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("shell", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("cwd", sa.Text(), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("output_cursor", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("takeover_cursor", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("takeover_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("transcript_artifact_id", sa.String(length=64), nullable=True)
        )
        batch.create_foreign_key(
            "fk_terminal_sessions_transcript_artifact_id_artifacts",
            "artifacts",
            ["transcript_artifact_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_terminal_sessions_transcript_artifact_id",
            ["transcript_artifact_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("terminal_sessions") as batch:
        batch.drop_index("ix_terminal_sessions_transcript_artifact_id")
        batch.drop_constraint(
            "fk_terminal_sessions_transcript_artifact_id_artifacts",
            type_="foreignkey",
        )
        batch.drop_column("transcript_artifact_id")
        batch.drop_column("takeover_started_at")
        batch.drop_column("takeover_cursor")
        batch.drop_column("output_cursor")
        batch.drop_column("cwd")
        batch.drop_column("shell")
        batch.drop_column("runner_id")
