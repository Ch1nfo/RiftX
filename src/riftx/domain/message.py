"""Persistable agent conversation messages and checkpoints."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, model_validator

from .base import DomainModel, new_id, utc_now
from .enums import MessageRole, MessageType, MessageVisibility


class AgentMessage(DomainModel):
    """A provider-neutral, permanently retained transcript item."""

    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    parent_message_id: str | None = None
    role: MessageRole
    message_type: MessageType
    content: str | None = None
    structured_content: dict[str, object] | None = None
    tool_call_id: str | None = None
    execution_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    visibility: MessageVisibility = MessageVisibility.AGENT_ONLY
    compacted_by_checkpoint_id: str | None = None
    token_count: int | None = Field(default=None, ge=0)
    sequence: int = Field(ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_content_or_structure(self) -> AgentMessage:
        if self.content is None and self.structured_content is None:
            raise ValueError("agent message requires content or structured_content")
        if self.parent_message_id == self.id:
            raise ValueError("agent message cannot be its own parent")
        return self


class TranscriptMessageDraft(DomainModel):
    """Message fields supplied before the repository assigns identity and order."""

    agent_id: str = Field(min_length=1)
    parent_message_id: str | None = None
    role: MessageRole
    message_type: MessageType
    content: str | None = None
    structured_content: dict[str, object] | None = None
    tool_call_id: str | None = None
    execution_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    visibility: MessageVisibility = MessageVisibility.AGENT_ONLY
    compacted_by_checkpoint_id: str | None = None
    token_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_content_or_structure(self) -> TranscriptMessageDraft:
        if self.content is None and self.structured_content is None:
            raise ValueError("transcript message requires content or structured_content")
        return self


class AgentCheckpoint(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    sdk_state: dict[str, object] = Field(default_factory=dict)
    status: str = "pending"
    created_at: AwareDatetime = Field(default_factory=utc_now)
    resolved_at: AwareDatetime | None = None
