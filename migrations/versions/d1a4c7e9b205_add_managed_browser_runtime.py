"""add durable managed browser runtime

Revision ID: d1a4c7e9b205
Revises: c9f1a3b5e407
Create Date: 2026-07-30 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1a4c7e9b205"
down_revision: str | None = "c9f1a3b5e407"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("agent_session_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("owner", sa.String(length=64), nullable=False),
        sa.Column("browser_type", sa.String(length=64), nullable=False),
        sa.Column("profile_id", sa.String(length=128), nullable=True),
        sa.Column("profile_path", sa.Text(), nullable=True),
        sa.Column("cdp_endpoint", sa.Text(), nullable=True),
        sa.Column("current_page_id", sa.String(length=64), nullable=True),
        sa.Column("page_ids_json", sa.JSON(), nullable=False),
        sa.Column("takeover_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("takeover_observation_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["agent_session_id"], ["agent_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "run_id",
        "agent_session_id",
        "node_id",
        "mode",
        "status",
        "owner",
        "profile_id",
        "current_page_id",
        "created_at",
    ):
        op.create_index(f"ix_browser_sessions_{column}", "browser_sessions", [column])

    op.create_table(
        "browser_pages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("browser_session_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("last_observation_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["browser_session_id"], ["browser_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "browser_session_id",
        "status",
        "created_at",
    ):
        op.create_index(f"ix_browser_pages_{column}", "browser_pages", [column])

    op.create_table(
        "browser_observations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("browser_session_id", sa.String(length=64), nullable=False),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("visible_text_excerpt", sa.Text(), nullable=False),
        sa.Column("headings_json", sa.JSON(), nullable=False),
        sa.Column("interactive_elements_json", sa.JSON(), nullable=False),
        sa.Column("forms_json", sa.JSON(), nullable=False),
        sa.Column("alerts_json", sa.JSON(), nullable=False),
        sa.Column("console_errors_json", sa.JSON(), nullable=False),
        sa.Column("network_summary_json", sa.JSON(), nullable=False),
        sa.Column("screenshot_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("network_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("dom_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("observation_version", sa.Integer(), nullable=False),
        sa.Column("content_trust", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["browser_session_id"], ["browser_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["browser_pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["screenshot_artifact_id"], ["artifacts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["network_artifact_id"], ["artifacts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["dom_artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "browser_session_id",
            "observation_version",
            name="uq_browser_observations_session_version",
        ),
    )
    for column in (
        "browser_session_id",
        "page_id",
        "screenshot_artifact_id",
        "network_artifact_id",
        "dom_artifact_id",
        "observation_version",
        "created_at",
    ):
        op.create_index(
            f"ix_browser_observations_{column}", "browser_observations", [column]
        )

    op.create_table(
        "browser_actions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("action_key", sa.String(length=255), nullable=False),
        sa.Column("browser_session_id", sa.String(length=64), nullable=False),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("observation_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("element_ref", sa.String(length=64), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("result_observation_id", sa.String(length=64), nullable=True),
        sa.Column("download_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["browser_session_id"], ["browser_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["browser_pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["result_observation_id"], ["browser_observations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["download_artifact_id"], ["artifacts.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "browser_session_id", "action_key", name="uq_browser_actions_session_key"
        ),
    )
    for column in (
        "action_key",
        "browser_session_id",
        "page_id",
        "action",
        "status",
        "result_observation_id",
        "download_artifact_id",
        "created_at",
    ):
        op.create_index(f"ix_browser_actions_{column}", "browser_actions", [column])

    op.create_table(
        "browser_takeover_summaries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("browser_session_id", sa.String(length=64), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["browser_session_id"], ["browser_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("run_id", "browser_session_id", "released_at"):
        op.create_index(
            f"ix_browser_takeover_summaries_{column}",
            "browser_takeover_summaries",
            [column],
        )


def downgrade() -> None:
    op.drop_table("browser_takeover_summaries")
    op.drop_table("browser_actions")
    op.drop_table("browser_observations")
    op.drop_table("browser_pages")
    op.drop_table("browser_sessions")
