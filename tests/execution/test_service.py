from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    ServiceUnavailableError,
)
from riftx.application.services.runs import RunApplicationService
from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    RunStatus,
)
from riftx.execution import ExecutionService, SubmitExecutionRequest, build_execution_key
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.runner import (
    ExecutionLaunchRequest,
    ExecutionOutput,
    OutputSlice,
    ProcessSupervisor,
    RunnerPaths,
)
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ToolCallIntent,
    ToolCallStatus,
)


class RecordingRunner:
    def __init__(self, repository: SQLAlchemyExecutionRepository) -> None:
        self.repository = repository
        self.launches = 0

    async def start(self, request: ExecutionLaunchRequest, *, effect_guard=None) -> Execution:
        if effect_guard is not None:
            await effect_guard()
        execution = Execution(
            execution_key=request.execution_key,
            run_id=request.run_id,
            session_id=request.session_id,
            tool_call_id=request.tool_call_id,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
            executor_type=request.executor_type,
            argv=request.argv,
            command_text=request.command_text,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            status=ExecutionStatus.QUEUED,
            stdout_path=str(request.cwd / "stdout.log"),
            stderr_path=str(request.cwd / "stderr.log"),
        )
        execution, created = await self.repository.create_if_absent(execution)
        if not created:
            return execution
        self.launches += 1
        execution.transition_to(ExecutionStatus.STARTING)
        execution.transition_to(ExecutionStatus.RUNNING)
        return await self.repository.save(execution)

    async def get(self, execution_id: str) -> Execution:
        execution = await self.repository.get(execution_id)
        assert execution is not None
        return execution

    async def wait(self, execution_id: str) -> Execution:
        execution = await self.get(execution_id)
        if execution.status is ExecutionStatus.RUNNING:
            execution.transition_to(ExecutionStatus.COMPLETED, exit_code=0)
            await self.repository.save(execution)
        return execution

    async def cancel(self, execution_id: str) -> Execution:
        execution = await self.get(execution_id)
        if execution.status is ExecutionStatus.RUNNING:
            execution.transition_to(ExecutionStatus.CANCELLED)
            await self.repository.save(execution)
        return execution

    async def read_output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ExecutionOutput:
        return ExecutionOutput(
            stdout=OutputSlice(data=b"", cursor=stdout_cursor, next_cursor=stdout_cursor, eof=True),
            stderr=OutputSlice(data=b"", cursor=stderr_cursor, next_cursor=stderr_cursor, eof=True),
        )


class DelayedRegistrationRunner:
    """Hold the original pre-registration race window open on demand."""

    def __init__(self, delegate: RecordingRunner) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    async def start(self, request: ExecutionLaunchRequest, *, effect_guard=None) -> Execution:
        self.entered.set()
        await self.release.wait()
        return await self.delegate.start(request, effect_guard=effect_guard)


class RunControlWorkflow:
    async def pause(self, run_id: str) -> None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None


class BlockSecondRunRead:
    """Block the supervisor's post-registration effect guard."""

    def __init__(self, delegate: SQLAlchemyRunRepository) -> None:
        self._delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._reads = 0

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def get(self, run_id: str) -> Run | None:
        self._reads += 1
        if self._reads == 2:
            self.entered.set()
            await self.release.wait()
        return await self._delegate.get(run_id)


async def build_service(
    tmp_path: Path,
) -> tuple[Database, ExecutionService, RecordingRunner, dict[str, object]]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Run a durable tool"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="fake-model"))
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
    tool_calls = SQLAlchemyToolCallIntentRepository(database.session_factory)
    await tool_calls.create(
        ToolCallIntent(
            id="tool-call-1",
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            step_id="step-1",
            tool_id="shell",
            status=ToolCallStatus.READY,
        )
    )
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    runner = RecordingRunner(executions)
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=runner,
        run_repository=runs,
    )
    return (
        database,
        service,
        runner,
        {
            "executions": executions,
            "runs": runs,
            "sessions": sessions,
            "tool_calls": tool_calls,
        },
    )


def request(tmp_path: Path, *, attempt_group: str = "initial") -> SubmitExecutionRequest:
    return SubmitExecutionRequest(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group=attempt_group,
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        cwd=tmp_path,
        argv=[sys.executable, "-c", "print('ok')"],
        tool_id="shell",
    )


async def test_submit_requires_persisted_tool_call_before_runner_launch(tmp_path: Path) -> None:
    database, service, runner, _ = await build_service(tmp_path)
    missing = request(tmp_path).model_copy(update={"tool_call_id": "missing"})

    with pytest.raises(EntityNotFoundError, match="ToolCallIntent"):
        await service.submit(missing)

    assert runner.launches == 0
    await database.dispose()


@pytest.mark.parametrize("fence_status", [RunStatus.PAUSING, RunStatus.COMPLETING])
async def test_submit_is_blocked_after_run_enters_safety_fence(
    tmp_path: Path,
    fence_status: RunStatus,
) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    runs = repos["runs"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.update_status("run-1", fence_status)

    with pytest.raises(ApplicationConflictError) as captured:
        await service.submit(request(tmp_path))

    assert captured.value.code == "run_execution_blocked"
    assert runner.launches == 0
    await database.dispose()


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        ("pause", RunStatus.PAUSED),
        ("cancel", RunStatus.CANCELLED),
    ],
)
async def test_run_stop_wins_pre_registration_race_and_delayed_execution_never_launches(
    tmp_path: Path,
    operation: str,
    expected_status: RunStatus,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    runs = repos["runs"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    executions = repos["executions"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)

    delegate = RecordingRunner(executions)
    delayed = DelayedRegistrationRunner(delegate)
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=delayed,  # type: ignore[arg-type]
        run_repository=runs,
    )
    events = SQLAlchemyRunEventRepository(database.session_factory)
    run_control = RunApplicationService(
        engagement_repository=object(),  # type: ignore[arg-type]
        run_repository=runs,
        event_repository=events,
        workflow_client=RunControlWorkflow(),  # type: ignore[arg-type]
        execution_repository=executions,
        execution_runner=delayed,  # type: ignore[arg-type]
        workspace_root=tmp_path,
        execution_cancel_timeout_seconds=0.01,
        execution_cancel_poll_seconds=0.001,
    )

    submit_task = asyncio.create_task(service.submit(request(tmp_path)))
    await delayed.entered.wait()
    stopped = await getattr(run_control, operation)("run-1")
    assert stopped.status is expected_status

    delayed.release.set()
    with pytest.raises(ApplicationConflictError) as captured:
        await submit_task

    assert captured.value.code == "run_execution_blocked"
    assert delegate.launches == 0
    assert list(await executions.list("run-1")) == []
    await database.dispose()


async def test_registered_starting_execution_keeps_pause_unconfirmed_until_guard_aborts(
    tmp_path: Path,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    runs = repos["runs"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    executions = repos["executions"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)

    guarded_runs = BlockSecondRunRead(runs)
    supervisor = ProcessSupervisor(executions, RunnerPaths(tmp_path / "admission-state"))
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=supervisor,
        run_repository=guarded_runs,  # type: ignore[arg-type]
    )
    run_control = RunApplicationService(
        engagement_repository=object(),  # type: ignore[arg-type]
        run_repository=runs,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        workflow_client=RunControlWorkflow(),  # type: ignore[arg-type]
        execution_repository=executions,
        execution_runner=supervisor,
        workspace_root=tmp_path,
        execution_cancel_timeout_seconds=0.01,
        execution_cancel_poll_seconds=0.001,
        execution_cancel_max_passes=1,
    )

    submit_task = asyncio.create_task(service.submit(request(tmp_path)))
    await guarded_runs.entered.wait()
    registered = list(await executions.list("run-1"))
    assert len(registered) == 1
    assert registered[0].status is ExecutionStatus.STARTING
    assert registered[0].pid is None

    with pytest.raises(ServiceUnavailableError) as captured:
        await run_control.pause("run-1")

    assert captured.value.code == "execution_cancel_failed"
    fenced = await runs.get("run-1")
    assert fenced is not None and fenced.status is RunStatus.PAUSING
    assert (await executions.get(registered[0].id)).status is ExecutionStatus.STARTING  # type: ignore[union-attr]

    guarded_runs.release.set()
    with pytest.raises(ApplicationConflictError) as blocked:
        await submit_task
    assert blocked.value.code == "run_execution_blocked"
    aborted = await executions.get(registered[0].id)
    assert aborted is not None and aborted.status is ExecutionStatus.CANCELLED

    paused = await run_control.pause("run-1")
    assert paused.status is RunStatus.PAUSED
    await supervisor.close()
    await database.dispose()


async def test_same_key_and_running_resubmission_launch_only_once(tmp_path: Path) -> None:
    database, service, runner, _ = await build_service(tmp_path)
    first = await service.submit(request(tmp_path))
    second = await service.submit(request(tmp_path))

    assert first.id == second.id
    assert second.status is ExecutionStatus.RUNNING
    assert runner.launches == 1
    assert first.execution_key == build_execution_key(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
    )
    await database.dispose()


async def test_completed_resubmission_returns_durable_result(tmp_path: Path) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    execution = await service.submit(request(tmp_path))
    wait_result = await service.wait(execution.id)
    completed = wait_result.execution
    duplicate = await service.submit(request(tmp_path))

    assert completed.status is ExecutionStatus.COMPLETED
    assert duplicate.id == execution.id
    assert duplicate.status is ExecutionStatus.COMPLETED
    assert runner.launches == 1
    intent = await repos["tool_calls"].get("tool-call-1")
    assert intent.status is ToolCallStatus.COMPLETED
    await database.dispose()


async def test_failed_execution_requires_new_attempt_group_for_retry(tmp_path: Path) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    first = await service.submit(request(tmp_path))
    first.transition_to(ExecutionStatus.FAILED)
    await repos["executions"].save(first)

    same_attempt = await service.submit(request(tmp_path))
    retry = await service.submit(request(tmp_path, attempt_group="retry-1"))

    assert same_attempt.id == first.id
    assert same_attempt.status is ExecutionStatus.FAILED
    assert retry.id != first.id
    assert retry.attempt_group == "retry-1"
    assert retry.status is ExecutionStatus.RUNNING
    assert runner.launches == 2
    await database.dispose()


async def test_cancelled_execution_does_not_restart_by_default(tmp_path: Path) -> None:
    database, service, runner, _ = await build_service(tmp_path)
    first = await service.submit(request(tmp_path))
    cancelled = await service.cancel(first.id, reason="user requested")
    duplicate = await service.submit(request(tmp_path))

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert duplicate.id == first.id
    assert duplicate.status is ExecutionStatus.CANCELLED
    assert runner.launches == 1
    await database.dispose()


async def test_execution_is_queryable_after_repository_reload(tmp_path: Path) -> None:
    database_path = tmp_path / "execution.db"
    database, service, _, _ = await build_service(tmp_path)
    submitted = await service.submit(request(tmp_path))
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    restored = await SQLAlchemyExecutionRepository(reopened.session_factory).get(submitted.id)
    assert restored is not None
    assert restored.session_id == "session-1"
    assert restored.tool_call_id == "tool-call-1"
    assert restored.attempt_group == "initial"
    assert restored.execution_key == submitted.execution_key
    await reopened.dispose()


def test_execution_status_exposes_post_v2_lifecycle() -> None:
    assert {
        ExecutionStatus.QUEUED,
        ExecutionStatus.STARTING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.HARD_TIMEOUT,
        ExecutionStatus.LOST,
    } <= set(ExecutionStatus)


async def test_wait_distinguishes_lost_execution(tmp_path: Path) -> None:
    database, service, _, repos = await build_service(tmp_path)
    execution = await service.submit(request(tmp_path))
    execution.transition_to(ExecutionStatus.LOST)
    await repos["executions"].save(execution)

    result = await service.wait(execution.id, timeout_seconds=0.1)

    assert result.wait_status.value == "execution_lost"
    assert result.execution.status is ExecutionStatus.LOST
    await database.dispose()
