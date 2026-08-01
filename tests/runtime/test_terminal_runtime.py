from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import (
    CreateTerminal,
    RunApplicationService,
    TerminalApplicationService,
)
from riftx.domain import (
    Engagement,
    Execution,
    ExecutorType,
    Objective,
    Run,
    RunStatus,
    TerminalOwner,
    TerminalSession,
    TerminalStatus,
)
from riftx.execution import DeferredExecutionDispatcher, ExecutionService
from riftx.hooks import (
    HookBus,
    HookDecision,
    HookPoint,
    HookRegistration,
    HookResult,
    PythonHook,
)
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
from riftx.runner import ProcessSupervisor, RunnerPaths, TerminalSupervisor
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import AgentEngineEvent, AgentEngineEventType, AgentEngineState
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import MinimalContextCompiler, RunCycleRequest
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ToolCallIntent,
    ToolCallStatus,
    YieldReason,
)


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


class DelayedTerminalController:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.physical_starts = 0

    async def start(self, request, *, effect_guard=None):
        self.entered.set()
        await self.release.wait()
        if effect_guard is not None:
            await effect_guard()
        self.physical_starts += 1
        raise AssertionError("test controller must be fenced before physical start")


class RecordingTerminalController:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.starts = 0
        self.writes = 0

    async def start(self, request: object, *, effect_guard: object = None) -> object:
        self.starts += 1
        raise AssertionError("COMPLETING must block terminal start")

    async def get(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=session_id, run_id=self.run_id)

    async def write(self, session_id: str, data: bytes, *, actor: TerminalOwner) -> None:
        self.writes += 1


class CapturingTerminalController:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.request = None
        self.execution: Execution | None = None

    async def start(self, request, *, effect_guard=None):
        if effect_guard is not None:
            await effect_guard()
        self.request = request
        self.execution = Execution(
            id=str(request.execution_id),
            execution_key=str(request.execution_key),
            launch_fingerprint=request.launch_fingerprint,
            run_id=request.run_id,
            session_id=request.agent_session_id,
            tool_call_id=request.tool_call_id,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
            executor_type=ExecutorType.PTY,
            argv=request.argv,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            stdout_path=str(self.tmp_path / "captured-terminal.log"),
            stderr_path=str(self.tmp_path / "captured-terminal.log"),
        )
        return TerminalSession(
            id=str(request.session_id),
            run_id=request.run_id,
            execution_id=self.execution.id,
            runner_id=request.node_id,
            shell=request.argv[0],
            cwd=str(request.cwd),
        )

    async def get_execution(self, session_id: str) -> Execution:
        assert self.execution is not None
        return self.execution


class RunControlWorkflow:
    async def pause(self, run_id: str) -> None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None


async def test_materialized_terminal_launch_is_built_once_and_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'materialized-terminal.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Materialized terminal")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Reuse exact terminal launch"),
            workspace_path=str(tmp_path),
        )
    )
    controller = CapturingTerminalController(tmp_path)
    service = TerminalApplicationService(
        run_repository=runs,
        supervisor=controller,  # type: ignore[arg-type]
    )
    command = CreateTerminal(
        session_id="materialized-session",
        execution_id="materialized-execution",
        execution_key="materialized-key",
        agent_session_id="agent-session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
        argv=["fake-shell"],
        cwd=str(tmp_path),
    )
    build = AsyncMock(wraps=service._build_launch_request)
    monkeypatch.setattr(service, "_build_launch_request", build)

    launch_request = await service.materialize_launch_request("run-1", command)
    view = await service.create(
        "run-1",
        command,
        launch_request=launch_request,
    )

    assert build.await_count == 1
    assert controller.request is launch_request
    assert view.execution.launch_fingerprint == launch_request.launch_fingerprint
    assert view.execution.id == launch_request.execution_id
    await database.dispose()


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        ("pause", RunStatus.PAUSED),
        ("cancel", RunStatus.CANCELLED),
    ],
)
async def test_run_stop_wins_pre_registration_race_and_delayed_terminal_never_starts(
    tmp_path: Path,
    operation: str,
    expected_status: RunStatus,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'terminal-admission.db'}")
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
            objective=Objective(description="Fence delayed terminal"),
            workspace_path=str(tmp_path),
        )
    )
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    process_supervisor = ProcessSupervisor(executions, RunnerPaths(tmp_path / "process-state"))
    terminal_controller = DelayedTerminalController()
    terminal_service = TerminalApplicationService(
        run_repository=runs,
        supervisor=terminal_controller,  # type: ignore[arg-type]
    )
    run_control = RunApplicationService(
        engagement_repository=object(),  # type: ignore[arg-type]
        run_repository=runs,
        event_repository=events,
        workflow_client=RunControlWorkflow(),  # type: ignore[arg-type]
        execution_repository=executions,
        execution_runner=process_supervisor,
        workspace_root=tmp_path,
        execution_cancel_timeout_seconds=0.01,
        execution_cancel_poll_seconds=0.001,
    )

    create_task = asyncio.create_task(
        terminal_service.create(
            "run-1",
            CreateTerminal(argv=[sys.executable], cwd=str(tmp_path)),
        )
    )
    await terminal_controller.entered.wait()
    stopped = await getattr(run_control, operation)("run-1")
    assert stopped.status is expected_status

    terminal_controller.release.set()
    with pytest.raises(ApplicationConflictError) as captured:
        await create_task

    assert captured.value.code == "run_execution_blocked"
    assert terminal_controller.physical_starts == 0
    assert list(await executions.list("run-1")) == []
    await process_supervisor.close()
    await database.dispose()


async def test_completing_fence_blocks_new_terminal_and_existing_terminal_input(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'terminal-completing.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Terminal completion fence")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Block terminal effects while finalizing"),
            workspace_path=str(tmp_path),
        )
    )
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.update_status("run-1", RunStatus.COMPLETING)
    controller = RecordingTerminalController("run-1")
    service = TerminalApplicationService(
        run_repository=runs,
        supervisor=controller,  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as create_error:
        await service.create("run-1", CreateTerminal(argv=[sys.executable]))
    with pytest.raises(ApplicationConflictError) as write_error:
        await service.write("terminal-1", b"dangerous command\n", actor=TerminalOwner.USER)

    assert create_error.value.code == "run_execution_blocked"
    assert write_error.value.code == "run_execution_blocked"
    assert controller.starts == 0
    assert controller.writes == 0
    await database.dispose()


async def test_cancel_after_pty_claim_blocks_hook_and_native_admission(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pty-claim-barrier.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="PTY claim barrier")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Fence a cancelled PTY claim"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="fake"))
    cycles = SQLAlchemyAgentCycleRepository(database.session_factory)
    await cycles.create(
        AgentCycle(id="cycle-1", run_id="run-1", session_id="session-1", sequence=1)
    )
    steps = SQLAlchemyAgentStepRepository(database.session_factory)
    await steps.create(
        AgentStep(
            id="step-1",
            cycle_id="cycle-1",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        )
    )
    intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    intent = await intents.create(
        ToolCallIntent(
            id="tool-call-1",
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            step_id="step-1",
            tool_id="interactive-shell",
            status=ToolCallStatus.READY,
        )
    )
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    controller = RecordingTerminalController("run-1")
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=intents,
        execution_service=ExecutionService(
            execution_repository=executions,
            session_repository=sessions,
            tool_call_repository=intents,
            runner=controller,  # type: ignore[arg-type]
            run_repository=runs,
        ),
    )
    execution_key = "terminal:terminal:claim-barrier"
    claim = await dispatcher.claim_intent_execution(
        intent,
        execution_key=execution_key,
        attempt_group="initial",
    )
    cancelled, changed = await intents.compare_and_set_status(
        intent.id,
        expected={ToolCallStatus.EXECUTING},
        target=ToolCallStatus.CANCELLED,
    )
    assert changed is True and cancelled.status is ToolCallStatus.CANCELLED
    seen_hooks: list[HookPoint] = []

    def record_hook(request) -> HookResult:
        seen_hooks.append(request.point)
        return HookResult(decision=HookDecision.CONTINUE)

    hooks = HookBus()
    hooks.register(
        HookRegistration(
            "terminal-claim-barrier",
            HookPoint.TERMINAL_OPEN,
            PythonHook(record_hook),
        )
    )
    terminal_service = TerminalApplicationService(
        run_repository=runs,
        supervisor=controller,  # type: ignore[arg-type]
        hooks=hooks,
    )

    async def exact_claim_guard() -> None:
        await dispatcher.require_current_intent_execution_claim(
            intent.id,
            execution_key=execution_key,
            attempt_group="initial",
        )

    command = CreateTerminal(
        session_id="terminal:claim-barrier",
        execution_id="terminal-exec:claim-barrier",
        execution_key=execution_key,
        agent_session_id="session-1",
        tool_call_id=intent.id,
        attempt_group="initial",
        argv=[sys.executable],
        cwd=str(tmp_path),
    )
    launch_request = await terminal_service.materialize_launch_request(
        "run-1",
        command,
    )
    with pytest.raises(ApplicationConflictError) as captured:
        await terminal_service.create(
            "run-1",
            command,
            effect_guard=exact_claim_guard,
            launch_request=launch_request,
        )
    await dispatcher.settle_failed_intent_execution_start(
        claim,
        launch_request=launch_request,
    )

    assert captured.value.code == "tool_call_execution_claim_lost"
    assert controller.starts == 0
    assert seen_hooks == []
    assert list(await executions.list("run-1")) == []
    durable = await intents.get(intent.id)
    assert durable is not None and durable.status is ToolCallStatus.CANCELLED
    await database.dispose()


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
    await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="fake-model"))
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
    seen_hooks: list[HookPoint] = []

    def terminal_hook(request) -> HookResult:
        seen_hooks.append(request.point)
        return HookResult(decision=HookDecision.CONTINUE)

    hooks = HookBus()
    for point in (HookPoint.TERMINAL_OPEN, HookPoint.TERMINAL_CLOSE):
        hooks.register(HookRegistration(point.value, point, PythonHook(terminal_hook)))
    terminal_service = TerminalApplicationService(
        run_repository=runs,
        supervisor=supervisor,
        hooks=hooks,
    )
    intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=intents,
        execution_service=ExecutionService(
            execution_repository=executions,
            session_repository=sessions,
            tool_call_repository=intents,
            runner=supervisor,  # PTY intents are routed before ExecutionService uses this.
            run_repository=runs,
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
                            (
                                "import time; print('PTY READY', flush=True); "
                                "input(); time.sleep(30)"
                            ),
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
    assert seen_hooks == [HookPoint.TERMINAL_OPEN, HookPoint.TERMINAL_CLOSE]
    await supervisor.close_all()
    await database.dispose()
