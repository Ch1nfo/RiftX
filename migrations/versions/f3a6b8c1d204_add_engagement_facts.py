"""add Engagement facts and attack graph relations

Revision ID: f3a6b8c1d204
Revises: e2f5a7c9d104
Create Date: 2026-07-30 23:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a6b8c1d204"
down_revision: str | None = "e2f5a7c9d104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "engagement_facts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("engagement_id", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("predicate", sa.String(length=255), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("natural_language", sa.Text(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("source_run_ids_json", sa.JSON(), nullable=False),
        sa.Column("source_session_ids_json", sa.JSON(), nullable=False),
        sa.Column("source_execution_ids_json", sa.JSON(), nullable=False),
        sa.Column("artifact_ids_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_fact_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["supersedes_fact_id"], ["engagement_facts.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "engagement_id",
        "subject",
        "predicate",
        "valid_from",
        "valid_until",
        "supersedes_fact_id",
        "status",
        "updated_at",
    ):
        op.create_index(f"ix_engagement_facts_{column}", "engagement_facts", [column])
    op.create_index(
        "ix_engagement_facts_identity",
        "engagement_facts",
        ["engagement_id", "subject", "predicate"],
    )

    op.create_table(
        "fact_relations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("engagement_id", sa.String(length=64), nullable=False),
        sa.Column("source_fact_id", sa.String(length=64), nullable=False),
        sa.Column("target_fact_id", sa.String(length=64), nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("source_run_id", sa.String(length=64), nullable=False),
        sa.Column("source_session_id", sa.String(length=64), nullable=True),
        sa.Column("source_execution_ids_json", sa.JSON(), nullable=False),
        sa.Column("artifact_ids_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_fact_id"], ["engagement_facts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_fact_id"], ["engagement_facts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_fact_id",
            "target_fact_id",
            "relation_type",
            name="uq_fact_relation_edge",
        ),
    )
    for column in (
        "engagement_id",
        "source_fact_id",
        "target_fact_id",
        "relation_type",
        "source_run_id",
        "source_session_id",
        "valid_until",
    ):
        op.create_index(f"ix_fact_relations_{column}", "fact_relations", [column])


def downgrade() -> None:
    op.drop_table("fact_relations")
    op.drop_table("engagement_facts")
