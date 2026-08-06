from __future__ import annotations

from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import WorkingMemoryProposalApplicationService
from riftx.context import (
    AttemptRecord,
    AttemptStatus,
    CurrentFocus,
    PlanItemStatus,
    PlanItemUpdate,
    PlanUpdateProposal,
)
from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTaskGraphRepository,
)
from riftx.persistence.working_memory_repositories import (
    SQLAlchemyWorkingMemoryRepository,
)
from riftx.tasks import Task, TaskGraph


async def create_service(
    tmp_path: Path,
) -> tuple[
    Database,
    WorkingMemoryProposalApplicationService,
    SQLAlchemyTaskGraphRepository,
]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'working-memory-proposals.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Cognitive proposals")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Reason about the authorized target"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    task_graphs = SQLAlchemyTaskGraphRepository(database.session_factory)
    return (
        database,
        WorkingMemoryProposalApplicationService(
            runs=runs,
            task_graphs=task_graphs,
            working_memory=SQLAlchemyWorkingMemoryRepository(database.session_factory),
        ),
        task_graphs,
    )


async def test_plan_proposal_is_reduced_and_version_guarded(tmp_path: Path) -> None:
    database, service, _ = await create_service(tmp_path)
    try:
        memory = await service.propose_plan_update(
            run_id="run-1",
            expected_memory_version=0,
            proposal=PlanUpdateProposal(
                item_updates=[
                    PlanItemUpdate(
                        item_id="step-1",
                        task="Inspect the target",
                        status=PlanItemStatus.PENDING,
                    )
                ]
            ),
        )
        assert memory.version == 2
        assert memory.run_plan.items[0].task == "Inspect the target"

        with pytest.raises(ApplicationConflictError) as stale:
            await service.propose_plan_update(
                run_id="run-1",
                expected_memory_version=1,
                proposal=PlanUpdateProposal(
                    current_focus=CurrentFocus(
                        phase="recon",
                        objective="Identify exposed services",
                    )
                ),
            )
        assert stale.value.code == "working_memory_version_conflict"
    finally:
        await database.dispose()


async def test_task_graph_rejects_legacy_plan_topology_but_allows_focus(
    tmp_path: Path,
) -> None:
    database, service, task_graphs = await create_service(tmp_path)
    try:
        await task_graphs.create(
            TaskGraph(
                run_id="run-1",
                tasks=[Task(id="task-1", run_id="run-1", sequence=1, title="Recon")],
            )
        )
        with pytest.raises(ApplicationConflictError) as rejected:
            await service.propose_plan_update(
                run_id="run-1",
                expected_memory_version=0,
                proposal=PlanUpdateProposal(
                    item_updates=[
                        PlanItemUpdate(item_id="legacy-step", task="Legacy plan mutation")
                    ]
                ),
            )
        assert rejected.value.code == "task_graph_plan_authoritative"

        memory = await service.propose_plan_update(
            run_id="run-1",
            expected_memory_version=0,
            proposal=PlanUpdateProposal(
                current_focus=CurrentFocus(
                    phase="recon",
                    objective="Inspect the current durable Task",
                    plan_item_id="task-1",
                )
            ),
        )
        assert memory.current_focus is not None
        assert memory.current_focus.plan_item_id == "task-1"
    finally:
        await database.dispose()


def attempt(
    attempt_id: str,
    *,
    retryable: bool,
    retry_of_attempt_id: str | None = None,
    retry_reason: str | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        id=attempt_id,
        action_signature="scan-directories:v1",
        target="https://192.0.2.10",
        tool_id="feroxbuster",
        normalized_arguments={"wordlist": "large.txt"},
        result_status=AttemptStatus.FAILED,
        result_summary="Target rate limit stopped the scan",
        retryable=retryable,
        retry_of_attempt_id=retry_of_attempt_id,
        retry_reason=retry_reason,
    )


async def test_failed_attempt_requires_explicit_retry_relation(tmp_path: Path) -> None:
    database, service, _ = await create_service(tmp_path)
    try:
        memory = await service.record_attempt(
            run_id="run-1",
            expected_memory_version=0,
            attempt=attempt("attempt-1", retryable=True),
        )
        with pytest.raises(ApplicationConflictError) as duplicate:
            await service.record_attempt(
                run_id="run-1",
                expected_memory_version=memory.version,
                attempt=attempt("attempt-2", retryable=True),
            )
        assert duplicate.value.code == "working_memory_duplicate_attempt"

        retried = await service.record_attempt(
            run_id="run-1",
            expected_memory_version=memory.version,
            attempt=attempt(
                "attempt-2",
                retryable=False,
                retry_of_attempt_id="attempt-1",
                retry_reason="Retry with the authorized lower request rate",
            ),
        )
        assert retried.version == memory.version + 1
        assert retried.attempts[-1].retry_of_attempt_id == "attempt-1"
    finally:
        await database.dispose()
