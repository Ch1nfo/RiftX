"""add durable web search and research records

Revision ID: b8e0f2a4d306
Revises: a7d9e1f3c205
Create Date: 2026-07-30 20:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e0f2a4d306"
down_revision: str | None = "a7d9e1f3c205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_search_queries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("search_type", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("run_id", "session_id", "search_type", "provider", "status"):
        op.create_index(f"ix_web_search_queries_{column}", "web_search_queries", [column])

    op.create_table(
        "web_search_results",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("query_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("provider_rank", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["query_id"], ["web_search_queries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("query_id", "normalized_url", name="uq_web_search_result_url"),
    )
    for column in ("query_id", "domain", "published_at"):
        op.create_index(f"ix_web_search_results_{column}", "web_search_results", [column])

    op.create_table(
        "web_research_notes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("key_points_json", sa.JSON(), nullable=False),
        sa.Column("evidence_spans_json", sa.JSON(), nullable=False),
        sa.Column("missing_information_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("model_profile", sa.String(length=255), nullable=True),
        sa.Column("content_trust", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["web_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["source_references.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("document_id", "source_id", "created_at"):
        op.create_index(f"ix_web_research_notes_{column}", "web_research_notes", [column])

    op.create_table(
        "web_research_packets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("claims_json", sa.JSON(), nullable=False),
        sa.Column("source_ids_json", sa.JSON(), nullable=False),
        sa.Column("disagreements_json", sa.JSON(), nullable=False),
        sa.Column("unresolved_questions_json", sa.JSON(), nullable=False),
        sa.Column("search_query_ids_json", sa.JSON(), nullable=False),
        sa.Column("document_ids_json", sa.JSON(), nullable=False),
        sa.Column("artifact_ids_json", sa.JSON(), nullable=False),
        sa.Column("content_trust", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("run_id", "session_id", "created_at"):
        op.create_index(f"ix_web_research_packets_{column}", "web_research_packets", [column])


def downgrade() -> None:
    op.drop_table("web_research_packets")
    op.drop_table("web_research_notes")
    op.drop_table("web_search_results")
    op.drop_table("web_search_queries")
