from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import yaml

from riftx.domain import Engagement, ExecutionStatus, Objective, Run
from riftx.execution import DeferredExecutionDispatcher, ExecutionService
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunLeaseRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.runner import ProcessSupervisor, RunnerPaths
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import AgentEngineEvent, AgentEngineEventType, AgentEngineState
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import (
    CompiledContext,
    DynamicToolContextCompiler,
    RunCycleRequest,
)
from riftx.runtime.types import AgentSession, YieldReason
from riftx.tools import RESIDENT_TOOL_IDS, ToolContextManager, ToolRegistry, ToolSearchRequest


class GateEngineRun:
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[AgentEngineEvent]:
        for event in self._events:
            yield event

    async def suspend(self) -> AgentEngineState:
        return AgentEngineState(
            engine_type="fake",
            engine_version="1",
            provider="fake",
            model="fake-model",
            serialized_state={"wave": "b"},
        )

    async def cancel(self) -> None:
        return None


class GateEngine:
    def __init__(self, launch_argv: list[str], cwd: Path) -> None:
        self._launch_argv = launch_argv
        self._cwd = cwd
        self.requests: list[object] = []

    async def start(self, request: object) -> GateEngineRun:
        self.requests.append(request)
        if len(self.requests) == 1:
            return GateEngineRun(
                [
                    AgentEngineEvent(
                        sequence=1,
                        event_type=AgentEngineEventType.TOOL_CALL_READY,
                        data={
                            "call_id": "wave-b-smb-call",
                            "tool_id": "netexec-smb",
                            "arguments": {"target": "authorized.example"},
                            "execution": {
                                "node_id": "local",
                                "executor_type": "process",
                                "cwd": str(self._cwd),
                                "argv": self._launch_argv,
                            },
                        },
                    ),
                    AgentEngineEvent(
                        sequence=2,
                        event_type=AgentEngineEventType.RUN_COMPLETED,
                    ),
                ]
            )
        return GateEngineRun(
            [
                AgentEngineEvent(
                    sequence=1,
                    event_type=AgentEngineEventType.ASSISTANT_MESSAGE,
                    data={"content": "SMB enumeration execution completed; continue analysis."},
                ),
                AgentEngineEvent(sequence=2, event_type=AgentEngineEventType.RUN_COMPLETED),
            ]
        )

    async def resume(self, request: object) -> GateEngineRun:
        return await self.start(request)


def _write_eighty_tools(path: Path) -> None:
    tools: dict[str, object] = {}
    for index in range(79):
        tools[f"utility-{index:02d}"] = {
            "command": [sys.executable],
            "short_description": f"Utility {index}",
            "capabilities": [f"utility_{index}"],
        }
    tools["netexec-smb"] = {
        "command": [sys.executable],
        "short_description": "Enumerate SMB shares, users, and hosts",
        "description": "Long-running authorized SMB enumeration through the RiftX Runner.",
        "capabilities": ["smb_enumeration", "network_share_discovery"],
        "synonyms": ["Windows share discovery", "CIFS recon"],
    }
    path.write_text(yaml.safe_dump({"version": 1, "tools": tools}, sort_keys=False))


async def test_wave_b_dynamic_discovery_deferred_execution_and_continuation(
    tmp_path: Path,
) -> None:
    tool_config = tmp_path / "tools.yaml"
    _write_eighty_tools(tool_config)
    tool_registry = ToolRegistry(tool_config, node_id="local")
    await tool_registry.refresh()
    tool_context = ToolContextManager(tool_registry)
    compiler = DynamicToolContextCompiler(tool_context)

    initial = await compiler.compile(_compile_request())
    registered_residents = [tool_id for tool_id in RESIDENT_TOOL_IDS if tool_id != "run_shell"]
    assert [schema["name"] for schema in initial.available_tools] == registered_residents
    assert len(initial.context_manifest["hidden_available_tools"]) == 80

    search_results = await tool_context.search_tools(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        request=ToolSearchRequest(query="SMB enumeration tools"),
    )
    assert search_results[0].tool.id == "netexec-smb"
    selected_tool = await tool_context.get_tool(
        "netexec-smb",
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
    )
    assert selected_tool.full_schema["name"] == "netexec-smb"
    selected = await compiler.compile(_compile_request())
    assert [schema["name"] for schema in selected.available_tools] == [
        *registered_residents,
        "netexec-smb",
    ]

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'wave-b.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Enumerate authorized SMB services"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(
        AgentSession(
            id="session-1",
            run_id="run-1",
            agent_type="primary",
            model_profile="fake-model",
        )
    )
    tool_calls = SQLAlchemyToolCallIntentRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = ProcessSupervisor(executions, RunnerPaths(tmp_path / "runner"))
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=supervisor,
        run_repository=runs,
    )
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=service,
    )
    engine = GateEngine(
        tool_registry.build_argv(
            "netexec-smb",
            ["-c", "import time; time.sleep(0.15); print('smb enumeration done')"],
        ),
        tmp_path,
    )
    coordinator = RuntimeCoordinator(
        run_repository=runs,
        session_repository=sessions,
        cycle_repository=SQLAlchemyAgentCycleRepository(database.session_factory),
        step_repository=SQLAlchemyAgentStepRepository(database.session_factory),
        provider_state_repository=SQLAlchemyProviderStateRepository(database.session_factory),
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        lease_manager=DatabaseRunLeaseManager(
            SQLAlchemyRunLeaseRepository(database.session_factory)
        ),
        context_compiler=compiler,
        agent_engine=engine,
        deferred_execution_dispatcher=dispatcher,
    )

    yielded = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )
    assert yielded.yield_reason is YieldReason.TOOL_RUNNING
    assert yielded.waiting_execution_id is not None
    inspected = await service.get(yielded.waiting_execution_id)
    assert inspected.tool_id == "netexec-smb"
    assert inspected.status in {
        ExecutionStatus.STARTING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.COMPLETED,
    }

    completed = await service.wait(yielded.waiting_execution_id, timeout_seconds=2)
    assert completed.execution.status is ExecutionStatus.COMPLETED
    assert "smb enumeration done" in (completed.partial_output or "")

    continued = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-2")
    )
    assert continued.yield_reason is YieldReason.RUN_COMPLETED
    runtime_context = engine.requests[0].context
    assert isinstance(runtime_context, CompiledContext)
    assert runtime_context.context_manifest["dynamically_loaded_tools"] == ["netexec-smb"]

    await supervisor.close()
    await database.dispose()


def _compile_request():
    from riftx.runtime.lifecycle import ContextCompileRequest

    return ContextCompileRequest(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        model_profile="fake-model",
        objective="Enumerate authorized SMB services",
    )
