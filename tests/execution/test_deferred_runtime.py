from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

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
from riftx.runtime.engine import (
    AgentEngineEvent,
    AgentEngineEventType,
    AgentEngineState,
)
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import MinimalContextCompiler, RunCycleRequest
from riftx.runtime.types import AgentSession, ToolCallStatus, YieldReason


class DeferredEngineRun:
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[AgentEngineEvent]:
        for item in self._events:
            yield item

    async def suspend(self) -> AgentEngineState:
        return AgentEngineState(
            engine_type="fake",
            engine_version="1",
            provider="fake",
            model="fake-model",
            serialized_state={"deferred": True},
        )

    async def cancel(self) -> None:
        return None


class DeferredEngine:
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        self._events = events

    async def start(self, request: object) -> DeferredEngineRun:
        return DeferredEngineRun(self._events)

    async def resume(self, request: object) -> DeferredEngineRun:
        return DeferredEngineRun(self._events)


async def test_runtime_retry_yields_same_deferred_execution_without_relaunch(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'deferred.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Run one deferred tool"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(
        AgentSession(id="session-1", run_id="run-1", model_profile="fake-model")
    )
    tool_calls = SQLAlchemyToolCallIntentRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = ProcessSupervisor(executions, RunnerPaths(tmp_path / "runner"))
    execution_service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=supervisor,
    )
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=execution_service,
    )
    events = [
        AgentEngineEvent(
            sequence=1,
            event_type=AgentEngineEventType.TOOL_CALL_READY,
            data={
                "call_id": "call-stable-1",
                "tool_id": "run_shell",
                "arguments": {"command": "deferred"},
                "execution": {
                    "node_id": "local",
                    "executor_type": "process",
                    "cwd": str(tmp_path),
                    "argv": [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(0.15); print('deferred done')",
                    ],
                },
            },
        ),
        AgentEngineEvent(sequence=2, event_type=AgentEngineEventType.RUN_COMPLETED),
    ]
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
        context_compiler=MinimalContextCompiler(),
        agent_engine=DeferredEngine(events),
        deferred_execution_dispatcher=dispatcher,
    )

    first = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )
    retried = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-2")
    )

    assert first.yield_reason is YieldReason.TOOL_RUNNING
    assert first.waiting_execution_id is not None
    assert retried.yield_reason is YieldReason.TOOL_RUNNING
    assert retried.waiting_execution_id == first.waiting_execution_id
    persisted = await executions.list("run-1")
    assert len(persisted) == 1
    intent = await tool_calls.get(persisted[0].tool_call_id or "")
    assert intent is not None
    assert intent.status in {ToolCallStatus.EXECUTING, ToolCallStatus.COMPLETED}

    completed = await execution_service.wait(first.waiting_execution_id, timeout_seconds=2)
    assert completed.execution.status is ExecutionStatus.COMPLETED
    await supervisor.close()
    await database.dispose()
