"""add canonical web document source registry

Revision ID: a7d9e1f3c205
Revises: f3a6b8c1d204
Create Date: 2026-07-30 23:58:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d9e1f3c205"
down_revision: str | None = "f3a6b8c1d204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_documents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("site_name", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("raw_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("normalized_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("text_length", sa.Integer(), nullable=False),
        sa.Column("extraction_status", sa.String(length=64), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("source_class", sa.String(length=64), nullable=False),
        sa.Column("destination_class", sa.String(length=64), nullable=False),
        sa.Column("cache_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["raw_artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["normalized_artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "run_id",
        "published_at",
        "fetched_at",
        "raw_artifact_id",
        "normalized_artifact_id",
        "content_hash",
        "extraction_status",
        "cache_expires_at",
    ):
        op.create_index(f"ix_web_documents_{column}", "web_documents", [column])
    op.create_index(
        "ix_web_documents_run_requested",
        "web_documents",
        ["run_id", "requested_url", "fetched_at"],
    )

    op.create_table(
        "web_document_chunks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("heading_path_json", sa.JSON(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("embedding_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["web_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "sequence", name="uq_web_document_chunk_sequence"),
    )
    op.create_index("ix_web_document_chunks_document_id", "web_document_chunks", ["document_id"])

    op.create_table(
        "source_references",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["web_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id"),
    )
    for column in (
        "document_id",
        "domain",
        "published_at",
        "fetched_at",
        "source_type",
        "content_hash",
    ):
        op.create_index(f"ix_source_references_{column}", "source_references", [column])


def downgrade() -> None:
    op.drop_table("source_references")
    op.drop_table("web_document_chunks")
    op.drop_table("web_documents")
