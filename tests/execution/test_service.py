from __future__ import annotations

import sys
from pathlib import Path

import pytest

from riftx.application.errors import EntityNotFoundError
from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
)
from riftx.execution import ExecutionService, SubmitExecutionRequest, build_execution_key
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.runner import ExecutionLaunchRequest, ExecutionOutput, OutputSlice
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

    async def start(self, request: ExecutionLaunchRequest) -> Execution:
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
            stdout=OutputSlice(
                data=b"", cursor=stdout_cursor, next_cursor=stdout_cursor, eof=True
            ),
            stderr=OutputSlice(
                data=b"", cursor=stderr_cursor, next_cursor=stderr_cursor, eof=True
            ),
        )


async def build_service(
    tmp_path: Path,
) -> tuple[Database, ExecutionService, RecordingRunner, dict[str, object]]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Run a durable tool"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(
        AgentSession(id="session-1", run_id="run-1", model_profile="fake-model")
    )
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
    )
    return database, service, runner, {
        "executions": executions,
        "tool_calls": tool_calls,
    }


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
