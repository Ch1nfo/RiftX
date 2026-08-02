"""Agent-visible skill metadata."""

from pydantic import Field

from .base import DomainModel


class Skill(DomainModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tool_ids: list[str] = Field(default_factory=list)
    input_schema: dict[str, object] = Field(default_factory=dict)
    output_schema: dict[str, object] = Field(default_factory=dict)
    enabled: bool = True
