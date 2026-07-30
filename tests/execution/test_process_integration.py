from __future__ import annotations

import sys
from pathlib import Path

from riftx.domain import Engagement, ExecutionStatus, ExecutorType, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import ExecutionLaunchRequest, ProcessSupervisor, RunnerPaths
from riftx.runtime.types import AgentSession


async def test_runtime_execution_is_persisted_before_one_process_launch(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime-execution.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Execute once"),
            workspace_path=str(tmp_path),
        )
    )
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="session-1", run_id="run-1", model_profile="test")
    )
    repository = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = ProcessSupervisor(repository, RunnerPaths(tmp_path / "runner"))
    request = ExecutionLaunchRequest(
        execution_key="execution:v1:test",
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
        node_id="local",
        executor_type=ExecutorType.PROCESS,
        cwd=tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(0.1); print('done')"],
    )

    first = await supervisor.start(request)
    duplicate = await supervisor.start(request)
    assert first.id == duplicate.id
    assert first.status is ExecutionStatus.RUNNING
    persisted = await repository.get(first.id)
    assert persisted is not None and persisted.pid is not None

    completed = await supervisor.wait(first.id)
    assert completed.status is ExecutionStatus.COMPLETED
    assert (await supervisor.start(request)).id == first.id
    await supervisor.close()
    await database.dispose()
