from __future__ import annotations

import asyncio
from pathlib import Path

from riftx.context import EvidenceSource, FactCandidate
from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.working_memory_repositories import (
    SQLAlchemyWorkingMemoryRepository,
)
from riftx.subagents import PrimaryResultMerger, SubagentResult, SubagentStatus


def result(index: int) -> SubagentResult:
    source_ref = f"artifact://probe-{index}"
    return SubagentResult(
        task_id=f"task-{index}",
        status=SubagentStatus.COMPLETED,
        summary=f"Service {index} confirmed",
        confirmed_fact_candidates=[
            FactCandidate(
                subject=f"10.0.0.{index}",
                predicate="service:443.product",
                value="nginx",
                natural_language=f"10.0.0.{index}:443 runs nginx",
                confidence=0.99,
                source_refs=[source_ref],
                source_type=EvidenceSource.DETERMINISTIC_PARSER,
            )
        ],
        evidence_refs=[source_ref],
    )


async def test_parallel_results_merge_through_primary_working_memory_reducer(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'merge.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Inspect three assets"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    repository = SQLAlchemyWorkingMemoryRepository(database.session_factory)
    merger = PrimaryResultMerger(repository, max_conflict_retries=10)

    merged = await asyncio.gather(*(merger.merge("run-1", result(i)) for i in range(1, 4)))
    memory = await repository.get_for_run("run-1")

    assert memory is not None
    assert {fact.subject for fact in memory.confirmed_facts} == {
        "10.0.0.1",
        "10.0.0.2",
        "10.0.0.3",
    }
    assert all(item.working_memory_version is not None for item in merged)
    assert memory.version == 4
    await database.dispose()
