"""Provider-neutral MCP governance contracts."""

from enum import StrEnum

from pydantic import Field

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
