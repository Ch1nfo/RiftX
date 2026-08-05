from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from riftx.application.errors import ApplicationConflictError
from riftx.domain import Objective, Run
from riftx.execution import DeferredExecutionDispatcher, RegistryDeferredExecutionResolver
from riftx.runtime.engine import AgentEngineEvent, AgentEngineEventType
from riftx.runtime.types import AgentCycle, AgentSession, AgentStep, AgentStepType, ToolCallIntent
from riftx.tools import ToolContextManager, ToolNotFoundError, ToolRegistry


@dataclass
class FakeRuns:
    run: Run

    async def get(self, run_id: str) -> Run | None:
        return self.run if self.run.id == run_id else None


@dataclass
class FakeExecutionService:
    run_repository: FakeRuns


class ExplodingToolContext:
    def __init__(self) -> None:
        self.called = False

    async def assert_allowed(self, *args: object, **kwargs: object) -> None:
        self.called = True
        raise AssertionError("tool context must not run before RunKind admission")

    async def assert_selected(self, *args: object, **kwargs: object) -> None:
        self.called = True
        raise AssertionError("tool context must not run before RunKind admission")


class FakeToolCalls:
    def __init__(self) -> None:
        self.item: ToolCallIntent | None = None

    async def get(self, intent_id: str) -> ToolCallIntent | None:
        return self.item if self.item is not None and self.item.id == intent_id else None

    async def create(self, intent: ToolCallIntent) -> ToolCallIntent:
        self.item = intent
        return intent


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
        kind="general",
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
        kind="general",
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


async def test_code_audit_resolver_denies_before_tool_context_and_path_resolution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "tools.yaml"
    config.write_text(yaml.safe_dump({"version": 1, "tools": {}}))
    run = Run(
        kind="code_audit",
        id="run-audit",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="test"),
        workspace_path=str(workspace),
    )
    tool_context = ExplodingToolContext()
    resolver = RegistryDeferredExecutionResolver(
        runs=FakeRuns(run),
        registry=ToolRegistry(config, node_id="node-1"),
        tool_context=tool_context,  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await resolver.resolve(
            session=AgentSession(id="session-audit", run_id=run.id, model_profile="test"),
            event=AgentEngineEvent(
                sequence=1,
                event_type=AgentEngineEventType.TOOL_CALL_READY,
                data={
                    "call_id": "call-audit",
                    "tool_id": "unknown-tool",
                    "arguments": {"cwd": "../outside"},
                },
            ),
            tool_id="unknown-tool",
        )

    assert captured.value.code == "run_kind_operation_unsupported"
    assert tool_context.called is False


async def test_registry_resolver_enforces_subagent_tool_allowlist(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "tools.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "registered_only",
                "tools": {
                    "assigned": {"command": [sys.executable]},
                    "unassigned": {"command": [sys.executable]},
                },
            }
        )
    )
    registry = ToolRegistry(config, node_id="node-1")
    await registry.refresh()
    run = Run(
        kind="general",
        id="run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="test"),
        workspace_path=str(workspace),
    )
    tools = ToolContextManager(registry)
    await tools.restrict_tools(
        ["assigned"],
        run_id=run.id,
        session_id="subagent-1",
        agent_id="subagent:recon",
    )
    resolver = RegistryDeferredExecutionResolver(
        runs=FakeRuns(run),
        registry=registry,
        tool_context=tools,
    )

    with pytest.raises(ToolNotFoundError):
        await resolver.resolve(
            session=AgentSession(
                id="subagent-1",
                run_id=run.id,
                model_profile="test",
                agent_type="subagent:recon",
            ),
            event=AgentEngineEvent(
                sequence=1,
                event_type=AgentEngineEventType.TOOL_CALL_READY,
                data={"call_id": "call-1", "tool_id": "unassigned", "arguments": {}},
            ),
            tool_id="unassigned",
        )


async def test_registered_tool_must_be_selected_and_raw_spec_cannot_override_registry(
    tmp_path: Path,
) -> None:
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
                        "command": [sys.executable, "--safe-registry-prefix"],
                        "executor": "process",
                    }
                },
            }
        )
    )
    registry = ToolRegistry(config, node_id="node-1")
    await registry.refresh()
    run = Run(
        kind="general",
        id="run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="test"),
        workspace_path=str(workspace),
    )
    tools = ToolContextManager(registry)
    resolver = RegistryDeferredExecutionResolver(
        runs=FakeRuns(run),
        registry=registry,
        tool_context=tools,
    )
    session = AgentSession(id="session-1", run_id=run.id, model_profile="test")
    event = AgentEngineEvent(
        sequence=1,
        event_type=AgentEngineEventType.TOOL_CALL_READY,
        data={
            "call_id": "call-1",
            "tool_id": "run_registered_tool",
            "arguments": {"tool_id": "scanner", "args": ["127.0.0.1"]},
            "execution": {
                "node_id": "attacker-node",
                "executor_type": "shell",
                "cwd": "/",
                "command_text": "unsafe raw model command",
            },
        },
    )

    with pytest.raises(ToolNotFoundError):
        await resolver.resolve(session=session, event=event, tool_id="scanner")

    await tools.get_tool(
        "scanner",
        run_id=run.id,
        session_id=session.id,
        agent_id=session.agent_type,
    )
    tool_calls = FakeToolCalls()
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,  # type: ignore[arg-type]
        execution_service=FakeExecutionService(FakeRuns(run)),  # type: ignore[arg-type]
        resolver=resolver,
    )
    intent = await dispatcher.prepare(
        session=session,
        cycle=AgentCycle(id="cycle-1", run_id=run.id, session_id=session.id, sequence=1),
        step=AgentStep(
            id="step-1",
            cycle_id="cycle-1",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        ),
        event=event,
    )

    assert intent.execution_spec is not None
    assert intent.execution_spec["node_id"] == "node-1"
    assert intent.execution_spec["executor_type"] == "process"
    assert intent.execution_spec["argv"] == [
        sys.executable,
        "--safe-registry-prefix",
        "127.0.0.1",
    ]
    assert intent.execution_spec["cwd"] == str(workspace)
    assert intent.execution_spec["command_text"] is None


async def test_same_engine_call_id_in_distinct_cycles_creates_distinct_intents(
    tmp_path: Path,
) -> None:
    tool_calls = FakeToolCalls()
    run = Run(
        kind="general",
        id="run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="test"),
        workspace_path=str(tmp_path),
    )
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,  # type: ignore[arg-type]
        execution_service=FakeExecutionService(FakeRuns(run)),  # type: ignore[arg-type]
    )
    session = AgentSession(id="session-1", run_id=run.id, model_profile="test")
    event = AgentEngineEvent(
        sequence=1,
        event_type=AgentEngineEventType.TOOL_CALL_READY,
        data={
            "call_id": "provider-call-1",
            "tool_id": "scanner",
            "arguments": {"target": "127.0.0.1"},
            "execution": {
                "node_id": "node-1",
                "executor_type": "process",
                "cwd": str(tmp_path),
                "argv": [sys.executable, "--version"],
            },
        },
    )

    first = await dispatcher.prepare(
        session=session,
        cycle=AgentCycle(id="cycle-1", run_id="run-1", session_id=session.id, sequence=1),
        step=AgentStep(
            id="step-1",
            cycle_id="cycle-1",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        ),
        event=event,
    )
    second = await dispatcher.prepare(
        session=session,
        cycle=AgentCycle(id="cycle-2", run_id="run-1", session_id=session.id, sequence=2),
        step=AgentStep(
            id="step-2",
            cycle_id="cycle-2",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        ),
        event=event,
    )

    assert first.id != second.id
    assert first.cycle_id == "cycle-1"
    assert second.cycle_id == "cycle-2"
