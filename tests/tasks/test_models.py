from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from riftx.tasks import (
    Task,
    TaskAttempt,
    TaskAttemptStatus,
    TaskBudget,
    TaskDependency,
    TaskEvidenceRequirement,
    TaskGraph,
)


def task(task_id: str, sequence: int, *, parent_task_id: str | None = None) -> Task:
    return Task(
        id=task_id,
        run_id="run-1",
        parent_task_id=parent_task_id,
        sequence=sequence,
        title=f"Task {task_id}",
    )


def test_task_graph_rejects_dependency_and_parent_cycles() -> None:
    with pytest.raises(ValidationError, match="dependency graph must be acyclic"):
        TaskGraph(
            run_id="run-1",
            tasks=[task("task-1", 1), task("task-2", 2)],
            dependencies=[
                TaskDependency(
                    run_id="run-1",
                    task_id="task-1",
                    depends_on_task_id="task-2",
                ),
                TaskDependency(
                    run_id="run-1",
                    task_id="task-2",
                    depends_on_task_id="task-1",
                ),
            ],
        )

    with pytest.raises(ValidationError, match="parent hierarchy must be acyclic"):
        TaskGraph(
            run_id="run-1",
            tasks=[
                task("task-1", 1, parent_task_id="task-2"),
                task("task-2", 2, parent_task_id="task-1"),
            ],
        )


def test_task_graph_requires_owned_attempt_lineage_budget_and_evidence() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    graph = TaskGraph(
        run_id="run-1",
        tasks=[task("task-1", 1), task("task-2", 2)],
        dependencies=[
            TaskDependency(
                run_id="run-1",
                task_id="task-2",
                depends_on_task_id="task-1",
            )
        ],
        attempts=[
            TaskAttempt(
                id="attempt-1",
                run_id="run-1",
                task_id="task-1",
                sequence=1,
                status=TaskAttemptStatus.FAILED,
                worker_id="worker-1",
                failure_summary="Target was temporarily unavailable",
                started_at=now,
                finished_at=now,
            ),
            TaskAttempt(
                id="attempt-2",
                run_id="run-1",
                task_id="task-1",
                sequence=2,
                status=TaskAttemptStatus.RUNNING,
                worker_id="worker-2",
                retry_of_attempt_id="attempt-1",
                started_at=now,
            ),
        ],
        budgets=[TaskBudget(run_id="run-1", task_id="task-1", max_tool_calls=4)],
        evidence_requirements=[
            TaskEvidenceRequirement(
                id="requirement-1",
                run_id="run-1",
                task_id="task-1",
                evidence_type="artifact",
                description="Retain the scanner output",
                minimum_count=1,
                evidence_refs=["artifact-1"],
            )
        ],
    )

    assert graph.attempts[1].retry_of_attempt_id == "attempt-1"
    assert graph.evidence_requirements[0].satisfied is True

    with pytest.raises(ValidationError, match="earlier attempt on the same task"):
        TaskGraph(
            run_id="run-1",
            tasks=[task("task-1", 1), task("task-2", 2)],
            attempts=[
                graph.attempts[0],
                graph.attempts[1].model_copy(update={"task_id": "task-2"}),
            ],
        )

    with pytest.raises(ValidationError, match="failed attempt"):
        TaskGraph(
            run_id="run-1",
            tasks=[task("task-1", 1)],
            attempts=[
                graph.attempts[0].model_copy(
                    update={
                        "status": TaskAttemptStatus.SUCCEEDED,
                        "failure_summary": None,
                    }
                ),
                graph.attempts[1],
            ],
        )


def test_task_budget_rejects_an_empty_limit_set() -> None:
    with pytest.raises(ValidationError, match="at least one limit"):
        TaskBudget(run_id="run-1", task_id="task-1")
