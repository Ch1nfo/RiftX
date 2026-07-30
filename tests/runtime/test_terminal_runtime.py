from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

from riftx.application.services import TerminalApplicationService
from riftx.domain import Engagement, Objective, Run, TerminalStatus
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
    SQLAlchemyTerminalRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.runner import RunnerPaths, TerminalSupervisor
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import AgentEngineEvent, AgentEngineEventType, AgentEngineState
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import MinimalContextCompiler, RunCycleRequest
from riftx.runtime.types import AgentSession, ToolCallStatus, YieldReason


class TerminalEngineRun:
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
            serialized_state={"terminal": "open"},
        )

    async def cancel(self) -> None:
        return None


class TerminalEngine:
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        self._events = events
        self.start_count = 0

    async def start(self, request: object) -> TerminalEngineRun:
        self.start_count += 1
        return TerminalEngineRun(self._events)

    async def resume(self, request: object) -> TerminalEngineRun:
        return TerminalEngineRun(self._events)


async def test_runtime_opens_one_durable_pty_and_yields_terminal_open(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'terminal-runtime.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized terminal")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Open an interactive terminal"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(
        AgentSession(id="session-1", run_id="run-1", model_profile="fake-model")
    )
    events = SQLAlchemyRunEventRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    terminals = SQLAlchemyTerminalRepository(database.session_factory)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=events,
        paths=RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.1,
    )
    terminal_service = TerminalApplicationService(
        run_repository=runs,
        supervisor=supervisor,
    )
    intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=intents,
        execution_service=ExecutionService(
            execution_repository=executions,
            session_repository=sessions,
            tool_call_repository=intents,
            runner=supervisor,  # PTY intents are routed before ExecutionService uses this.
        ),
    )
    engine = TerminalEngine(
        [
            AgentEngineEvent(
                sequence=1,
                event_type=AgentEngineEventType.TOOL_CALL_READY,
                data={
                    "call_id": "interactive-call-1",
                    "tool_id": "interactive-python",
                    "approval_level": "never",
                    "approval_required": False,
                    "arguments": {},
                    "execution": {
                        "node_id": "local",
                        "executor_type": "pty",
                        "cwd": str(tmp_path),
                        "argv": [
                            sys.executable,
                            "-u",
                            "-c",
                            "print('PTY READY', flush=True); input()",
                        ],
                    },
                },
            ),
            AgentEngineEvent(sequence=2, event_type=AgentEngineEventType.RUN_COMPLETED),
        ]
    )
    coordinator = RuntimeCoordinator(
        run_repository=runs,
        session_repository=sessions,
        cycle_repository=SQLAlchemyAgentCycleRepository(database.session_factory),
        step_repository=SQLAlchemyAgentStepRepository(database.session_factory),
        provider_state_repository=SQLAlchemyProviderStateRepository(database.session_factory),
        event_repository=events,
        lease_manager=DatabaseRunLeaseManager(
            SQLAlchemyRunLeaseRepository(database.session_factory)
        ),
        context_compiler=MinimalContextCompiler(),
        agent_engine=engine,
        deferred_execution_dispatcher=dispatcher,
        terminal_service=terminal_service,
    )
    cycle_request = RunCycleRequest(
        run_id="run-1",
        session_id="session-1",
        worker_id="worker-1",
        cycle_id="terminal-cycle-1",
    )

    first = await coordinator.run_cycle(cycle_request)
    retried = await coordinator.run_cycle(cycle_request)

    assert first.yield_reason is YieldReason.TERMINAL_OPEN
    assert retried.waiting_execution_id == first.waiting_execution_id
    assert engine.start_count == 1
    persisted_executions = list(await executions.list("run-1"))
    assert len(persisted_executions) == 1
    terminal = await terminals.get_by_execution(first.waiting_execution_id or "")
    assert terminal is not None and terminal.status is TerminalStatus.OPEN
    intent = await intents.get(persisted_executions[0].tool_call_id or "")
    assert intent is not None and intent.status is ToolCallStatus.EXECUTING

    await terminal_service.write(terminal.id, b"done\n", actor=terminal.owner)
    closed = await terminal_service.close(terminal.id)
    assert closed.terminal.status is TerminalStatus.CLOSED
    await supervisor.close_all()
    await database.dispose()
