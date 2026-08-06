import asyncio
from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.domain import Engagement, Objective, Run
from riftx.evidence import (
    Evidence,
    EvidenceCreatorType,
    EvidenceKind,
    EvidenceRedactionStatus,
    EvidenceReplayMetadata,
    EvidenceReplayStrategy,
    EvidenceScope,
    EvidenceTrustClass,
    SourceLocator,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyEvidenceLedgerRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTaskGraphRepository,
    SQLAlchemyTaskPlanner,
)
from riftx.tasks import (
    AddTaskCommand,
    BlockTaskCommand,
    CancelTaskCommand,
    ClaimReadyTaskCommand,
    CompleteTaskCommand,
    FailTaskAttemptCommand,
    LinkTasksCommand,
    ReopenTaskCommand,
    TaskEvidenceRequirementInput,
    TaskStatus,
    UpdateTaskCommand,
)


async def create_planner(tmp_path: Path) -> tuple[Database, SQLAlchemyTaskPlanner]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'task-planner.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized engagement")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Exercise the durable Task Planner"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    return database, SQLAlchemyTaskPlanner(database.session_factory)


def task_evidence(evidence_id: str, *, task_id: str | None) -> Evidence:
    locator = SourceLocator(uri=f"execution://{evidence_id}/stdout")
    return Evidence(
        id=evidence_id,
        kind=EvidenceKind.EXECUTION_OUTPUT,
        source_uri=locator.source_uri,
        digest="a" * 64,
        run_id="run-1",
        task_id=task_id,
        creator_type=EvidenceCreatorType.TOOL,
        created_by="run_shell",
        trust_class=EvidenceTrustClass.UNTRUSTED_TOOL_OUTPUT,
        scope=EvidenceScope(engagement_id="engagement-1", run_id="run-1"),
        redaction_status=EvidenceRedactionStatus.METADATA_ONLY,
        replay=EvidenceReplayMetadata(
            strategy=EvidenceReplayStrategy.SOURCE_LOOKUP,
            replayable=True,
            expected_digest="a" * 64,
            source_digest="a" * 64,
            parameters_digest="b" * 64,
        ),
        locator=locator,
    )


async def test_ready_claim_completion_retry_and_parallel_isolation(tmp_path: Path) -> None:
    database, planner = await create_planner(tmp_path)
    try:
        result = await planner.add_task(
            AddTaskCommand(
                run_id="run-1",
                expected_graph_version=0,
                task_id="discover",
                title="Discover attack surface",
                evidence_requirements=[
                    TaskEvidenceRequirementInput(
                        id="discover-output",
                        evidence_type="artifact",
                        description="Preserve discovery output",
                        minimum_count=2,
                    )
                ],
            )
        )
        assert result.graph_version == 1
        result = await planner.add_task(
            AddTaskCommand(
                run_id="run-1",
                expected_graph_version=1,
                task_id="verify",
                title="Verify candidates",
            )
        )
        result = await planner.add_task(
            AddTaskCommand(
                run_id="run-1",
                expected_graph_version=result.graph_version,
                task_id="research",
                title="Research target technology",
            )
        )
        result = await planner.link_tasks(
            LinkTasksCommand(
                run_id="run-1",
                expected_graph_version=result.graph_version,
                task_id="verify",
                depends_on_task_id="discover",
            )
        )
        assert [task.id for task in await planner.list_ready("run-1")] == [
            "discover",
            "research",
        ]

        claims = await asyncio.gather(
            planner.claim_ready_task(ClaimReadyTaskCommand(run_id="run-1", worker_id="worker-a")),
            planner.claim_ready_task(ClaimReadyTaskCommand(run_id="run-1", worker_id="worker-b")),
        )
        assert all(claim is not None for claim in claims)
        claimed = {claim.task.id: claim for claim in claims if claim is not None}
        assert set(claimed) == {"discover", "research"}
        assert claimed["discover"].attempt is not None
        assert claimed["research"].attempt is not None
        assert claimed["discover"].attempt.worker_id != claimed["research"].attempt.worker_id
        graph_version = max(claim.graph_version for claim in claimed.values())

        discover_attempt = claimed["discover"].attempt
        assert discover_attempt is not None
        with pytest.raises(RepositoryConflictError, match="not satisfied"):
            await planner.complete_task(
                CompleteTaskCommand(
                    run_id="run-1",
                    expected_graph_version=graph_version,
                    task_id="discover",
                    attempt_id=discover_attempt.id,
                    completion_summary="Discovery completed",
                )
            )
        with pytest.raises(RepositoryConflictError, match="outside the current Run or Task"):
            await planner.complete_task(
                CompleteTaskCommand(
                    run_id="run-1",
                    expected_graph_version=graph_version,
                    task_id="discover",
                    attempt_id=discover_attempt.id,
                    completion_summary="Discovery completed",
                    evidence_refs_by_requirement={"discover-output": ["missing-evidence"]},
                )
            )
        evidence_ledger = SQLAlchemyEvidenceLedgerRepository(database.session_factory)
        await evidence_ledger.create(task_evidence("research-evidence", task_id="research"))
        with pytest.raises(RepositoryConflictError, match="outside the current Run or Task"):
            await planner.complete_task(
                CompleteTaskCommand(
                    run_id="run-1",
                    expected_graph_version=graph_version,
                    task_id="discover",
                    attempt_id=discover_attempt.id,
                    completion_summary="Discovery completed",
                    evidence_refs_by_requirement={
                        "discover-output": ["research-evidence"]
                    },
                )
            )
        await evidence_ledger.create(task_evidence("discover-evidence", task_id="discover"))
        await evidence_ledger.create(task_evidence("run-evidence", task_id=None))
        completed = await planner.complete_task(
            CompleteTaskCommand(
                run_id="run-1",
                expected_graph_version=graph_version,
                task_id="discover",
                attempt_id=discover_attempt.id,
                completion_summary="Discovery completed",
                evidence_refs_by_requirement={
                    "discover-output": ["discover-evidence", "run-evidence"]
                },
            )
        )
        assert completed.task.status is TaskStatus.COMPLETED
        assert [task.id for task in await planner.list_ready("run-1")] == ["verify"]

        research_attempt = claimed["research"].attempt
        assert research_attempt is not None
        failed = await planner.fail_task_attempt(
            FailTaskAttemptCommand(
                run_id="run-1",
                expected_graph_version=completed.graph_version,
                task_id="research",
                attempt_id=research_attempt.id,
                failure_summary="Research provider was unavailable",
            )
        )
        assert failed.task.status is TaskStatus.FAILED
        assert "research" not in {task.id for task in await planner.list_ready("run-1")}

        reopened = await planner.reopen_task(
            ReopenTaskCommand(
                run_id="run-1",
                expected_graph_version=failed.graph_version,
                task_id="research",
                reason="Retry after provider recovery",
            )
        )
        retry = await planner.claim_ready_task(
            ClaimReadyTaskCommand(
                run_id="run-1",
                worker_id="worker-c",
                preferred_task_id="research",
            )
        )
        assert retry is not None and retry.attempt is not None
        assert retry.graph_version == reopened.graph_version + 1
        assert retry.attempt.retry_of_attempt_id == research_attempt.id

        recovered = await SQLAlchemyTaskGraphRepository(database.session_factory).get("run-1")
        assert recovered is not None
        assert len(recovered.attempts) == 3
        assert {attempt.worker_id for attempt in recovered.attempts} == {
            "worker-a",
            "worker-b",
            "worker-c",
        }
    finally:
        await database.dispose()


async def test_planner_rejects_stale_versions_cycles_and_illegal_transitions(
    tmp_path: Path,
) -> None:
    database, planner = await create_planner(tmp_path)
    try:
        version = 0
        for task_id in ("task-a", "task-b", "task-c"):
            result = await planner.add_task(
                AddTaskCommand(
                    run_id="run-1",
                    expected_graph_version=version,
                    task_id=task_id,
                    title=task_id,
                )
            )
            version = result.graph_version

        with pytest.raises(RepositoryConflictError, match="version conflict"):
            await planner.update_task(
                UpdateTaskCommand(
                    run_id="run-1",
                    expected_graph_version=version - 1,
                    task_id="task-a",
                    title="stale update",
                )
            )

        linked = await planner.link_tasks(
            LinkTasksCommand(
                run_id="run-1",
                expected_graph_version=version,
                task_id="task-b",
                depends_on_task_id="task-a",
            )
        )
        with pytest.raises(RepositoryConflictError, match="cycle"):
            await planner.link_tasks(
                LinkTasksCommand(
                    run_id="run-1",
                    expected_graph_version=linked.graph_version,
                    task_id="task-a",
                    depends_on_task_id="task-b",
                )
            )

        updated = await planner.update_task(
            UpdateTaskCommand(
                run_id="run-1",
                expected_graph_version=linked.graph_version,
                task_id="task-c",
                title="Updated task C",
                required_capability_ids=["code.read", "code.search"],
            )
        )
        blocked = await planner.block_task(
            BlockTaskCommand(
                run_id="run-1",
                expected_graph_version=updated.graph_version,
                task_id="task-c",
                reason="Awaiting operator scope",
            )
        )
        assert blocked.task.status is TaskStatus.BLOCKED
        reopened = await planner.reopen_task(
            ReopenTaskCommand(
                run_id="run-1",
                expected_graph_version=blocked.graph_version,
                task_id="task-c",
                reason="Operator supplied scope",
            )
        )
        claim = await planner.claim_ready_task(
            ClaimReadyTaskCommand(
                run_id="run-1",
                worker_id="worker-c",
                preferred_task_id="task-c",
            )
        )
        assert claim is not None and claim.attempt is not None
        cancelled = await planner.cancel_task(
            CancelTaskCommand(
                run_id="run-1",
                expected_graph_version=claim.graph_version,
                task_id="task-c",
                reason="Operator changed priorities",
            )
        )
        assert cancelled.task.status is TaskStatus.CANCELLED
        assert cancelled.attempt is not None
        assert cancelled.attempt.status.value == "cancelled"
        assert cancelled.graph_version == reopened.graph_version + 2

        with pytest.raises(RepositoryConflictError, match="different cancellation reason"):
            await planner.cancel_task(
                CancelTaskCommand(
                    run_id="run-1",
                    expected_graph_version=cancelled.graph_version,
                    task_id="task-c",
                    reason="A different decision",
                )
            )

        owned = await planner.add_task(
            AddTaskCommand(
                run_id="run-1",
                expected_graph_version=cancelled.graph_version,
                task_id="owned-task",
                title="Session-owned task",
                session_owner_id="session-a",
            )
        )
        assert (
            await planner.claim_ready_task(
                ClaimReadyTaskCommand(
                    run_id="run-1",
                    worker_id="worker-b",
                    session_id="session-b",
                    preferred_task_id="owned-task",
                )
            )
            is None
        )
        owned_claim = await planner.claim_ready_task(
            ClaimReadyTaskCommand(
                run_id="run-1",
                worker_id="worker-a",
                session_id="session-a",
                preferred_task_id="owned-task",
            )
        )
        assert owned_claim is not None
        assert owned_claim.graph_version == owned.graph_version + 1
        assert owned_claim.attempt is not None
        with pytest.raises(RepositoryConflictError, match="different Agent Session"):
            await planner.fail_task_attempt(
                FailTaskAttemptCommand(
                    run_id="run-1",
                    expected_graph_version=owned_claim.graph_version,
                    task_id="owned-task",
                    attempt_id=owned_claim.attempt.id,
                    actor_session_id="session-b",
                    failure_summary="Must not cross Session ownership",
                )
            )
    finally:
        await database.dispose()
