"""SQLAlchemy mappings for durable RiftX business state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import UTCDateTime

ID_LENGTH = 64
STATUS_LENGTH = 32


class Base(DeclarativeBase):
    """Declarative metadata root."""


class EngagementRecord(Base):
    __tablename__ = "engagements"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    authorization_reference: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunRecord(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        ForeignKey("engagements.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    success_criteria_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    entry_points_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    approval_mode: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AgentMessageRecord(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_messages_run_sequence"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentCheckpointRecord(Base):
    __tablename__ = "agent_checkpoints"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sdk_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ToolCallRecord(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_step_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    tool_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    skill_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    approval_status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ExecutionRecord(Base):
    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    execution_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    executor_type: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    argv_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    command_text: Mapped[str | None] = mapped_column(Text)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    env_diff_json: Mapped[dict[str, str | None]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    pid: Mapped[int | None] = mapped_column(Integer)
    process_group_id: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout_path: Mapped[str] = mapped_column(Text, nullable=False)
    stderr_path: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TerminalSessionRecord(Base):
    __tablename__ = "terminal_sessions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    owner: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    cols: Mapped[int] = mapped_column(Integer, nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_call_id: Mapped[str] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RunEventRecord(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        Index("ix_run_events_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("executions.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class FindingRecord(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    severity: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    affected_assets_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    reproduction_steps_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    impact: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ReportRecord(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    format: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    finding_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class NodeRecord(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    architecture: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    labels_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ToolStateRecord(Base):
    __tablename__ = "tool_states"
    __table_args__ = (UniqueConstraint("node_id", "tool_id", name="uq_tool_states_node_tool"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    availability: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    resolved_command: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
