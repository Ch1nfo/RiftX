from pathlib import Path

import yaml

from riftx.agent import RiftXAgentContext
from riftx.domain import (
    ApprovalMode,
    EntryPoint,
    EntryPointKind,
    Objective,
    Run,
    Scope,
    SuccessCriterion,
)
from riftx.tools import ToolRegistry


async def test_agent_context_contains_only_available_node_tools(tmp_path: Path) -> None:
    config = tmp_path / "tools.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "available": {"command": ["echo"], "capabilities": ["verify"]},
                    "missing": {"command": ["not-a-real-riftx-command"]},
                },
            }
        )
    )
    registry = ToolRegistry(config, node_id="node-1")
    await registry.refresh()
    run = Run(
        id="run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Verify target"),
        success_criteria=[SuccessCriterion(description="Evidence collected")],
        entry_points=[EntryPoint(kind=EntryPointKind.DOMAIN, value="example.test")],
        scope=Scope(domains=["example.test"], exclusions=["admin.example.test"]),
        approval_mode=ApprovalMode.MANUAL,
        model_profile="fast",
        workspace_path=str(tmp_path),
    )

    context = RiftXAgentContext.from_run(run, registry, agent_step_id="step-1")

    assert [tool.id for tool in context.available_tools] == ["available"]
    assert context.success_criteria == ["Evidence collected"]
    assert context.entry_points == ["domain:example.test"]
    assert context.scope == ["domain:example.test", "exclude:admin.example.test"]
    assert context.model_dump(mode="json")["approval_mode"] == "manual"
    assert context.model_profile == "fast"
