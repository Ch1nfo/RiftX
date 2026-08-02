"""add complete agent transcript

Revision ID: f2a6c8d91e04
Revises: e7c3a91f4b20
Create Date: 2026-07-30 15:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a6c8d91e04"
down_revision: str | None = "e7c3a91f4b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_messages") as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "agent_id",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'primary'"),
            )
        )
        batch_op.add_column(sa.Column("parent_message_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("structured_content_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("tool_call_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("execution_id", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "artifact_ids_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "visibility",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'agent_only'"),
            )
        )
        batch_op.add_column(
            sa.Column("compacted_by_checkpoint_id", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column("token_count", sa.Integer(), nullable=True))
        batch_op.alter_column("content", existing_type=sa.Text(), nullable=True)
        batch_op.drop_constraint("uq_agent_messages_run_sequence", type_="unique")

    # The legacy SDK adapter used run_id as its session key. Preserve that identity and
    # create an active primary Agent Session for every existing Run before backfilling.
    op.execute(
        sa.text(
            """
            INSERT INTO agent_sessions (
                id, run_id, parent_session_id, agent_type, model_profile, status,
                latest_checkpoint_id, provider_state_id, turn_count, model_call_count,
                tool_call_count, created_at, closed_at
            )
            SELECT
                r.id, r.id, NULL, 'primary', COALESCE(r.model_profile, 'default'), 'active',
                NULL, NULL, 0, 0, 0, r.created_at, NULL
            FROM runs AS r
            WHERE NOT EXISTS (
                SELECT 1 FROM agent_sessions AS s WHERE s.id = r.id
            )
            """
        )
    )
    op.execute(sa.text("UPDATE agent_messages SET session_id = run_id WHERE session_id IS NULL"))
    op.execute(
        sa.text(
            """
            UPDATE agent_messages
            SET message_type = CASE
                WHEN message_type IN ('function_call', 'computer_call', 'tool_call')
                    THEN 'tool_call'
                WHEN message_type LIKE '%_output'
                    OR message_type IN ('tool_result', 'computer_call_output')
                    THEN 'tool_result_reference'
                WHEN message_type IN ('handoff_request', 'subagent_delegation')
                    THEN 'subagent_delegation'
                WHEN message_type IN ('handoff_output', 'subagent_result')
                    THEN 'subagent_result'
                WHEN role = 'user' THEN 'user_message'
                ELSE 'assistant_message'
            END,
            visibility = CASE
                WHEN role IN ('user', 'assistant') THEN 'user_visible'
                ELSE 'agent_only'
            END
            """
        )
    )

    with op.batch_alter_table("agent_messages") as batch_op:
        batch_op.alter_column(
            "session_id", existing_type=sa.String(length=64), nullable=False
        )
        batch_op.alter_column(
            "agent_id", existing_type=sa.String(length=64), server_default=None
        )
        batch_op.alter_column("artifact_ids_json", existing_type=sa.JSON(), server_default=None)
        batch_op.alter_column(
            "visibility", existing_type=sa.String(length=32), server_default=None
        )
        batch_op.create_foreign_key(
            "fk_agent_messages_session_id_agent_sessions",
            "agent_sessions",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_agent_messages_parent_message_id_agent_messages",
            "agent_messages",
            ["parent_message_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_agent_messages_session_sequence", ["session_id", "sequence"]
        )
        batch_op.create_index("ix_agent_messages_session_id", ["session_id"])
        batch_op.create_index("ix_agent_messages_agent_id", ["agent_id"])
        batch_op.create_index("ix_agent_messages_parent_message_id", ["parent_message_id"])
        batch_op.create_index("ix_agent_messages_message_type", ["message_type"])
        batch_op.create_index("ix_agent_messages_tool_call_id", ["tool_call_id"])
        batch_op.create_index("ix_agent_messages_execution_id", ["execution_id"])
        batch_op.create_index("ix_agent_messages_visibility", ["visibility"])
        batch_op.create_index(
            "ix_agent_messages_compacted_by_checkpoint_id", ["compacted_by_checkpoint_id"]
        )
        batch_op.create_index(
            "ix_agent_messages_session_created", ["session_id", "created_at"]
        )


def downgrade() -> None:
    op.execute(sa.text("UPDATE agent_messages SET content = '' WHERE content IS NULL"))
    # Per-session sequences can overlap inside one Run. Rebuild the legacy run-wide
    # sequence before restoring its unique constraint.
    op.execute(
        sa.text(
            """
            UPDATE agent_messages AS current
            SET sequence = (
                SELECT COUNT(*)
                FROM agent_messages AS preceding
                WHERE preceding.run_id = current.run_id
                  AND (
                    preceding.created_at < current.created_at
                    OR (
                        preceding.created_at = current.created_at
                        AND preceding.id <= current.id
                    )
                  )
            )
            """
        )
    )
    with op.batch_alter_table("agent_messages") as batch_op:
        batch_op.drop_index("ix_agent_messages_session_created")
        batch_op.drop_index("ix_agent_messages_compacted_by_checkpoint_id")
        batch_op.drop_index("ix_agent_messages_visibility")
        batch_op.drop_index("ix_agent_messages_execution_id")
        batch_op.drop_index("ix_agent_messages_tool_call_id")
        batch_op.drop_index("ix_agent_messages_message_type")
        batch_op.drop_index("ix_agent_messages_parent_message_id")
        batch_op.drop_index("ix_agent_messages_agent_id")
        batch_op.drop_index("ix_agent_messages_session_id")
        batch_op.drop_constraint("uq_agent_messages_session_sequence", type_="unique")
        batch_op.drop_constraint(
            "fk_agent_messages_parent_message_id_agent_messages", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_agent_messages_session_id_agent_sessions", type_="foreignkey"
        )
        batch_op.create_unique_constraint(
            "uq_agent_messages_run_sequence", ["run_id", "sequence"]
        )
        batch_op.alter_column("content", existing_type=sa.Text(), nullable=False)
        batch_op.drop_column("token_count")
        batch_op.drop_column("compacted_by_checkpoint_id")
        batch_op.drop_column("visibility")
        batch_op.drop_column("artifact_ids_json")
        batch_op.drop_column("execution_id")
        batch_op.drop_column("tool_call_id")
        batch_op.drop_column("structured_content_json")
        batch_op.drop_column("parent_message_id")
        batch_op.drop_column("agent_id")
        batch_op.drop_column("session_id")
