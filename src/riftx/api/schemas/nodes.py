"""Schemas for runner registration and execution-node management."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from riftx.domain import Execution, Node, NodeStatus


class RegisterNodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=64)
    architecture: str = Field(min_length=1, max_length=64)
    runner_version: str = Field(default="unknown", min_length=1, max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=1000)
    labels: dict[str, str] = Field(default_factory=dict)


class HeartbeatNodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: NodeStatus = NodeStatus.ONLINE
    runner_version: str | None = Field(default=None, min_length=1, max_length=64)
    capabilities: list[str] | None = Field(default=None, max_length=1000)
    labels: dict[str, str] | None = None

    @field_validator("status")
    @classmethod
    def validate_live_status(cls, value: NodeStatus) -> NodeStatus:
        if value not in {NodeStatus.ONLINE, NodeStatus.DEGRADED}:
            raise ValueError("heartbeat status must be online or degraded")
        return value


class NodeResponse(BaseModel):
    id: str
    name: str
    platform: str
    architecture: str
    runner_version: str
    status: NodeStatus
    capabilities: list[str]
    labels: dict[str, str]
    shell: str | None = None
    working_directory: str | None = None
    tool_count: int | None = None
    active_execution_ids: list[str] = Field(default_factory=list)
    current_run_ids: list[str] = Field(default_factory=list)
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(
        cls,
        node: Node,
        *,
        active_executions: list[Execution] | None = None,
        tool_count: int | None = None,
    ) -> "NodeResponse":
        active = active_executions or []
        configured_tool_count = tool_count
        if configured_tool_count is None:
            try:
                configured_tool_count = int(node.labels["tool_count"])
            except (KeyError, TypeError, ValueError):
                configured_tool_count = None
        return cls(
            id=node.id,
            name=node.name,
            platform=node.platform,
            architecture=node.architecture,
            runner_version=node.runner_version,
            status=node.status,
            capabilities=node.capabilities,
            labels=node.labels,
            shell=node.labels.get("shell"),
            working_directory=node.labels.get("working_directory"),
            tool_count=configured_tool_count,
            active_execution_ids=[execution.id for execution in active],
            current_run_ids=list(dict.fromkeys(execution.run_id for execution in active)),
            last_seen_at=node.last_seen_at,
            created_at=node.created_at,
            updated_at=node.updated_at,
        )


class NodeRegistrationResponse(BaseModel):
    node: NodeResponse
    created: bool
    runner_token: str


class NodeListResponse(BaseModel):
    items: list[NodeResponse]
