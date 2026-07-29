"""Tool definitions and availability snapshots."""

from pydantic import AwareDatetime, Field, field_validator

from .base import DomainModel, utc_now
from .enums import ApprovalLevel, ExecutorType, ToolAvailability


class Tool(DomainModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    command: list[str]
    executor: ExecutorType = ExecutorType.PROCESS
    capabilities: list[str] = Field(default_factory=list)
    version_probe: list[str] = Field(default_factory=list)
    approval_level: ApprovalLevel = ApprovalLevel.NEVER
    timeout_seconds: float = Field(default=1800, gt=0)
    environment: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("command")
    @classmethod
    def command_must_not_be_empty(cls, command: list[str]) -> list[str]:
        if not command or any(not part for part in command):
            raise ValueError("tool command must contain non-empty argv elements")
        return command


class ToolState(DomainModel):
    tool_id: str
    node_id: str
    availability: ToolAvailability = ToolAvailability.UNKNOWN
    resolved_command: str | None = None
    version: str | None = None
    reason: str | None = None
    checked_at: AwareDatetime = Field(default_factory=utc_now)
