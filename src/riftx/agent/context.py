"""Serializable business context supplied to the primary agent."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import ApprovalLevel, ApprovalMode, ExecutorType, Run
from riftx.domain.base import new_id
from riftx.tools import ToolRegistry


class AgentToolSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    capabilities: list[str] = Field(default_factory=list)
    executor: ExecutorType
    approval_level: ApprovalLevel
    version: str | None = None


class RiftXAgentContext(BaseModel):
    """JSON-serializable context; runtime services deliberately live elsewhere."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str
    node_id: str
    agent_step_id: str
    objective: str
    success_criteria: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    approval_mode: ApprovalMode
    workspace: str
    available_tools: list[AgentToolSnapshot] = Field(default_factory=list)
    plan_summary: str = ""
    completion_requested: bool = False
    run_summary: str | None = None

    @classmethod
    def from_run(
        cls,
        run: Run,
        tool_registry: ToolRegistry,
        *,
        agent_step_id: str | None = None,
    ) -> RiftXAgentContext:
        tools = []
        for definition in tool_registry.available_tools():
            state = tool_registry.snapshot.states[definition.id]
            tools.append(
                AgentToolSnapshot(
                    id=definition.id,
                    capabilities=definition.capabilities,
                    executor=definition.executor,
                    approval_level=definition.approval_level,
                    version=state.version,
                )
            )
        return cls(
            run_id=run.id,
            node_id=run.node_id,
            agent_step_id=agent_step_id or new_id(),
            objective=run.objective.description,
            success_criteria=[item.description for item in run.success_criteria],
            entry_points=[f"{item.kind.value}:{item.value}" for item in run.entry_points],
            scope=_scope_items(run),
            approval_mode=run.approval_mode,
            workspace=run.workspace_path,
            available_tools=tools,
        )


def _scope_items(run: Run) -> list[str]:
    values: list[str] = []
    for prefix, items in (
        ("cidr", run.scope.cidrs),
        ("ip", run.scope.ips),
        ("domain", run.scope.domains),
        ("url", run.scope.url_prefixes),
        ("tag", run.scope.asset_tags),
        ("exclude", run.scope.exclusions),
    ):
        values.extend(f"{prefix}:{item}" for item in items)
    if run.scope.starts_at is not None:
        values.append(f"starts_at:{run.scope.starts_at.isoformat()}")
    if run.scope.ends_at is not None:
        values.append(f"ends_at:{run.scope.ends_at.isoformat()}")
    return values
