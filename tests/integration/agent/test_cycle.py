from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml
from agents import Model, ModelProvider, ModelResponse, Usage, function_tool
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
from riftx.application.services import (
    ArtifactApplicationService,
    FindingApplicationService,
    TerminalApplicationService,
)
from riftx.domain import ApprovalMode, Engagement, FindingSeverity, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.runner import ProcessSupervisor, RunnerPaths, TerminalSupervisor
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


class RecordingModelProvider(ModelProvider):
    def __init__(self, model: Model) -> None:
        self.model = model
        self.requested_profiles: list[str | None] = []

    def get_model(self, model_name: str | None) -> Model:
        self.requested_profiles.append(model_name)
        return self.model


async def _runtime(
    tmp_path: Path,
    *,
    execution_policy: str = "registered_only",
    approval_mode: ApprovalMode = ApprovalMode.BALANCED,
    tool_approval: str = "never",
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
        approval_mode=approval_mode,
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
                        "approval": tool_approval,
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
    paths = RunnerPaths(tmp_path / "state")
    execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = ProcessSupervisor(
        execution_repository,
        paths,
        termination_grace_seconds=0.1,
    )
    run_repository = SQLAlchemyRunRepository(database.session_factory)
    event_repository = SQLAlchemyRunEventRepository(database.session_factory)
    finding_repository = SQLAlchemyFindingRepository(database.session_factory)
    artifact_repository = SQLAlchemyArtifactRepository(database.session_factory)
    terminal_supervisor = TerminalSupervisor(
        terminal_repository=SQLAlchemyTerminalRepository(database.session_factory),
        execution_repository=execution_repository,
        event_repository=event_repository,
        paths=paths,
    )
    artifact_service = ArtifactApplicationService(
        run_repository=run_repository,
        execution_repository=execution_repository,
        artifact_repository=artifact_repository,
        event_repository=event_repository,
        paths=paths,
    )
    services = AgentRuntimeServices(
        tool_registry=registry,
        skill_registry=create_default_skill_registry(),
        supervisor=supervisor,
        finding_repository=finding_repository,
        event_repository=event_repository,
        approval_repository=SQLAlchemyApprovalRepository(database.session_factory),
        artifact_service=artifact_service,
        terminal_service=TerminalApplicationService(
            run_repository=run_repository,
            supervisor=terminal_supervisor,
        ),
        finding_service=FindingApplicationService(
            run_repository=run_repository,
            finding_repository=finding_repository,
            artifact_repository=artifact_repository,
            execution_repository=execution_repository,
            event_repository=event_repository,
        ),
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
        "open_terminal",
        "read_terminal",
        "send_terminal_input",
        "close_terminal",
        "create_finding",
        "add_artifact",
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


async def test_agent_cycle_uses_run_model_profile(tmp_path: Path) -> None:
    database, _, context, services, supervisor = await _runtime(tmp_path)
    context.model_profile = "fast"
    model = SequenceModel(
        [
            _message(
                AgentCycleOutput(
                    assistant_message="Selected profile completed the cycle.",
                    plan_summary="Use the per-run model profile.",
                )
            )
        ]
    )
    provider = RecordingModelProvider(model)
    cycle = AgentCycle(
        services=services,
        session_factory=database.session_factory,
        checkpoint_store=SQLAlchemyCheckpointStore(database.session_factory),
        model="primary",
        model_provider=provider,
    )

    result = await cycle.run(context)

    assert result.status is AgentCycleStatus.COMPLETED
    assert provider.requested_profiles == ["fast"]
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


async def test_balanced_sensitive_tool_interrupts_and_run_grant_skips_future_prompt(
    tmp_path: Path,
) -> None:
    database, _, context, services, supervisor = await _runtime(
        tmp_path,
        tool_approval="sensitive",
    )
    first_cycle = AgentCycle(
        services=services,
        session_factory=database.session_factory,
        checkpoint_store=SQLAlchemyCheckpointStore(database.session_factory),
        model=SequenceModel(
            [
                _tool_call(
                    "run_registered_tool",
                    {"tool_id": "custom", "args": ["hello"], "timeout_seconds": None},
                    call_id="approval-call",
                )
            ]
        ),
    )

    interrupted = await first_cycle.run(context)
    assert interrupted.status is AgentCycleStatus.INTERRUPTED
    assert [item.call_id for item in interrupted.interruptions] == ["approval-call"]

    assert services.approval_repository is not None
    await services.approval_repository.grant_for_run(
        context.run_id,
        "custom",
        created_by="tester",
    )
    granted_cycle = AgentCycle(
        services=services,
        session_factory=database.session_factory,
        checkpoint_store=SQLAlchemyCheckpointStore(database.session_factory),
        model=SequenceModel(
            [
                _tool_call(
                    "run_registered_tool",
                    {"tool_id": "custom", "args": ["hello"], "timeout_seconds": None},
                    call_id="granted-call",
                ),
                _message(
                    AgentCycleOutput(
                        assistant_message="The granted tool completed.",
                        plan_summary="Continue with the approved tool.",
                    )
                ),
            ]
        ),
    )
    completed = await granted_cycle.run(context)
    assert completed.status is AgentCycleStatus.COMPLETED

    await supervisor.close()
    await database.dispose()


async def test_manual_mode_interrupts_never_approval_tool(tmp_path: Path) -> None:
    database, _, context, services, supervisor = await _runtime(
        tmp_path,
        approval_mode=ApprovalMode.MANUAL,
        tool_approval="never",
    )
    cycle = AgentCycle(
        services=services,
        session_factory=database.session_factory,
        checkpoint_store=SQLAlchemyCheckpointStore(database.session_factory),
        model=SequenceModel(
            [
                _tool_call(
                    "run_registered_tool",
                    {"tool_id": "custom", "args": [], "timeout_seconds": None},
                )
            ]
        ),
    )

    result = await cycle.run(context)
    assert result.status is AgentCycleStatus.INTERRUPTED

    await supervisor.close()
    await database.dispose()


async def test_auto_mode_runs_sensitive_tool_without_interruption(tmp_path: Path) -> None:
    database, _, context, services, supervisor = await _runtime(
        tmp_path,
        approval_mode=ApprovalMode.AUTO,
        tool_approval="always",
    )
    cycle = AgentCycle(
        services=services,
        session_factory=database.session_factory,
        checkpoint_store=SQLAlchemyCheckpointStore(database.session_factory),
        model=SequenceModel(
            [
                _tool_call(
                    "run_registered_tool",
                    {"tool_id": "custom", "args": [], "timeout_seconds": None},
                ),
                _message(
                    AgentCycleOutput(
                        assistant_message="Auto mode completed the tool.",
                        plan_summary="Run without prompting in auto mode.",
                    )
                ),
            ]
        ),
    )

    result = await cycle.run(context)
    assert result.status is AgentCycleStatus.COMPLETED

    await supervisor.close()
    await database.dispose()


async def _invoke_agent_tool(
    services: AgentRuntimeServices,
    context: RiftXAgentContext,
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str,
) -> str:
    tool = next(item for item in build_agent_tools(services) if item.name == name)
    tool_context = ToolContext(
        context,
        usage=Usage(),
        tool_name=name,
        tool_call_id=call_id,
        tool_arguments=json.dumps(arguments),
    )
    result = await tool.on_invoke_tool(tool_context, json.dumps(arguments))
    assert isinstance(result, str)
    return result


async def test_agent_base_tools_manage_artifacts_and_terminal_sessions(tmp_path: Path) -> None:
    database, _, context, services, supervisor = await _runtime(
        tmp_path,
        execution_policy="open",
        approval_mode=ApprovalMode.AUTO,
    )
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("durable evidence")

    artifact_payload = json.loads(
        await _invoke_agent_tool(
            services,
            context,
            "add_artifact",
            {"source_path": str(evidence), "description": "Agent evidence"},
            call_id="artifact-call",
        )
    )
    terminal_payload = json.loads(
        await _invoke_agent_tool(
            services,
            context,
            "open_terminal",
            {
                "argv": [
                    sys.executable,
                    "-u",
                    "-c",
                    "print('ready'); print(input())",
                ]
            },
            call_id="terminal-open",
        )
    )
    session_id = terminal_payload["id"]

    for _ in range(100):
        first_read = json.loads(
            await _invoke_agent_tool(
                services,
                context,
                "read_terminal",
                {"session_id": session_id, "cursor": 0},
                call_id="terminal-read-ready",
            )
        )
        if "ready" in first_read["data"]:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("terminal did not emit readiness output")

    await _invoke_agent_tool(
        services,
        context,
        "send_terminal_input",
        {"session_id": session_id, "data": "hello-agent\n"},
        call_id="terminal-write",
    )
    cursor = first_read["next_cursor"]
    for _ in range(100):
        second_read = json.loads(
            await _invoke_agent_tool(
                services,
                context,
                "read_terminal",
                {"session_id": session_id, "cursor": cursor},
                call_id="terminal-read-result",
            )
        )
        if "hello-agent" in second_read["data"]:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("terminal did not echo Agent input")

    closed = json.loads(
        await _invoke_agent_tool(
            services,
            context,
            "close_terminal",
            {"session_id": session_id},
            call_id="terminal-close",
        )
    )
    assert artifact_payload["sha256"]
    assert artifact_payload["size"] == len("durable evidence")
    assert closed["status"] == "closed"
    await supervisor.close()
    await database.dispose()
