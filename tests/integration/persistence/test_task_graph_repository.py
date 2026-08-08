from datetime import UTC, datetime
from pathlib import Path

from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTaskGraphRepository,
)
from riftx.tasks import (
    Task,
    TaskAttempt,
    TaskAttemptStatus,
    TaskBudget,
    TaskDependency,
    TaskEvidenceRequirement,
    TaskGraph,
    TaskStatus,
)


async def create_run(database: Database, run_id: str, workspace: Path) -> None:
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id=run_id,
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description=f"Objective for {run_id}"),
            workspace_path=str(workspace),
        )
    )


def graph(run_id: str) -> TaskGraph:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    return TaskGraph(
        run_id=run_id,
        tasks=[
            Task(
                id=f"{run_id}-discover",
                run_id=run_id,
                sequence=1,
                title="Discover",
                status=TaskStatus.RUNNING,
            ),
            Task(id=f"{run_id}-verify", run_id=run_id, sequence=2, title="Verify"),
        ],
        dependencies=[
            TaskDependency(
                run_id=run_id,
                task_id=f"{run_id}-verify",
                depends_on_task_id=f"{run_id}-discover",
            )
        ],
        attempts=[
            TaskAttempt(
                id=f"{run_id}-attempt-1",
                run_id=run_id,
                task_id=f"{run_id}-discover",
                sequence=1,
                status=TaskAttemptStatus.FAILED,
                worker_id="worker-1",
                failure_summary="Transient failure",
                started_at=now,
                finished_at=now,
            ),
            TaskAttempt(
                id=f"{run_id}-attempt-2",
                run_id=run_id,
                task_id=f"{run_id}-discover",
                sequence=2,
                status=TaskAttemptStatus.RUNNING,
                worker_id="worker-2",
                retry_of_attempt_id=f"{run_id}-attempt-1",
                started_at=now,
            ),
        ],
        budgets=[TaskBudget(run_id=run_id, task_id=f"{run_id}-discover", max_tool_calls=3)],
        evidence_requirements=[
            TaskEvidenceRequirement(
                id=f"{run_id}-evidence-1",
                run_id=run_id,
                task_id=f"{run_id}-discover",
                evidence_type="artifact",
                description="Preserve discovery output",
            )
        ],
    )


async def test_task_graph_survives_repository_restart_and_isolates_runs(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'task-graph.db'}")
    await database.create_schema()
    try:
        await SQLAlchemyEngagementRepository(database.session_factory).create(
            Engagement(id="engagement-1", name="Authorized engagement")
        )
        await create_run(database, "run-1", tmp_path / "run-1")
        await create_run(database, "run-2", tmp_path / "run-2")
        first = graph("run-1")
        second = graph("run-2")
        await SQLAlchemyTaskGraphRepository(database.session_factory).create(first)
        await SQLAlchemyTaskGraphRepository(database.session_factory).create(second)

        restarted_repository = SQLAlchemyTaskGraphRepository(database.session_factory)
        assert await restarted_repository.get("run-1") == first
        assert await restarted_repository.get("run-2") == second
        assert await restarted_repository.get("missing-run") is None
    finally:
        await database.dispose()
