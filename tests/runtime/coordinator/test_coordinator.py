from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
)
from riftx.application.services import (
    ClosureVerifierApplicationService,
    ResourceStopDisposition,
    SafetyStopResult,
)
from riftx.context import ContextApplicationService, ManifestingContextCompiler
from riftx.domain import (
    DomainError,
    Engagement,
    EntryPoint,
    EntryPointKind,
    Objective,
    PentestAdmission,
    PentestBudget,
    Run,
    RunKind,
    RunStatus,
    Scope,
)
from riftx.hooks import (
    HookBus,
    HookDecision,
    HookPoint,
    HookRegistration,
    HookRequest,
    HookResult,
    PythonHook,
)
from riftx.observer import (
    SupervisorCheck,
    SupervisorDisposition,
    SupervisorReport,
    SupervisorSeverity,
    SupervisorSignal,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyEvidenceLedgerRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyReasoningGraphRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunLeaseRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTaskGraphRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import (
    AgentEngineEvent,
    AgentEngineEventType,
    AgentEngineState,
)
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import (
    CompiledContext,
    ContextCompiler,
    CycleLimits,
    MinimalContextCompiler,
    RunCycleRequest,
)
from riftx.runtime.types import AgentSession, CycleStatus, SessionStatus, YieldReason


class FakeEngineRun:
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        self._events = events
        self.suspended = False
        self.cancelled = False

    async def events(self) -> AsyncIterator[AgentEngineEvent]:
        for event in self._events:
            yield event

    async def suspend(self) -> AgentEngineState:
        self.suspended = True
        return AgentEngineState(
            engine_type="fake",
            engine_version="1",
            provider="fake",
            model="fake-model",
            serialized_state={"cursor": len(self._events)},
            sdk_run_state={"cursor": len(self._events)},
        )

    async def cancel(self) -> None:
        self.cancelled = True


class FakeEngine:
    def __init__(
        self,
        events: list[AgentEngineEvent],
        *,
        start_error: Exception | None = None,
    ) -> None:
        self.run = FakeEngineRun(events)
        self.start_error = start_error
        self.requests: list[object] = []
        self.resume_requests: list[object] = []

    async def start(self, request: object) -> FakeEngineRun:
        self.requests.append(request)
        if self.start_error:
            raise self.start_error
        return self.run

    async def resume(self, request: object) -> FakeEngineRun:
        self.resume_requests.append(request)
        return self.run


class FakeSubagentBatchExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, object]]]] = []

    async def execute(
        self,
        parent_session_id: str,
        requests: list[dict[str, object]],
    ) -> None:
        self.calls.append((parent_session_id, requests))


class CapabilityContextCompiler:
    async def compile(self, request: object) -> CompiledContext:
        return CompiledContext(
            system_instructions="Observe compiled capabilities",
            available_tools=[{"name": "tool-b"}, {"id": "tool-a"}],
            available_skills=[{"id": "skill-b"}, {"name": "skill-a"}],
        )


class RecordingObserver:
    def __init__(self, report: SupervisorReport) -> None:
        self.report = report
        self.calls: list[dict[str, object]] = []

    async def inspect(self, **kwargs: object) -> SupervisorReport:
        self.calls.append(kwargs)
        return self.report


class RecordingSafetyStopper:
    def __init__(
        self,
        runs: SQLAlchemyRunRepository,
        events: SQLAlchemyRunEventRepository,
        *,
        confirmed: bool = True,
    ) -> None:
        self._runs = runs
        self._events = events
        self.confirmed = confirmed
        self.observed_statuses: list[RunStatus] = []
        self.observed_closure_event_counts: list[int] = []
        self.calls: list[str] = []

    async def stop_run(self, run_id: str, *, drain: bool = True) -> SafetyStopResult:
        assert drain is True
        self.calls.append(run_id)
        run = await self._runs.get(run_id)
        assert run is not None
        self.observed_statuses.append(run.status)
        events = await self._events.list_after(run_id)
        self.observed_closure_event_counts.append(
            sum(event.event_type == "run.closure_evaluated" for event in events)
        )
        failures = {} if self.confirmed else {"browser-1": "owner ACK pending"}
        return SafetyStopResult(
            resources={
                "executions": ResourceStopDisposition((), {}, {}, {}, {}),
                "browser_sessions": ResourceStopDisposition(
                    ("browser-1",),
                    {"browser-1": "node-1"},
                    {"browser-1": "active"},
                    {},
                    failures,
                ),
                "target_http_requests": ResourceStopDisposition((), {}, {}, {}, {}),
            }
        )


class RecordingBudgetExhaustionHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, run_id: str) -> None:
        self.calls.append(run_id)


def event(sequence: int, event_type: AgentEngineEventType, **data: object) -> AgentEngineEvent:
    return AgentEngineEvent(sequence=sequence, event_type=event_type, data=data)


async def build_runtime(
    tmp_path: Path,
    events: list[AgentEngineEvent],
    *,
    limits: CycleLimits | None = None,
    clock: object | None = None,
    start_error: Exception | None = None,
    observable_context: bool = False,
    workspace_path: Path | None = None,
    hooks: HookBus | None = None,
    observer: object | None = None,
    context_compiler: ContextCompiler | None = None,
    subagent_executor: object | None = None,
    budget_exhaustion_handler: RecordingBudgetExhaustionHandler | None = None,
    with_safety_stopper: bool = True,
    run_kind: RunKind = RunKind.GENERAL,
    pentest_budget: PentestBudget | None = None,
) -> tuple[Database, RuntimeCoordinator, FakeEngine, dict[str, object]]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(
            id="engagement-1",
            name="Authorized",
            authorization_reference=(
                "ticket://runtime-test" if run_kind is RunKind.PENTEST else None
            ),
        )
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    admission = (
        PentestAdmission(
            budget=pentest_budget
            or PentestBudget(
                max_duration_seconds=600,
                max_model_calls=10,
                max_tokens=10_000,
                max_tool_calls=10,
                max_target_interactions=10,
                max_concurrent_target_interactions=1,
            )
        )
        if run_kind is RunKind.PENTEST
        else None
    )
    await runs.create(
        Run(
            kind=run_kind,
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Map the authorized target"),
            entry_points=(
                [EntryPoint(kind=EntryPointKind.IP, value="127.0.0.1")]
                if run_kind is RunKind.PENTEST
                else []
            ),
            scope=(Scope(ips=["127.0.0.1"]) if run_kind is RunKind.PENTEST else Scope()),
            pentest_admission=admission,
            workspace_path=str(workspace_path or tmp_path / "workspace"),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="fake-model"))
    engine = FakeEngine(events, start_error=start_error)
    task_graphs = SQLAlchemyTaskGraphRepository(database.session_factory)
    reasoning_graphs = SQLAlchemyReasoningGraphRepository(database.session_factory)
    evidence = SQLAlchemyEvidenceLedgerRepository(database.session_factory)
    repos: dict[str, object] = {
        "runs": runs,
        "sessions": sessions,
        "cycles": SQLAlchemyAgentCycleRepository(database.session_factory),
        "steps": SQLAlchemyAgentStepRepository(database.session_factory),
        "providers": SQLAlchemyProviderStateRepository(database.session_factory),
        "events": SQLAlchemyRunEventRepository(database.session_factory),
        "leases": SQLAlchemyRunLeaseRepository(database.session_factory),
        "task_graphs": task_graphs,
        "reasoning_graphs": reasoning_graphs,
        "evidence": evidence,
    }
    event_repository = repos["events"]
    assert isinstance(event_repository, SQLAlchemyRunEventRepository)
    safety_stopper = RecordingSafetyStopper(runs, event_repository)
    repos["safety_stopper"] = safety_stopper
    resolved_context_compiler = context_compiler or MinimalContextCompiler()
    if observable_context or run_kind is RunKind.PENTEST:
        context_repository = SQLAlchemyContextCompilationRepository(database.session_factory)
        repos["context"] = context_repository
        resolved_context_compiler = ManifestingContextCompiler(
            resolved_context_compiler,
            ContextApplicationService(context_repository),
        )
    coordinator = RuntimeCoordinator(
        run_repository=runs,
        session_repository=sessions,
        cycle_repository=repos["cycles"],
        step_repository=repos["steps"],
        provider_state_repository=repos["providers"],
        event_repository=repos["events"],
        lease_manager=DatabaseRunLeaseManager(repos["leases"]),
        context_compiler=resolved_context_compiler,
        agent_engine=engine,
        hooks=hooks,
        observer=observer,  # type: ignore[arg-type]
        closure_verifier=ClosureVerifierApplicationService(
            runs=runs,
            task_graphs=task_graphs,
            reasoning_graphs=reasoning_graphs,
            evidence=evidence,
        ),
        **({"safety_stopper": safety_stopper} if with_safety_stopper else {}),
        **({"subagent_executor": subagent_executor} if subagent_executor is not None else {}),
        **(
            {"budget_exhaustion_handler": budget_exhaustion_handler}
            if budget_exhaustion_handler is not None
            else {}
        ),
        limits=limits,
        **({"clock": clock} if clock is not None else {}),
    )
    return database, coordinator, engine, repos


async def test_observer_blocks_before_model_and_records_redacted_audit_event(
    tmp_path: Path,
) -> None:
    signal = SupervisorSignal(
        code="scope_boundary_rejected",
        check=SupervisorCheck.SCOPE,
        severity=SupervisorSeverity.BLOCKING,
        summary="Sensitive scope detail must not enter the audit event",
        refs=("event:scope-event",),
    )
    observer = RecordingObserver(
        SupervisorReport(
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-observer",
            disposition=SupervisorDisposition.BLOCK,
            signals=(signal,),
        )
    )
    database, coordinator, engine, repos = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
        observer=observer,
        context_compiler=CapabilityContextCompiler(),
    )

    result = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-1",
            cycle_id="cycle-observer",
        )
    )

    assert result.yield_reason is YieldReason.FATAL_FAILURE
    assert engine.requests == []
    assert observer.calls[0]["available_tool_ids"] == ("tool-a", "tool-b")
    assert observer.calls[0]["available_skill_ids"] == ("skill-a", "skill-b")
    events = await repos["events"].list_after("run-1")
    audit = next(item for item in events if item.event_type == "runtime.observer_inspected")
    assert audit.payload == {
        "cycle_id": "cycle-observer",
        "phase": "pre_model",
        "disposition": "block",
        "yield_reason": None,
        "signals": [
            {
                "code": "scope_boundary_rejected",
                "check": "scope",
                "severity": "blocking",
                "refs": ["event:scope-event"],
            }
        ],
    }
    assert "Sensitive scope detail" not in str(audit.payload)
    await database.dispose()


async def test_code_audit_runtime_cycle_denies_before_lease_event_state_and_model(
    tmp_path: Path,
) -> None:
    database, coordinator, engine, repos = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
        run_kind=RunKind.CODE_AUDIT,
    )
    runs = repos["runs"]
    cycles = repos["cycles"]
    events = repos["events"]
    leases = repos["leases"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    assert isinstance(cycles, SQLAlchemyAgentCycleRepository)
    assert isinstance(events, SQLAlchemyRunEventRepository)
    assert isinstance(leases, SQLAlchemyRunLeaseRepository)
    baseline_events = await events.list_after("run-1", limit=100)

    with pytest.raises(ApplicationConflictError) as captured:
        await coordinator.run_cycle(
            RunCycleRequest(
                run_id="run-1",
                session_id="session-1",
                worker_id="worker-audit",
                cycle_id="cycle-audit",
            )
        )

    assert captured.value.code == "run_kind_operation_unsupported"
    run = await runs.get("run-1")
    assert run is not None and run.status is RunStatus.CREATED
    assert await cycles.list_by_session("session-1") == []
    assert await events.list_after("run-1", limit=100) == baseline_events
    assert await leases.get("run-1") is None
    assert engine.requests == []
    assert engine.resume_requests == []
    await database.dispose()


async def test_code_audit_runtime_preserves_cross_owner_session_error_precedence(
    tmp_path: Path,
) -> None:
    database, coordinator, engine, repos = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
        run_kind=RunKind.CODE_AUDIT,
    )
    runs = repos["runs"]
    sessions = repos["sessions"]
    events = repos["events"]
    leases = repos["leases"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(events, SQLAlchemyRunEventRepository)
    assert isinstance(leases, SQLAlchemyRunLeaseRepository)
    await runs.create(
        Run(
            kind=RunKind.GENERAL,
            id="run-foreign",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Foreign owner"),
            workspace_path=str(tmp_path / "foreign-workspace"),
        )
    )
    await sessions.create(
        AgentSession(
            id="session-foreign",
            run_id="run-foreign",
            model_profile="fake-model",
        )
    )
    baseline_events = await events.list_after("run-1", limit=100)

    with pytest.raises(EntityNotFoundError) as captured:
        await coordinator.run_cycle(
            RunCycleRequest(
                run_id="run-1",
                session_id="session-foreign",
                worker_id="worker-audit",
            )
        )

    assert captured.value.entity == "AgentSession"
    assert await events.list_after("run-1", limit=100) == baseline_events
    assert await leases.get("run-1") is None
    assert engine.requests == []
    await database.dispose()


async def test_three_delegations_execute_as_one_batch_then_primary_continues(
    tmp_path: Path,
) -> None:
    executor = FakeSubagentBatchExecutor()
    delegation_events = [
        event(
            index,
            AgentEngineEventType.SUBAGENT_REQUESTED,
            call_id=f"delegate-{index}",
            arguments={"task": f"Inspect asset {index}"},
        )
        for index in range(1, 4)
    ]
    database, coordinator, engine, _ = await build_runtime(
        tmp_path,
        [*delegation_events, event(4, AgentEngineEventType.RUN_COMPLETED)],
        subagent_executor=executor,
    )

    delegated = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-1",
            cycle_id="delegate-cycle",
        )
    )
    engine.run = FakeEngineRun([event(1, AgentEngineEventType.RUN_COMPLETED)])
    continued = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-1",
            cycle_id="continue-cycle",
        )
    )

    assert delegated.yield_reason is YieldReason.CYCLE_LIMIT_REACHED
    assert executor.calls == [("session-1", [item.data for item in delegation_events])]
    assert continued.yield_reason is YieldReason.RUN_COMPLETED
    await database.dispose()


async def test_runtime_hooks_wrap_context_and_model_lifecycle(tmp_path: Path) -> None:
    seen: list[HookPoint] = []

    def observe(request: HookRequest) -> HookResult:
        seen.append(request.point)
        if request.point is HookPoint.BEFORE_CONTEXT_COMPILE:
            return HookResult(
                decision=HookDecision.MODIFY,
                modified_payload={"input_text": "hooked", "input_items": []},
            )
        if request.point is HookPoint.BEFORE_MODEL_CALL:
            return HookResult(
                decision=HookDecision.CONTINUE,
                additional_context="Hook-provided bounded context",
            )
        return HookResult(decision=HookDecision.CONTINUE)

    hooks = HookBus()
    for point in (
        HookPoint.BEFORE_CONTEXT_COMPILE,
        HookPoint.AFTER_CONTEXT_COMPILE,
        HookPoint.BEFORE_MODEL_CALL,
        HookPoint.AFTER_MODEL_CALL,
    ):
        hooks.register(HookRegistration(point.value, point, PythonHook(observe)))
    database, coordinator, engine, _ = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
        hooks=hooks,
    )

    await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-1",
            input_text="original",
        )
    )

    request_items = engine.requests[0].input_items
    assert any("hooked" in str(item.get("content")) for item in request_items)
    assert any(item.get("content") == "Hook-provided bounded context" for item in request_items)
    assert seen == [
        HookPoint.BEFORE_CONTEXT_COMPILE,
        HookPoint.AFTER_CONTEXT_COMPILE,
        HookPoint.BEFORE_MODEL_CALL,
        HookPoint.AFTER_MODEL_CALL,
    ]
    await database.dispose()


async def test_blocking_model_hook_fails_cycle_before_provider_call(tmp_path: Path) -> None:
    hooks = HookBus()
    hooks.register(
        HookRegistration(
            "block-model",
            HookPoint.BEFORE_MODEL_CALL,
            PythonHook(lambda _: HookResult(decision=HookDecision.BLOCK)),
        )
    )
    database, coordinator, engine, repos = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
        hooks=hooks,
    )

    with pytest.raises(DomainError, match="blocked before_model_call"):
        await coordinator.run_cycle(
            RunCycleRequest(
                run_id="run-1",
                session_id="session-1",
                worker_id="worker-1",
            )
        )

    assert engine.requests == []
    cycles = await repos["cycles"].list_by_session("session-1")
    assert cycles[0].status is CycleStatus.FAILED
    await database.dispose()


async def test_pentest_model_budget_exhaustion_stops_before_provider_call(
    tmp_path: Path,
) -> None:
    budget_handler = RecordingBudgetExhaustionHandler()
    database, coordinator, engine, repos = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
        run_kind=RunKind.PENTEST,
        pentest_budget=PentestBudget(
            max_duration_seconds=600,
            max_model_calls=1,
            max_tokens=10_000,
            max_tool_calls=10,
            max_target_interactions=10,
            max_concurrent_target_interactions=1,
        ),
        budget_exhaustion_handler=budget_handler,
    )
    sessions = repos["sessions"]
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    session = await sessions.get("session-1")
    assert session is not None
    session.model_call_count = 1
    await sessions.save(session)

    with pytest.raises(ApplicationConflictError) as exhausted:
        await coordinator.run_cycle(
            RunCycleRequest(
                run_id="run-1",
                session_id="session-1",
                worker_id="worker-1",
                cycle_id="pentest-budget-cycle",
            )
        )

    assert exhausted.value.code == "pentest_budget_exhausted"
    assert exhausted.value.details == {
        "run_id": "run-1",
        "budget_name": "max_model_calls",
        "limit": 1,
        "used": 1,
        "reason": "exhausted",
    }
    assert engine.requests == []
    assert engine.resume_requests == []
    assert budget_handler.calls == ["run-1"]
    events = await repos["events"].list_after("run-1")
    assert any(item.event_type == "pentest.budget_exhausted" for item in events)
    cycles = await repos["cycles"].list_by_session("session-1")
    assert cycles[0].status is CycleStatus.FAILED
    await database.dispose()


async def test_usage_event_backfills_the_persisted_context_compilation(
    tmp_path: Path,
) -> None:
    database, coordinator, _, repos = await build_runtime(
        tmp_path,
        [
            event(1, AgentEngineEventType.RUN_STARTED),
            event(
                2,
                AgentEngineEventType.USAGE,
                input_tokens=456,
                output_tokens=78,
            ),
            event(3, AgentEngineEventType.RUN_COMPLETED),
        ],
        observable_context=True,
    )

    await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-1",
            input_text="go",
        )
    )

    compilation = await repos["context"].latest_for_session("session-1")
    assert compilation is not None
    assert compilation.actual_input_tokens == 456
    assert compilation.actual_output_tokens == 78
    await database.dispose()


async def test_normal_cycle_completes_and_persists_step(tmp_path: Path) -> None:
    database, coordinator, engine, repos = await build_runtime(
        tmp_path,
        [
            event(1, AgentEngineEventType.RUN_STARTED),
            event(2, AgentEngineEventType.ASSISTANT_MESSAGE, output_refs=["message://1"]),
            event(3, AgentEngineEventType.RUN_COMPLETED),
        ],
    )
    result = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1", session_id="session-1", worker_id="worker-1", input_text="go"
        )
    )
    assert result.yield_reason is YieldReason.RUN_COMPLETED
    assert result.model_call_count == 1
    cycles = await repos["cycles"].list_by_session("session-1")
    assert cycles[0].status is CycleStatus.YIELDED
    steps = await repos["steps"].list_by_cycle(result.cycle_id)
    assert len(steps) == 1
    assert engine.requests
    session = await repos["sessions"].get("session-1")
    assert session.status is SessionStatus.ACTIVE
    assert session.model_call_count == 1
    assert session.tool_call_count == 0
    run = await SQLAlchemyRunRepository(database.session_factory).get("run-1")
    assert run is not None and run.status is RunStatus.COMPLETED
    stopper = repos["safety_stopper"]
    assert isinstance(stopper, RecordingSafetyStopper)
    assert stopper.calls == ["run-1"]
    assert stopper.observed_statuses == [RunStatus.COMPLETING]
    assert stopper.observed_closure_event_counts == [1]
    await database.dispose()


async def test_non_deferred_cycle_without_safety_stopper_stays_fenced(
    tmp_path: Path,
) -> None:
    database, coordinator, _, repos = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
        with_safety_stopper=False,
    )

    result = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="standalone-worker",
            cycle_id="standalone-completion",
        )
    )
    run = await SQLAlchemyRunRepository(database.session_factory).get("run-1")
    runs = repos["runs"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    intent = await runs.get_finalization_intent("run-1")

    assert result.yield_reason is YieldReason.RUN_COMPLETED
    assert run is not None and run.status is RunStatus.COMPLETING
    assert intent is not None and intent.target is RunStatus.COMPLETED
    closure_events = [
        event
        for event in await repos["events"].list_after("run-1")
        if event.event_type == "run.closure_evaluated"
    ]
    assert len(closure_events) == 1
    await database.dispose()


async def test_non_deferred_cycle_retry_resumes_stop_gate_without_rerunning_model(
    tmp_path: Path,
) -> None:
    database, coordinator, engine, repos = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
    )
    stopper = repos["safety_stopper"]
    assert isinstance(stopper, RecordingSafetyStopper)
    stopper.confirmed = False
    request = RunCycleRequest(
        run_id="run-1",
        session_id="session-1",
        worker_id="standalone-worker",
        cycle_id="retry-safe-completion",
    )

    with pytest.raises(DomainError, match="could not confirm every Run effect stopped"):
        await coordinator.run_cycle(request)
    fenced = await SQLAlchemyRunRepository(database.session_factory).get("run-1")
    runs = repos["runs"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    persisted_intent = await runs.get_finalization_intent("run-1")
    stopper.confirmed = True
    result = await coordinator.run_cycle(request)
    completed = await SQLAlchemyRunRepository(database.session_factory).get("run-1")

    assert fenced is not None and fenced.status is RunStatus.COMPLETING
    assert persisted_intent is not None
    assert persisted_intent.target is RunStatus.COMPLETED
    assert result.yield_reason is YieldReason.RUN_COMPLETED
    assert completed is not None and completed.status is RunStatus.COMPLETED
    assert len(engine.requests) == 1
    assert stopper.observed_statuses == [RunStatus.COMPLETING, RunStatus.COMPLETING]
    assert stopper.observed_closure_event_counts == [1, 1]
    closure_events = [
        event
        for event in await repos["events"].list_after("run-1")
        if event.event_type == "run.closure_evaluated"
    ]
    assert len(closure_events) == 1
    await database.dispose()


async def test_temporal_cycle_defers_terminal_status_until_workflow_drains_input(
    tmp_path: Path,
) -> None:
    database, coordinator, engine, _ = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
    )

    first = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="temporal-worker",
            cycle_id="temporal-cycle-1",
            defer_run_completion=True,
        )
    )
    after_first = await SQLAlchemyRunRepository(database.session_factory).get("run-1")
    engine.run = FakeEngineRun([event(1, AgentEngineEventType.RUN_COMPLETED)])
    second = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="temporal-worker",
            cycle_id="temporal-cycle-2",
            defer_run_completion=True,
        )
    )
    after_second = await SQLAlchemyRunRepository(database.session_factory).get("run-1")

    assert first.yield_reason is YieldReason.RUN_COMPLETED
    assert second.yield_reason is YieldReason.RUN_COMPLETED
    assert after_first is not None and after_first.status is RunStatus.RUNNING
    assert after_second is not None and after_second.status is RunStatus.RUNNING
    assert len(engine.requests) == 2
    await database.dispose()


async def test_temporal_cycle_defers_fatal_status_until_fail_closed_cleanup(
    tmp_path: Path,
) -> None:
    database, coordinator, _, _ = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.ERROR, retryable=False, message="fatal")],
    )

    result = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="temporal-worker",
            cycle_id="temporal-fatal-cycle",
            defer_run_completion=True,
        )
    )
    run = await SQLAlchemyRunRepository(database.session_factory).get("run-1")

    assert result.yield_reason is YieldReason.FATAL_FAILURE
    assert run is not None and run.status is RunStatus.RUNNING
    await database.dispose()


async def test_non_deferred_fatal_cycle_uses_stop_gate_before_failed_status(
    tmp_path: Path,
) -> None:
    database, coordinator, _, repos = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.ERROR, retryable=False, message="fatal")],
    )

    result = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="standalone-worker",
            cycle_id="standalone-fatal-cycle",
        )
    )
    run = await SQLAlchemyRunRepository(database.session_factory).get("run-1")
    stopper = repos["safety_stopper"]

    assert result.yield_reason is YieldReason.FATAL_FAILURE
    assert run is not None and run.status is RunStatus.FAILED
    assert isinstance(stopper, RecordingSafetyStopper)
    assert stopper.observed_statuses == [RunStatus.COMPLETING]
    await database.dispose()


async def test_activity_retry_reuses_persisted_cycle_result(tmp_path: Path) -> None:
    database, coordinator, engine, _ = await build_runtime(
        tmp_path,
        [
            event(1, AgentEngineEventType.RUN_STARTED),
            event(2, AgentEngineEventType.RUN_COMPLETED),
        ],
    )
    request = RunCycleRequest(
        run_id="run-1",
        session_id="session-1",
        worker_id="temporal-workflow-1",
        cycle_id="temporal-cycle-1",
    )

    first = await coordinator.run_cycle(request)
    replayed = await coordinator.run_cycle(request)

    assert replayed == first
    assert replayed.cycle_id == "temporal-cycle-1"
    assert len(engine.requests) == 1
    await database.dispose()


async def test_next_cycle_resumes_persisted_provider_state(tmp_path: Path) -> None:
    database, coordinator, engine, _ = await build_runtime(
        tmp_path,
        [
            event(1, AgentEngineEventType.RUN_STARTED),
            event(
                2,
                AgentEngineEventType.ASSISTANT_MESSAGE,
                content="Need operator input",
                requires_user_input=True,
            ),
        ],
    )
    first = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="temporal-worker",
            cycle_id="cycle-1",
        )
    )
    engine.run = FakeEngineRun([event(1, AgentEngineEventType.RUN_COMPLETED)])

    second = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="temporal-worker",
            cycle_id="cycle-2",
        )
    )

    assert first.provider_state_id is not None
    assert second.yield_reason is YieldReason.RUN_COMPLETED
    assert len(engine.requests) == 1
    assert len(engine.resume_requests) == 1
    resumed = engine.resume_requests[0]
    assert resumed.state.engine_type == "fake"
    await database.dispose()


async def test_coordinator_passes_instruction_roots_to_context_compiler(
    tmp_path: Path,
) -> None:
    engagement = tmp_path / "engagement"
    workspace = engagement / "workspace"
    current = workspace / "src"
    for root, marker in (
        (engagement, "ENGAGEMENT-RULE"),
        (workspace, "WORKSPACE-RULE"),
        (current, "CURRENT-RULE"),
    ):
        instruction = root / ".riftx" / "RIFTX.md"
        instruction.parent.mkdir(parents=True, exist_ok=True)
        instruction.write_text(marker, encoding="utf-8")
    database, coordinator, engine, _ = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_COMPLETED)],
        workspace_path=workspace,
    )

    await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-1",
            engagement_path=str(engagement),
            current_path=str(current),
        )
    )

    engine_request = engine.requests[0]
    context = engine_request.context
    assert "ENGAGEMENT-RULE" in context.system_instructions
    assert "WORKSPACE-RULE" in context.system_instructions
    assert "CURRENT-RULE" in context.system_instructions
    assert context.context_manifest["instruction_scopes"][-3:] == [
        "engagement",
        "workspace",
        "current_path",
    ]
    assert context.input_items == []
    await database.dispose()


async def test_model_call_limit_yields_and_saves_provider_state(tmp_path: Path) -> None:
    database, coordinator, engine, repos = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.RUN_STARTED)],
        limits=CycleLimits(max_model_calls=1),
    )
    result = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )
    assert result.yield_reason is YieldReason.CYCLE_LIMIT_REACHED
    assert result.provider_state_id is not None
    assert engine.run.suspended
    assert await repos["providers"].get(result.provider_state_id) is not None
    await database.dispose()


async def test_tool_call_limit_prevents_unbounded_batch(tmp_path: Path) -> None:
    database, coordinator, engine, _ = await build_runtime(
        tmp_path,
        [
            event(1, AgentEngineEventType.TOOL_CALL_READY, tool_id="one"),
            event(2, AgentEngineEventType.TOOL_CALL_READY, tool_id="two"),
        ],
        limits=CycleLimits(max_tool_calls=2),
    )
    result = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )
    assert result.yield_reason is YieldReason.CYCLE_LIMIT_REACHED
    assert result.tool_call_count == 2
    assert engine.run.suspended
    await database.dispose()


async def test_inline_control_tool_call_is_counted_once_across_sdk_start_events(
    tmp_path: Path,
) -> None:
    database, coordinator, _, repos = await build_runtime(
        tmp_path,
        [
            event(
                1,
                AgentEngineEventType.TOOL_CALL_STARTED,
                call_id="call-complete",
                name="complete_run",
            ),
            event(
                2,
                AgentEngineEventType.TOOL_CALL_STARTED,
                call_id="call-complete",
                tool_id="complete_run",
                arguments={"run_summary": "done"},
            ),
            event(3, AgentEngineEventType.RUN_COMPLETED),
        ],
    )

    result = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )

    assert result.yield_reason is YieldReason.RUN_COMPLETED
    assert result.tool_call_count == 1
    run = await repos["runs"].get("run-1")
    assert run is not None and run.status is RunStatus.COMPLETED
    safety_stopper = repos["safety_stopper"]
    assert safety_stopper.observed_statuses == [RunStatus.COMPLETING]
    await database.dispose()


async def test_inline_control_tool_calls_obey_cycle_limit(tmp_path: Path) -> None:
    database, coordinator, engine, _ = await build_runtime(
        tmp_path,
        [
            event(
                1,
                AgentEngineEventType.TOOL_CALL_STARTED,
                call_id="control-1",
                tool_id="search_tools",
            ),
            event(
                2,
                AgentEngineEventType.TOOL_CALL_STARTED,
                call_id="control-2",
                tool_id="list_tools",
            ),
        ],
        limits=CycleLimits(max_tool_calls=2),
    )

    result = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )

    assert result.yield_reason is YieldReason.CYCLE_LIMIT_REACHED
    assert result.tool_call_count == 2
    assert engine.run.suspended
    await database.dispose()


async def test_duration_limit_yields(tmp_path: Path) -> None:
    times = iter([0.0, 901.0])
    database, coordinator, engine, _ = await build_runtime(
        tmp_path,
        [event(1, AgentEngineEventType.ASSISTANT_DELTA, delta="late")],
        clock=lambda: next(times),
    )
    result = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )
    assert result.yield_reason is YieldReason.CYCLE_LIMIT_REACHED
    assert engine.run.suspended
    await database.dispose()


async def test_lease_conflict_rejects_second_primary_cycle(tmp_path: Path) -> None:
    database, coordinator, _, repos = await build_runtime(tmp_path, [])
    held = await DatabaseRunLeaseManager(repos["leases"]).acquire("run-1", "worker-a")
    with pytest.raises(RepositoryConflictError, match="active lease"):
        await coordinator.run_cycle(
            RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-b")
        )
    await held.release()
    await database.dispose()


async def test_cycle_failure_releases_lease_and_marks_cycle_failed(tmp_path: Path) -> None:
    database, coordinator, _, repos = await build_runtime(
        tmp_path, [], start_error=RuntimeError("provider unavailable")
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await coordinator.run_cycle(
            RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
        )
    assert await repos["leases"].get("run-1") is None
    cycles = await repos["cycles"].list_by_session("session-1")
    assert cycles[0].status is CycleStatus.FAILED
    await database.dispose()


async def test_runtime_events_have_stable_order(tmp_path: Path) -> None:
    database, coordinator, _, repos = await build_runtime(
        tmp_path,
        [
            event(1, AgentEngineEventType.RUN_STARTED),
            event(2, AgentEngineEventType.ASSISTANT_MESSAGE),
            event(3, AgentEngineEventType.RUN_COMPLETED),
        ],
    )
    await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )
    persisted = await repos["events"].list_after("run-1", limit=100)
    runtime_events = [item for item in persisted if item.event_type.startswith("runtime.")]
    assert [item.event_type for item in runtime_events] == [
        "runtime.lease_acquired",
        "runtime.session_activated",
        "runtime.cycle_created",
        "runtime.cycle_started",
        "runtime.context_compiled",
        "runtime.engine_event",
        "runtime.engine_event",
        "runtime.step_started",
        "runtime.step_completed",
        "runtime.engine_event",
        "runtime.cycle_yielded",
        "runtime.lease_released",
    ]
    assert [item.sequence for item in persisted] == list(range(1, len(persisted) + 1))
    await database.dispose()


@pytest.mark.parametrize(
    ("events", "expected", "expected_status"),
    [
        (
            [
                event(1, AgentEngineEventType.TOOL_CALL_READY, tool_id="scan"),
                event(2, AgentEngineEventType.RUN_COMPLETED),
            ],
            YieldReason.TOOL_RUNNING,
            RunStatus.WAITING_TOOL,
        ),
        (
            [
                event(
                    1,
                    AgentEngineEventType.TOOL_CALL_READY,
                    tool_id="scan",
                    approval_required=True,
                ),
                event(2, AgentEngineEventType.RUN_COMPLETED),
            ],
            YieldReason.APPROVAL_REQUIRED,
            RunStatus.WAITING_APPROVAL,
        ),
        (
            [
                event(
                    1,
                    AgentEngineEventType.ASSISTANT_MESSAGE,
                    requires_user_input=True,
                )
            ],
            YieldReason.USER_INPUT_REQUIRED,
            RunStatus.WAITING_USER,
        ),
        (
            [event(1, AgentEngineEventType.ERROR, retryable=True, message="temporary")],
            YieldReason.RETRYABLE_FAILURE,
            RunStatus.RUNNING,
        ),
        (
            [event(1, AgentEngineEventType.ERROR, retryable=False, message="fatal")],
            YieldReason.FATAL_FAILURE,
            RunStatus.FAILED,
        ),
    ],
)
async def test_coordinator_supports_required_engine_yields(
    tmp_path: Path,
    events: list[AgentEngineEvent],
    expected: YieldReason,
    expected_status: RunStatus,
) -> None:
    database, coordinator, engine, _ = await build_runtime(tmp_path, events)
    result = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )
    assert result.yield_reason is expected
    assert engine.run.suspended
    run = await SQLAlchemyRunRepository(database.session_factory).get("run-1")
    assert run is not None and run.status is expected_status
    await database.dispose()


async def test_compaction_policy_yields_before_model_call(tmp_path: Path) -> None:
    database, coordinator, engine, _ = await build_runtime(tmp_path, [])
    result = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-1",
            compaction_required=True,
        )
    )
    assert result.yield_reason is YieldReason.COMPACTION_REQUIRED
    assert not engine.requests
    run = await SQLAlchemyRunRepository(database.session_factory).get("run-1")
    assert run is not None and run.status is RunStatus.COMPACTING
    await database.dispose()
