"""SQLAlchemy records for unified Session Capability selection state."""

from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .orm import Base
from .skill_selection_records import _lower_hex_digest_check
from .types import UTCDateTime


class AgentCapabilityScopeRecord(Base):
    __tablename__ = "agent_capability_scopes"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('tool', 'skill', 'technique')",
            name="ck_agent_capability_scopes_kind",
        ),
        Index("ix_agent_capability_scopes_run_id", "run_id"),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed_capability_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentCapabilitySelectionRecord(Base):
    __tablename__ = "agent_capability_selections"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('tool', 'skill', 'technique')",
            name="ck_agent_capability_selections_kind",
        ),
        CheckConstraint(
            "source IN ('official', 'operator', 'organization', 'engagement')",
            name="ck_agent_capability_selections_source",
        ),
        CheckConstraint(
            _lower_hex_digest_check("capability_digest"),
            name="ck_agent_capability_selections_digest",
        ),
        CheckConstraint(
            "(active = 1 AND unloaded_at IS NULL) OR (active = 0 AND unloaded_at IS NOT NULL)",
            name="ck_agent_capability_selections_active_shape",
        ),
        Index(
            "ix_agent_capability_selections_run_active",
            "run_id",
            "active",
            "session_id",
        ),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(1024), nullable=False)
    capability_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    snapshot_json: Mapped[dict[str, JsonValue]] = mapped_column(JSON, nullable=False)
    state_json: Mapped[dict[str, JsonValue]] = mapped_column(JSON, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    selected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    unloaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
