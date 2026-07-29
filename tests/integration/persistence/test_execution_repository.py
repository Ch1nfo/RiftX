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
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
    )

    first, first_created = await repository.create_if_absent(execution)
    duplicate = execution.model_copy(update={"id": "execution-2"})
    second, second_created = await repository.create_if_absent(duplicate)
    first.transition_to(ExecutionStatus.STARTING)
    await repository.save(first)
    active = await repository.list_active()

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert [item.id for item in active] == [first.id]
    await database.dispose()
