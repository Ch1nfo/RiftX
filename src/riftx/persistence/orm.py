"""SQLAlchemy mappings for durable RiftX business state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
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
    model_profile: Mapped[str | None] = mapped_column(String(255))
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AgentMessageRecord(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_agent_messages_session_sequence"),
        Index("ix_agent_messages_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    parent_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_messages.id", ondelete="SET NULL"), index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content: Mapped[str | None] = mapped_column(Text)
    structured_content_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), index=True)
    execution_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    compacted_by_checkpoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    token_count: Mapped[int | None] = mapped_column(Integer)
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
    __table_args__ = (UniqueConstraint("run_id", "sdk_call_id", name="uq_tool_calls_run_sdk_call"),)

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    sdk_call_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
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
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="SET NULL"), index=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    attempt_group: Mapped[str | None] = mapped_column(String(64), index=True)
    node_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    executor_type: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    argv_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    command_text: Mapped[str | None] = mapped_column(Text)
    tool_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    tool_version: Mapped[str | None] = mapped_column(Text)
    executable_path: Mapped[str | None] = mapped_column(Text)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    env_diff_json: Mapped[dict[str, str | None]] = mapped_column(JSON, nullable=False, default=dict)
    platform_system: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    platform_release: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    platform_architecture: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    pid: Mapped[int | None] = mapped_column(Integer)
    process_group_id: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout_path: Mapped[str] = mapped_column(Text, nullable=False)
    stderr_path: Mapped[str] = mapped_column(Text, nullable=False)
    process_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
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
    runner_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, default="")
    shell: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cwd: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    owner: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    cols: Mapped[int] = mapped_column(Integer, nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False)
    output_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    takeover_cursor: Mapped[int | None] = mapped_column(Integer)
    takeover_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    transcript_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
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
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    command_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    cwd: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    env_diff_json: Mapped[dict[str, str | None]] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ApprovalGrantRecord(Base):
    __tablename__ = "approval_grants"
    __table_args__ = (UniqueConstraint("run_id", "tool_id", name="uq_approval_grants_run_tool"),)

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


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
    runner_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    labels_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunnerCredentialRecord(Base):
    __tablename__ = "runner_credentials"

    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    rotated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RunnerCommandRecord(Base):
    __tablename__ = "runner_commands"
    __table_args__ = (
        UniqueConstraint(
            "node_id",
            "idempotency_key",
            name="uq_runner_commands_node_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


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


class AgentSessionRecord(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    model_profile: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    latest_checkpoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    provider_state_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AgentCycleRecord(Base):
    __tablename__ = "agent_cycles"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_agent_cycles_session_sequence"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    yield_reason: Mapped[str | None] = mapped_column(String(64))
    waiting_object_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    model_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AgentRuntimeStepRecord(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("cycle_id", "sequence", name="uq_agent_steps_cycle_sequence"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("agent_cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    input_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    output_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ProviderStateRecord(Base):
    __tablename__ = "provider_states"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    engine_type: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    previous_response_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class WorkingMemoryRecord(Base):
    __tablename__ = "working_memories"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ContextCompilationRecord(Base):
    __tablename__ = "context_compilations"
    __table_args__ = (
        Index("ix_context_compilations_session_created", "session_id", "created_at"),
        Index("ix_context_compilations_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    model_profile: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_input_tokens: Mapped[int | None] = mapped_column(Integer)
    actual_output_tokens: Mapped[int | None] = mapped_column(Integer)
    loaded_memory_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    checkpoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ContextCheckpointRecord(Base):
    __tablename__ = "context_checkpoints"
    __table_args__ = (
        Index("ix_context_checkpoints_session_created", "session_id", "created_at"),
        Index("ix_context_checkpoints_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkpoint_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    compaction_stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_profile: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    working_memory_version: Mapped[int | None] = mapped_column(Integer)
    # A checkpoint must remain readable even if a provider-native state expires
    # or is deleted. The canonical snapshot therefore keeps this as a soft ID.
    provider_state_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    context_compilation_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_compilations.id", ondelete="SET NULL"), index=True
    )
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class MemoryRecordRow(Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_scope_status", "scope_type", "scope_id", "status"),
        Index("ix_memories_type_status", "memory_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_keywords_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    source_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    supersedes: Mapped[str | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class EngagementFactRecord(Base):
    __tablename__ = "engagement_facts"
    __table_args__ = (
        Index("ix_engagement_facts_identity", "engagement_id", "subject", "predicate"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    natural_language: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_run_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_session_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_execution_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    supersedes_fact_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_facts.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class FactRelationRecord(Base):
    __tablename__ = "fact_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_fact_id",
            "target_fact_id",
            "relation_type",
            name="uq_fact_relation_edge",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_fact_id: Mapped[str] = mapped_column(
        ForeignKey("engagement_facts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_fact_id: Mapped[str] = mapped_column(
        ForeignKey("engagement_facts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    source_session_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    source_execution_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class WebDocumentRecord(Base):
    __tablename__ = "web_documents"
    __table_args__ = (
        Index("ix_web_documents_run_requested", "run_id", "requested_url", "fetched_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    site_name: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str | None] = mapped_column(String(64))
    raw_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    normalized_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    text_length: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_class: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_class: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class WebDocumentChunkRecord(Base):
    __tablename__ = "web_document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "sequence", name="uq_web_document_chunk_sequence"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("web_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_json: Mapped[list[float] | None] = mapped_column(JSON)


class SourceReferenceRecord(Base):
    __tablename__ = "source_references"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("web_documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class WebSearchQueryRecord(Base):
    __tablename__ = "web_search_queries"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    search_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class WebSearchResultRecord(Base):
    __tablename__ = "web_search_results"
    __table_args__ = (
        UniqueConstraint("query_id", "normalized_url", name="uq_web_search_result_url"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    query_id: Mapped[str] = mapped_column(
        ForeignKey("web_search_queries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class WebResearchNoteRecord(Base):
    __tablename__ = "web_research_notes"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("web_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("source_references.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    key_points_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_spans_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    missing_information_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    model_profile: Mapped[str | None] = mapped_column(String(255))
    content_trust: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class WebResearchPacketRecord(Base):
    __tablename__ = "web_research_packets"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    claims_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    source_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    disagreements_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    unresolved_questions_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    search_query_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    document_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    content_trust: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ToolCallIntentRecord(Base):
    __tablename__ = "tool_call_intents"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("agent_cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[str] = mapped_column(
        ForeignKey("agent_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    skill_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), index=True)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    command_preview: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_summary: Mapped[str | None] = mapped_column(Text)
    approval_level: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    engine_call_id: Mapped[str | None] = mapped_column(String(255), index=True)
    execution_spec_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RuntimeApprovalRequestRecord(Base):
    __tablename__ = "runtime_approval_requests"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("agent_cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_call_intent_id: Mapped[str] = mapped_column(
        ForeignKey("tool_call_intents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    context_compilation_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_compilations.id", ondelete="SET NULL"), index=True
    )
    working_memory_version: Mapped[int | None] = mapped_column(Integer)
    provider_state_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_states.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    decision: Mapped[str | None] = mapped_column(String(STATUS_LENGTH))
    feedback: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class UserInputRequestRecord(Base):
    __tablename__ = "user_input_requests"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("agent_cycles.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    context_compilation_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_compilations.id", ondelete="SET NULL"), index=True
    )
    working_memory_version: Mapped[int | None] = mapped_column(Integer)
    provider_state_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_states.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False, index=True)
    response_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_messages.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RunLeaseRecord(Base):
    __tablename__ = "run_leases"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
