"""add durable Agent Runtime domain tables

Revision ID: e7c3a91f4b20
Revises: d4f26a8b7c10
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7c3a91f4b20"
down_revision: str | Sequence[str] | None = "d4f26a8b7c10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("parent_session_id", sa.String(length=64), nullable=True),
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column("model_profile", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("latest_checkpoint_id", sa.String(length=64), nullable=True),
        sa.Column("provider_state_id", sa.String(length=64), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("model_call_count", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("turn_count >= 0", name="ck_agent_sessions_turn_count"),
        sa.CheckConstraint("model_call_count >= 0", name="ck_agent_sessions_model_call_count"),
        sa.CheckConstraint("tool_call_count >= 0", name="ck_agent_sessions_tool_call_count"),
        sa.ForeignKeyConstraint(["parent_session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_sessions_run_id"), "agent_sessions", ["run_id"])
    op.create_index(
        op.f("ix_agent_sessions_parent_session_id"), "agent_sessions", ["parent_session_id"]
    )
    op.create_index(op.f("ix_agent_sessions_status"), "agent_sessions", ["status"])
    op.create_index(
        op.f("ix_agent_sessions_latest_checkpoint_id"),
        "agent_sessions",
        ["latest_checkpoint_id"],
    )
    op.create_index(
        op.f("ix_agent_sessions_provider_state_id"),
        "agent_sessions",
        ["provider_state_id"],
    )

    op.create_table(
        "agent_cycles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("yield_reason", sa.String(length=64), nullable=True),
        sa.Column("model_call_count", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("sequence >= 1", name="ck_agent_cycles_sequence"),
        sa.CheckConstraint("model_call_count >= 0", name="ck_agent_cycles_model_call_count"),
        sa.CheckConstraint("tool_call_count >= 0", name="ck_agent_cycles_tool_call_count"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "sequence", name="uq_agent_cycles_session_sequence"),
    )
    op.create_index(op.f("ix_agent_cycles_run_id"), "agent_cycles", ["run_id"])
    op.create_index(op.f("ix_agent_cycles_session_id"), "agent_cycles", ["session_id"])
    op.create_index(op.f("ix_agent_cycles_status"), "agent_cycles", ["status"])

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_refs_json", sa.JSON(), nullable=False),
        sa.Column("output_refs_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("sequence >= 1", name="ck_agent_steps_sequence"),
        sa.ForeignKeyConstraint(["cycle_id"], ["agent_cycles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cycle_id", "sequence", name="uq_agent_steps_cycle_sequence"),
    )
    op.create_index(op.f("ix_agent_steps_cycle_id"), "agent_steps", ["cycle_id"])
    op.create_index(op.f("ix_agent_steps_status"), "agent_steps", ["status"])
    op.create_index(op.f("ix_agent_steps_step_type"), "agent_steps", ["step_type"])

    op.create_table(
        "provider_states",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("engine_type", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.String(length=64), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("previous_response_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_provider_states_session_id"), "provider_states", ["session_id"])
    op.create_index(op.f("ix_provider_states_created_at"), "provider_states", ["created_at"])

    op.create_table(
        "tool_call_intents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), nullable=False),
        sa.Column("step_id", sa.String(length=64), nullable=False),
        sa.Column("tool_id", sa.String(length=64), nullable=True),
        sa.Column("skill_id", sa.String(length=64), nullable=True),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("command_preview", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("target_summary", sa.Text(), nullable=True),
        sa.Column("approval_level", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("engine_call_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "tool_id IS NOT NULL OR skill_id IS NOT NULL",
            name="ck_tool_call_intents_tool_or_skill",
        ),
        sa.ForeignKeyConstraint(["cycle_id"], ["agent_cycles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["agent_steps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "run_id",
        "session_id",
        "cycle_id",
        "step_id",
        "tool_id",
        "skill_id",
        "status",
        "engine_call_id",
    ):
        op.create_index(op.f(f"ix_tool_call_intents_{column}"), "tool_call_intents", [column])

    op.create_table(
        "run_leases",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_run_leases_version"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(op.f("ix_run_leases_owner_id"), "run_leases", ["owner_id"])
    op.create_index(op.f("ix_run_leases_expires_at"), "run_leases", ["expires_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_run_leases_expires_at"), table_name="run_leases")
    op.drop_index(op.f("ix_run_leases_owner_id"), table_name="run_leases")
    op.drop_table("run_leases")

    for column in reversed(
        (
            "run_id",
            "session_id",
            "cycle_id",
            "step_id",
            "tool_id",
            "skill_id",
            "status",
            "engine_call_id",
        )
    ):
        op.drop_index(op.f(f"ix_tool_call_intents_{column}"), table_name="tool_call_intents")
    op.drop_table("tool_call_intents")

    op.drop_index(op.f("ix_provider_states_created_at"), table_name="provider_states")
    op.drop_index(op.f("ix_provider_states_session_id"), table_name="provider_states")
    op.drop_table("provider_states")

    op.drop_index(op.f("ix_agent_steps_step_type"), table_name="agent_steps")
    op.drop_index(op.f("ix_agent_steps_status"), table_name="agent_steps")
    op.drop_index(op.f("ix_agent_steps_cycle_id"), table_name="agent_steps")
    op.drop_table("agent_steps")

    op.drop_index(op.f("ix_agent_cycles_status"), table_name="agent_cycles")
    op.drop_index(op.f("ix_agent_cycles_session_id"), table_name="agent_cycles")
    op.drop_index(op.f("ix_agent_cycles_run_id"), table_name="agent_cycles")
    op.drop_table("agent_cycles")

    op.drop_index(op.f("ix_agent_sessions_provider_state_id"), table_name="agent_sessions")
    op.drop_index(op.f("ix_agent_sessions_latest_checkpoint_id"), table_name="agent_sessions")
    op.drop_index(op.f("ix_agent_sessions_status"), table_name="agent_sessions")
    op.drop_index(op.f("ix_agent_sessions_parent_session_id"), table_name="agent_sessions")
    op.drop_index(op.f("ix_agent_sessions_run_id"), table_name="agent_sessions")
    op.drop_table("agent_sessions")
