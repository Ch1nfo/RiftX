from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from riftx.domain import Engagement, ExecutionStatus, ExecutorType, Objective, Run
from riftx.execution import (
    ExecutionService,
    ExecutionWaitStatus,
    SubmitExecutionRequest,
)
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
from riftx.runner import ProcessSupervisor, RunnerPaths
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ToolCallIntent,
    ToolCallStatus,
)


async def build_service(
    tmp_path: Path,
) -> tuple[Database, ExecutionService, ProcessSupervisor]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'wait.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Wait for one durable tool"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="test"))
    await SQLAlchemyAgentCycleRepository(database.session_factory).create(
        AgentCycle(id="cycle-1", run_id="run-1", session_id="session-1", sequence=1)
    )
    await SQLAlchemyAgentStepRepository(database.session_factory).create(
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
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.1,
    )
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=supervisor,
    )
    return database, service, supervisor


def request(
    tmp_path: Path,
    script: str,
    *,
    timeout_seconds: float | None = None,
) -> SubmitExecutionRequest:
    return SubmitExecutionRequest(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        node_id="local",
        executor_type=ExecutorType.PROCESS,
        cwd=tmp_path,
        argv=[sys.executable, "-c", script],
        tool_id="shell",
        timeout_seconds=timeout_seconds,
    )


async def test_wait_timeout_preserves_running_then_second_wait_completes(
    tmp_path: Path,
) -> None:
    database, service, supervisor = await build_service(tmp_path)
    execution = await service.submit(
        request(
            tmp_path,
            "import time; print('started', flush=True); time.sleep(0.2); print('done')",
        )
    )

    first = await service.wait(execution.id, timeout_seconds=0.02)
    assert first.wait_status is ExecutionWaitStatus.WAIT_TIMEOUT
    assert first.execution.status is ExecutionStatus.RUNNING
    assert first.next_poll_after_seconds == 10

    second = await service.wait(
        execution.id,
        timeout_seconds=2,
        stdout_cursor=first.stdout_cursor,
        stderr_cursor=first.stderr_cursor,
    )
    assert second.wait_status is ExecutionWaitStatus.EXECUTION_COMPLETED
    assert second.execution.status is ExecutionStatus.COMPLETED
    assert second.next_poll_after_seconds is None
    assert second.partial_output is not None and "done" in second.partial_output
    await supervisor.close()
    await database.dispose()


async def test_user_cancel_returns_execution_cancelled(tmp_path: Path) -> None:
    database, service, supervisor = await build_service(tmp_path)
    execution = await service.submit(request(tmp_path, "import time; time.sleep(30)"))

    cancelled = await service.cancel(execution.id, reason="user requested")
    waited = await service.wait(execution.id, timeout_seconds=1)

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert waited.wait_status is ExecutionWaitStatus.EXECUTION_CANCELLED
    assert waited.execution.status is ExecutionStatus.CANCELLED
    await supervisor.close()
    await database.dispose()


async def test_hard_timeout_is_execution_result_not_wait_timeout(tmp_path: Path) -> None:
    database, service, supervisor = await build_service(tmp_path)
    execution = await service.submit(
        request(tmp_path, "import time; time.sleep(30)", timeout_seconds=0.05)
    )

    result = await service.wait(execution.id, timeout_seconds=2)

    assert result.wait_status is ExecutionWaitStatus.EXECUTION_COMPLETED
    assert result.execution.status is ExecutionStatus.HARD_TIMEOUT
    await supervisor.close()
    await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_cancel_terminates_entire_child_process_group(tmp_path: Path) -> None:
    database, service, supervisor = await build_service(tmp_path)
    marker = tmp_path / "child-terminated"
    ready = tmp_path / "child-ready"
    child = (
        "import pathlib,signal,sys,time; "
        "marker=pathlib.Path(sys.argv[1]); ready=pathlib.Path(sys.argv[2]); "
        "signal.signal(signal.SIGTERM, lambda *_: (marker.write_text('terminated'), sys.exit(0))); "
        "ready.write_text('ready'); time.sleep(30)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, {str(marker)!r}, {str(ready)!r}]); "
        "time.sleep(30)"
    )
    execution = await service.submit(request(tmp_path, parent))
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    assert ready.exists()

    await service.cancel(execution.id, reason="cancel process tree")

    assert marker.read_text() == "terminated"
    await supervisor.close()
    await database.dispose()
