"""Execution node domain model."""

from pydantic import AwareDatetime, Field, field_validator

from .base import DomainModel, new_id, utc_now
from .enums import NodeStatus


class Node(DomainModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=64)
    architecture: str = Field(min_length=1, max_length=64)
    runner_version: str = Field(default="unknown", min_length=1, max_length=64)
    status: NodeStatus = NodeStatus.UNKNOWN
    capabilities: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    last_seen_at: AwareDatetime | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()})
