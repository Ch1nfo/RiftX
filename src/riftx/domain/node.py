"""Execution node domain model."""

from pydantic import AwareDatetime, Field

from .base import DomainModel, new_id, utc_now
from .enums import NodeStatus


class Node(DomainModel):
    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    architecture: str = Field(min_length=1)
    status: NodeStatus = NodeStatus.UNKNOWN
    labels: dict[str, str] = Field(default_factory=dict)
    last_seen_at: AwareDatetime | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
