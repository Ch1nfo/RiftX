from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.context import ContextApplicationService, ManifestingContextCompiler
from riftx.domain import Engagement, Objective, Run, RunStatus
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunLeaseRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import (
    AgentEngineEvent,
    AgentEngineEventType,
    AgentEngineState,
)
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import CycleLimits, MinimalContextCompiler, RunCycleRequest
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

    async def start(self, request: object) -> FakeEngineRun:
        self.requests.append(request)
        if self.start_error:
            raise self.start_error
        return self.run

    async def resume(self, request: object) -> FakeEngineRun:
        return self.run


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
) -> tuple[Database, RuntimeCoordinator, FakeEngine, dict[str, object]]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Map the authorized target"),
            workspace_path="/tmp/riftx/run-1",
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="fake-model"))
    engine = FakeEngine(events, start_error=start_error)
    repos: dict[str, object] = {
        "sessions": sessions,
        "cycles": SQLAlchemyAgentCycleRepository(database.session_factory),
        "steps": SQLAlchemyAgentStepRepository(database.session_factory),
        "providers": SQLAlchemyProviderStateRepository(database.session_factory),
        "events": SQLAlchemyRunEventRepository(database.session_factory),
        "leases": SQLAlchemyRunLeaseRepository(database.session_factory),
    }
    context_compiler = MinimalContextCompiler()
    if observable_context:
        context_repository = SQLAlchemyContextCompilationRepository(database.session_factory)
        repos["context"] = context_repository
        context_compiler = ManifestingContextCompiler(
            context_compiler,
            ContextApplicationService(context_repository),
        )
    coordinator = RuntimeCoordinator(
        run_repository=SQLAlchemyRunRepository(database.session_factory),
        session_repository=sessions,
        cycle_repository=repos["cycles"],
        step_repository=repos["steps"],
        provider_state_repository=repos["providers"],
        event_repository=repos["events"],
        lease_manager=DatabaseRunLeaseManager(repos["leases"]),
        context_compiler=context_compiler,
        agent_engine=engine,
        limits=limits,
        **({"clock": clock} if clock is not None else {}),
    )
    return database, coordinator, engine, repos


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
    run = await SQLAlchemyRunRepository(database.session_factory).get("run-1")
    assert run is not None and run.status is RunStatus.COMPLETED
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
