from datetime import UTC, datetime
from pathlib import Path

from riftx.domain import Engagement, Execution, ExecutionStatus, ExecutorType, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)


async def test_execution_repository_claim_is_idempotent(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Test")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test"),
            workspace_path=str(tmp_path),
        )
    )
    repository = SQLAlchemyExecutionRepository(database.session_factory)
    execution = Execution(
        id="execution-1",
        execution_key="stable-key",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        argv=["printf", "ok"],
        tool_id="printf",
        tool_version="coreutils 9",
        executable_path="/usr/bin/printf",
        cwd=str(tmp_path),
        platform_system="linux",
        platform_release="6.10",
        platform_architecture="x86_64",
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
        process_created_at=datetime(2026, 7, 30, tzinfo=UTC),
    )

    first, first_created = await repository.create_if_absent(execution)
    duplicate = execution.model_copy(update={"id": "execution-2"})
    second, second_created = await repository.create_if_absent(duplicate)
    created_active = await repository.list_active()
    first.transition_to(ExecutionStatus.STARTING)
    await repository.save(first)
    active = await repository.list_active()
    listed = await repository.list("run-1")

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert [item.id for item in created_active] == [first.id]
    assert created_active[0].status is ExecutionStatus.CREATED
    assert [item.id for item in active] == [first.id]
    assert [item.id for item in listed] == [first.id]
    assert listed[0].tool_id == "printf"
    assert listed[0].tool_version == "coreutils 9"
    assert listed[0].executable_path == "/usr/bin/printf"
    assert listed[0].platform_system == "linux"
    assert listed[0].platform_architecture == "x86_64"
    assert listed[0].process_created_at == datetime(2026, 7, 30, tzinfo=UTC)
    await database.dispose()
