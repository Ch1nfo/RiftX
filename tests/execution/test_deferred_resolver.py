from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from riftx.application.errors import ApplicationConflictError
from riftx.domain import Objective, Run
from riftx.execution import RegistryDeferredExecutionResolver
from riftx.runtime.engine import AgentEngineEvent, AgentEngineEventType
from riftx.runtime.types import AgentSession
from riftx.tools import ToolRegistry


@dataclass
class FakeRuns:
    run: Run

    async def get(self, run_id: str) -> Run | None:
        return self.run if self.run.id == run_id else None


async def test_registry_resolver_builds_trusted_process_spec(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "tools.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "registered_only",
                "tools": {
                    "scanner": {
                        "command": [sys.executable, "--batch"],
                        "executor": "process",
                        "timeout": 45,
                        "environment": {"SAFE": "1"},
                    }
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
        objective=Objective(description="test"),
        workspace_path=str(workspace),
    )
    resolver = RegistryDeferredExecutionResolver(runs=FakeRuns(run), registry=registry)

    spec = await resolver.resolve(
        session=AgentSession(id="session-1", run_id=run.id, model_profile="test"),
        event=AgentEngineEvent(
            sequence=1,
            event_type=AgentEngineEventType.TOOL_CALL_READY,
            data={
                "call_id": "call-1",
                "tool_id": "run_registered_tool",
                "arguments": {
                    "tool_id": "scanner",
                    "args": ["127.0.0.1"],
                    "environment": {"TARGET": "test"},
                },
            },
        ),
        tool_id="scanner",
    )

    assert spec.argv == [sys.executable, "--batch", "127.0.0.1"]
    assert spec.cwd == workspace
    assert spec.env == {"SAFE": "1", "TARGET": "test"}
    assert spec.timeout_seconds == 45


async def test_registry_resolver_rejects_workspace_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "tools.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "registered_only",
                "tools": {"scanner": {"command": [sys.executable]}},
            }
        )
    )
    registry = ToolRegistry(config, node_id="node-1")
    await registry.refresh()
    run = Run(
        id="run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="test"),
        workspace_path=str(workspace),
    )
    resolver = RegistryDeferredExecutionResolver(runs=FakeRuns(run), registry=registry)

    with pytest.raises(ApplicationConflictError, match="cwd must remain"):
        await resolver.resolve(
            session=AgentSession(id="session-1", run_id=run.id, model_profile="test"),
            event=AgentEngineEvent(
                sequence=1,
                event_type=AgentEngineEventType.TOOL_CALL_READY,
                data={
                    "call_id": "call-1",
                    "tool_id": "scanner",
                    "arguments": {"cwd": "../outside"},
                },
            ),
            tool_id="scanner",
        )
