"""Provider-neutral MCP governance and discovery contracts."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from riftx.domain import ToolAvailability
from riftx.domain.base import DomainModel


class MCPCircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class MCPServerHealth(DomainModel):
    server_id: str = Field(min_length=1)
    circuit_state: MCPCircuitState = MCPCircuitState.CLOSED
    failure_count: int = Field(default=0, ge=0)
    cooldown_remaining_seconds: float = Field(default=0, ge=0)
    half_open_probe_in_flight: bool = False
    active_calls: int = Field(default=0, ge=0)
    completed_calls: int = Field(default=0, ge=0)
    failed_calls: int = Field(default=0, ge=0)


class MCPHealthSnapshot(DomainModel):
    active_calls: int = Field(default=0, ge=0)
    max_concurrent_total: int = Field(ge=1)
    max_concurrent_per_server: int = Field(ge=1)
    servers: list[MCPServerHealth] = Field(default_factory=list)


class MCPServerAvailability(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class MCPToolIndexEntry(DomainModel):
    """Bounded MCP metadata safe for search without exposing server configuration."""

    id: str = Field(min_length=1, max_length=64)
    server_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    title: str | None = Field(default=None, max_length=256)
    description: str = Field(min_length=1, max_length=1000)
    availability: ToolAvailability = ToolAvailability.AVAILABLE
    execution_type: Literal["mcp"] = "mcp"
    input_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None


class MCPToolSchema(DomainModel):
    tool_id: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=1)
    full_schema: dict[str, object]


class MCPServerSnapshot(DomainModel):
    server_id: str = Field(min_length=1, max_length=64)
    availability: MCPServerAvailability
    tool_count: int = Field(default=0, ge=0)
    rejected_tool_count: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=128)


class MCPRegistrySnapshot(DomainModel):
    generation: int = Field(ge=1)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    servers: list[MCPServerSnapshot] = Field(default_factory=list)
    tools: list[MCPToolIndexEntry] = Field(default_factory=list)
