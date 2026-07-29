from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml
from agents import Model, ModelResponse, Usage, function_tool
from agents.tool_context import ToolContext
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from riftx.agent import (
    AgentCycle,
    AgentCycleOutput,
    AgentCycleStatus,
    AgentRuntimeServices,
    RiftXAgentContext,
    RiftXDatabaseSession,
    SQLAlchemyCheckpointStore,
    build_agent_tools,
)
from riftx.domain import Engagement, FindingSeverity, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import ProcessSupervisor, RunnerPaths
from riftx.skills import create_default_skill_registry
from riftx.tools import ToolRegistry

FIXTURE = Path(__file__).parents[2] / "tools" / "fixtures" / "fake_tool.py"


class SequenceModel(Model):
    def __init__(self, outputs: list[list[Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        input_value = args[1] if len(args) > 1 else kwargs["input"]
        tools = args[3] if len(args) > 3 else kwargs["tools"]
        self.calls.append(
            {
                "input": input_value,
                "tool_names": [tool.name for tool in tools],
            }
        )
        output = self.outputs[len(self.calls) - 1]
        return ModelResponse(
            output=output,
            usage=Usage(),
            response_id=f"response-{len(self.calls)}",
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async def generate() -> AsyncIterator[Any]:
            if False:
                yield None

        return generate()


async def _runtime(
    tmp_path: Path,
    *,
    execution_policy: str = "registered_only",
) -> tuple[
    Database,
    Run,
    RiftXAgentContext,
    AgentRuntimeServices,
    ProcessSupervisor,
]:
    await asyncio.to_thread(tmp_path.mkdir, parents=True, exist_ok=True)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Agent tests")
    )
    run = Run(
        id="run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Run the configured verification tool"),
        workspace_path=str(tmp_path),
    )
    await SQLAlchemyRunRepository(database.session_factory).create(run)

    config_path = tmp_path / "tools.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": execution_policy,
                "tools": {
                    "custom": {
                        "command": [sys.executable, str(FIXTURE)],
                        "executor": "process",
                        "capabilities": ["custom_verification"],
                        "timeout": 30,
                    },
                    "missing-binary": {
                        "command": ["definitely-not-a-riftx-command"],
                        "executor": "process",
                        "capabilities": ["unavailable"],
                    },
                },
            },
            sort_keys=False,
        )
    )
    registry = ToolRegistry(config_path, node_id="node-1")
    await registry.refresh()
    supervisor = ProcessSupervisor(
        SQLAlchemyExecutionRepository(database.session_factory),
        RunnerPaths(tmp_path / "state"),
        termination_grace_seconds=0.1,
    )
    services = AgentRuntimeServices(
        tool_registry=registry,
        skill_registry=create_default_skill_registry(),
        supervisor=supervisor,
        finding_repository=SQLAlchemyFindingRepository(database.session_factory),
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
    )
    context = RiftXAgentContext.from_run(run, registry, agent_step_id="step-1")
    return database, run, context, services, supervisor


def _message(output: AgentCycleOutput) -> list[Any]:
    return [
        ResponseOutputMessage(
            id="message-1",
            role="assistant",
            status="completed",
            type="message",
            content=[
                ResponseOutputText(
                    annotations=[],
                    text=output.model_dump_json(),
                    type="output_text",
                )
            ],
        )
    ]


def _tool_call(name: str, arguments: dict[str, Any], *, call_id: str = "call-1") -> list[Any]:
    return [
        ResponseFunctionToolCall(
            arguments=json.dumps(arguments),
            call_id=call_id,
            name=name,
            type="function_call",
            status="completed",
        )
    ]


async def test_agent_cycle_runs_configured_tool_and_persists_timeline(tmp_path: Path) -> None:
    database, _, context, services, supervisor = await _runtime(tmp_path)
    model = SequenceModel(
        [
            _tool_call(
                "run_registered_tool",
                {"tool_id": "custom", "args": ["hello"], "timeout_seconds": None},
            ),
            _message(
                AgentCycleOutput(
                    assistant_message="The configured tool completed.",
                    plan_summary="Use the available custom verifier and inspect its result.",
                )
            ),
        ]
    )
    cycle = AgentCycle(
        services=services,
        session_factory=database.session_factory,
        checkpoint_store=SQLAlchemyCheckpointStore(database.session_factory),
        model=model,
    )

    result = await cycle.run(context)

    assert result.status is AgentCycleStatus.COMPLETED
    assert result.output is not None
    assert result.output.assistant_message == "The configured tool completed."
    assert set(model.calls[0]["tool_names"]) == {
        "list_available_tools",
        "run_registered_tool",
        "create_finding",
        "update_plan",
        "complete_run",
    }
    assert "missing-binary" not in {item.id for item in context.available_tools}
    assert "stdout_excerpt" in json.dumps(model.calls[1]["input"])

    events = await services.event_repository.list_after("run-1")
    event_types = [event.event_type for event in events]
    assert "agent.tool_started" in event_types
    assert "agent.tool_completed" in event_types
    assert event_types[-3:] == ["agent.message", "agent.plan_updated", "agent.cycle_completed"]
    history = await RiftXDatabaseSession("run-1", database.session_factory).get_items()
    assert history
    await supervisor.close()
    await database.dispose()


async def test_agent_cycle_replans_when_requested_tool_is_missing(tmp_path: Path) -> None:
    database, _, context, services, supervisor = await _runtime(tmp_path)
    model = SequenceModel(
        [
            _tool_call(
                "run_registered_tool",
                {"tool_id": "not-installed", "args": [], "timeout_seconds": None},
            ),
            _message(
                AgentCycleOutput(
                    assistant_message="The requested tool is unavailable; I selected another plan.",
                    plan_summary="Use only available tools and avoid the missing dependency.",
                    needs_input=True,
                )
            ),
        ]
    )
    cycle = AgentCycle(
        services=services,
        session_factory=database.session_factory,
        checkpoint_store=SQLAlchemyCheckpointStore(database.session_factory),
        model=model,
    )

    result = await cycle.run(context)

    assert result.status is AgentCycleStatus.COMPLETED
    assert result.output is not None and result.output.needs_input is True
    assert len(model.calls) == 2
    assert "not available" in json.dumps(model.calls[1]["input"])
    events = await services.event_repository.list_after("run-1")
    assert "agent.tool_failed" in [event.event_type for event in events]
    await supervisor.close()
    await database.dispose()


async def test_agent_cycle_create_finding_tool_persists_structured_finding(
    tmp_path: Path,
) -> None:
    database, _, context, services, supervisor = await _runtime(tmp_path)
    model = SequenceModel(
        [
            _tool_call(
                "create_finding",
                {
                    "title": "Exposed test service",
                    "severity": "high",
                    "affected_assets": ["example.test"],
                    "description": "A test service responded on an exposed port.",
                    "evidence": [
                        {
                            "artifact_id": None,
                            "execution_id": None,
                            "description": "Observed response",
                            "location": "model-fixture",
                        }
                    ],
                    "reproduction_steps": ["Run the authorized verifier"],
                    "impact": "Test impact",
                    "recommendation": "Restrict access",
                },
            ),
            _message(
                AgentCycleOutput(
                    assistant_message="A supported finding was recorded.",
                    plan_summary="Record verified evidence as a structured finding.",
                )
            ),
        ]
    )
    cycle = AgentCycle(
        services=services,
        session_factory=database.session_factory,
        checkpoint_store=SQLAlchemyCheckpointStore(database.session_factory),
        model=model,
    )

    await cycle.run(context)

    findings = list(await services.finding_repository.list("run-1"))
    assert len(findings) == 1
    assert findings[0].title == "Exposed test service"
    assert findings[0].severity is FindingSeverity.HIGH
    events = await services.event_repository.list_after("run-1")
    assert "finding.created" in [event.event_type for event in events]
    await supervisor.close()
    await database.dispose()


async def test_agent_cycle_checkpoints_and_resumes_approval(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    database, _, context, services, supervisor = await _runtime(tmp_path)

    @function_tool(needs_approval=True)
    async def approval_tool(
        ctx: ToolContext[RiftXAgentContext],
        value: str,
    ) -> str:
        """Return an approved test value."""

        return f"approved:{value}:{ctx.tool_call_id}"

    monkeypatch.setattr(
        "riftx.agent.factory.build_agent_tools",
        lambda _: [approval_tool],
    )
    model = SequenceModel(
        [
            _tool_call("approval_tool", {"value": "safe"}, call_id="approval-call"),
            _message(
                AgentCycleOutput(
                    assistant_message="The approved action completed.",
                    plan_summary="Resume the exact interrupted tool call.",
                )
            ),
        ]
    )
    store = SQLAlchemyCheckpointStore(database.session_factory)
    cycle = AgentCycle(
        services=services,
        session_factory=database.session_factory,
        checkpoint_store=store,
        model=model,
    )

    interrupted = await cycle.run(context)
    assert interrupted.status is AgentCycleStatus.INTERRUPTED
    assert interrupted.checkpoint_id is not None
    assert interrupted.interruptions[0].call_id == "approval-call"

    resumed = await cycle.run(
        context,
        checkpoint_id=interrupted.checkpoint_id,
        approval_decisions={"approval-call": True},
    )

    assert resumed.status is AgentCycleStatus.COMPLETED
    checkpoint = await store.get(interrupted.checkpoint_id)
    assert checkpoint is not None and checkpoint.status == "resolved"
    assert "approved:safe:approval-call" in json.dumps(model.calls[1]["input"])
    await supervisor.close()
    await database.dispose()


async def test_agent_tools_follow_shell_execution_policy(tmp_path: Path) -> None:
    database, _, _, registered_services, supervisor = await _runtime(tmp_path / "registered")
    registered_names = {tool.name for tool in build_agent_tools(registered_services)}
    assert "run_shell" not in registered_names
    await supervisor.close()
    await database.dispose()

    database, _, _, open_services, supervisor = await _runtime(
        tmp_path / "open",
        execution_policy="open",
    )
    open_names = {tool.name for tool in build_agent_tools(open_services)}
    assert "run_shell" in open_names
    await supervisor.close()
    await database.dispose()
