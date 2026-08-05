from __future__ import annotations

import sys
from pathlib import Path

from riftx.domain import Engagement, ExecutionStatus, ExecutorType, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import ExecutionLaunchRequest, ProcessSupervisor, RunnerPaths


async def test_real_runner_restart_preserves_completed_output(tmp_path: Path) -> None:
    database_path = tmp_path / "qa-runner.db"
    state_path = tmp_path / "runner-state"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="qa-runner-engagement", name="QA runner restart")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="qa-runner-run",
            engagement_id="qa-runner-engagement",
            node_id="local",
            objective=Objective(description="Verify real Runner restart recovery"),
            workspace_path=str(tmp_path),
        )
    )
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = ProcessSupervisor(executions, RunnerPaths(state_path))
    started = await supervisor.start(
        ExecutionLaunchRequest(
            execution_key="qa-real-runner-restart",
            run_id="qa-runner-run",
            node_id="local",
            executor_type=ExecutorType.PROCESS,
            cwd=tmp_path,
            argv=[
                sys.executable,
                "-c",
                "print('runner output survived restart')",
            ],
            tool_id="qa-real-process",
        )
    )
    completed = await supervisor.wait(started.id)
    await supervisor.close()
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    restarted = ProcessSupervisor(
        SQLAlchemyExecutionRepository(reopened.session_factory),
        RunnerPaths(state_path),
    )
    restored = await restarted.get(completed.id)
    output = await restarted.read_output(completed.id)

    assert restored.status is ExecutionStatus.EXITED
    assert restored.exit_code == 0
    assert output.stdout.data == b"runner output survived restart\n"
    assert output.stdout.eof
    await restarted.close()
    await reopened.dispose()
