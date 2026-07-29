"""Persistable agent conversation messages and checkpoints."""

from pydantic import AwareDatetime, Field

from .base import DomainModel, new_id, utc_now
from .enums import MessageRole


class AgentMessage(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    role: MessageRole
    message_type: str = "message"
    content: str
    sequence: int = Field(ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class AgentCheckpoint(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    sdk_state: dict[str, object] = Field(default_factory=dict)
    status: str = "pending"
    created_at: AwareDatetime = Field(default_factory=utc_now)
    resolved_at: AwareDatetime | None = None
