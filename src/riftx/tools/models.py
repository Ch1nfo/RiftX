"""Configuration and snapshot models for the Tool Registry."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from riftx.domain import ApprovalLevel, ExecutorType, ToolState


class ExecutionPolicy(StrEnum):
    OPEN = "open"
    REGISTERED_ONLY = "registered_only"


class PlatformShells(BaseModel):
    model_config = ConfigDict(extra="forbid")

    linux: str = "/bin/bash"
    macos: str = "/bin/zsh"
    windows: str = "pwsh.exe"


class ShellConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: PlatformShells = Field(default_factory=PlatformShells)


class VersionProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: list[str]
    timeout_seconds: float = Field(default=5, gt=0, le=60)

    @field_validator("command")
    @classmethod
    def validate_command(cls, command: list[str]) -> list[str]:
        return _validate_command(command, "version probe command")


class ToolOutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred: str | None = None


class RawToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    command: list[str]
    executor: ExecutorType = ExecutorType.PROCESS
    short_description: str | None = None
    description: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    input_schema: dict[str, object] | None = None
    version_probe: VersionProbe | None = None
    approval: ApprovalLevel = ApprovalLevel.NEVER
    timeout: float = Field(default=1800, gt=0)
    output: ToolOutputConfig = Field(default_factory=ToolOutputConfig)
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def validate_command(cls, command: list[str]) -> list[str]:
        return _validate_command(command, "tool command")

    @field_validator("short_description", "description")
    @classmethod
    def normalize_descriptions(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @field_validator("capabilities", "synonyms")
    @classmethod
    def normalize_search_terms(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in values:
            value = item.strip()
            if value and value not in seen:
                normalized.append(value)
                seen.add(value)
        return normalized


class ToolRegistryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, le=1)
    execution_policy: ExecutionPolicy = ExecutionPolicy.REGISTERED_ONLY
    shells: ShellConfig = Field(default_factory=ShellConfig)
    tools: dict[str, RawToolDefinition] = Field(default_factory=dict)

    @field_validator("tools")
    @classmethod
    def validate_tool_ids(cls, tools: dict[str, RawToolDefinition]) -> dict[str, RawToolDefinition]:
        for tool_id in tools:
            if not tool_id or any(char.isspace() for char in tool_id):
                raise ValueError(f"invalid tool id: {tool_id!r}")
        return tools


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    enabled: bool
    command: list[str]
    executor: ExecutorType
    short_description: str | None
    description: str | None
    capabilities: list[str]
    synonyms: list[str]
    input_schema: dict[str, object] | None
    version_probe: VersionProbe | None
    approval_level: ApprovalLevel
    timeout_seconds: float
    output: ToolOutputConfig
    environment: dict[str, str]

    @classmethod
    def from_raw(cls, tool_id: str, raw: RawToolDefinition) -> ToolDefinition:
        return cls(
            id=tool_id,
            enabled=raw.enabled,
            command=raw.command,
            executor=raw.executor,
            short_description=raw.short_description,
            description=raw.description,
            capabilities=raw.capabilities,
            synonyms=raw.synonyms,
            input_schema=raw.input_schema,
            version_probe=raw.version_probe,
            approval_level=raw.approval,
            timeout_seconds=raw.timeout,
            output=raw.output,
            environment=raw.environment,
        )


class ToolSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    generation: int = Field(ge=1)
    source_digest: str
    definitions: dict[str, ToolDefinition]
    states: dict[str, ToolState]


def _validate_command(command: list[str], label: str) -> list[str]:
    if not command or any(not item or "\x00" in item for item in command):
        raise ValueError(f"{label} must contain non-empty argv elements")
    return command
