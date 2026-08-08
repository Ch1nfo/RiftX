"""SQLAlchemy records for Session-scoped Progressive Skill state."""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .orm import Base
from .types import UTCDateTime


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


class AgentSkillScopeRecord(Base):
    __tablename__ = "agent_skill_scopes"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed_skill_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)


class AgentSkillSelectionRecord(Base):
    __tablename__ = "agent_skill_selections"
    __table_args__ = (
        CheckConstraint(
            "source IN ('official', 'operator', 'organization', 'engagement')",
            name="ck_agent_skill_selections_source",
        ),
        CheckConstraint(
            _lower_hex_digest_check("skill_digest"),
            name="ck_agent_skill_selections_digest",
        ),
        CheckConstraint(
            "(active = 1 AND unloaded_at IS NULL) OR "
            "(active = 0 AND unloaded_at IS NOT NULL)",
            name="ck_agent_skill_selections_active_shape",
        ),
        CheckConstraint(
            "references_loaded = 0 OR reference_json IS NOT NULL",
            name="ck_agent_skill_selections_reference_shape",
        ),
        Index(
            "ix_agent_skill_selections_run_active",
            "run_id",
            "active",
            "session_id",
        ),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    skill_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    document_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    reference_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    references_loaded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    selected_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    unloaded_at: Mapped[object | None] = mapped_column(UTCDateTime(), nullable=True)
