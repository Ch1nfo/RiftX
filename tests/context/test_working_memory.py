from __future__ import annotations

from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.context import (
    AttemptRecord,
    AttemptStatus,
    DuplicateAttemptError,
    EvidenceSource,
    FactCandidate,
    FactStatus,
    HypothesisEvidenceEffect,
    HypothesisStatus,
    HypothesisUpdate,
    PlanItemStatus,
    PlanItemUpdate,
    PlanRegressionError,
    PlanUpdateProposal,
    WorkingMemory,
    WorkingMemoryReducer,
    WorkingMemoryVersionConflict,
)
from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.working_memory_repositories import (
    SQLAlchemyWorkingMemoryRepository,
)


def fact_candidate(
    *,
    subject: str = "asset:192.0.2.10",
    predicate: str = "service:443.product",
    value: str = "nginx",
    confidence: float = 0.4,
    source_ref: str = "model://response-1",
    source_type: EvidenceSource = EvidenceSource.MODEL_INFERENCE,
) -> FactCandidate:
    return FactCandidate(
        subject=subject,
        predicate=predicate,
        value=value,
        natural_language=f"{subject} {predicate} is {value}",
        confidence=confidence,
        source_refs=[source_ref],
        source_type=source_type,
    )


def test_fact_is_added_and_independent_sources_raise_confidence() -> None:
    reducer = WorkingMemoryReducer()
    memory = WorkingMemory(run_id="run-1")

    memory = reducer.reduce(
        memory,
        expected_version=1,
        fact_candidates=[fact_candidate()],
    )
    original_confidence = memory.confirmed_facts[0].confidence
    memory = reducer.reduce(
        memory,
        expected_version=2,
        fact_candidates=[
            fact_candidate(
                confidence=0.5,
                source_ref="artifact://runs/run-1/executions/execution-1/stdout",
                source_type=EvidenceSource.DETERMINISTIC_PARSER,
            )
        ],
    )

    assert len(memory.confirmed_facts) == 1
    fact = memory.confirmed_facts[0]
    assert fact.status is FactStatus.CONFIRMED
    assert fact.confidence > original_confidence
    assert fact.confidence >= 0.95
    assert fact.source_refs == [
        "model://response-1",
        "artifact://runs/run-1/executions/execution-1/stdout",
    ]


def test_conflicting_fact_is_retained_and_deterministic_parser_wins() -> None:
    reducer = WorkingMemoryReducer()
    memory = reducer.reduce(
        WorkingMemory(run_id="run-1"),
        expected_version=1,
        fact_candidates=[fact_candidate(value="nginx")],
    )

    memory = reducer.reduce(
        memory,
        expected_version=2,
        fact_candidates=[
            fact_candidate(
                value="Apache httpd",
                confidence=0.9,
                source_ref="artifact://runs/run-1/executions/nmap-1/stdout",
                source_type=EvidenceSource.DETERMINISTIC_PARSER,
            )
        ],
    )

    assert len(memory.confirmed_facts) == 2
    by_value = {fact.value: fact for fact in memory.confirmed_facts}
    assert by_value["nginx"].status is FactStatus.DISPUTED
    assert by_value["Apache httpd"].status is FactStatus.CONFIRMED
    assert by_value["Apache httpd"].confidence >= 0.95


def test_hypothesis_can_be_supported_and_then_rejected_by_facts() -> None:
    reducer = WorkingMemoryReducer()
    memory = reducer.reduce(
        WorkingMemory(run_id="run-1"),
        expected_version=1,
        fact_candidates=[
            fact_candidate(predicate="path:/admin.status", value="open", source_ref="parser://1"),
            fact_candidate(predicate="auth.required", value="true", source_ref="parser://2"),
            fact_candidate(predicate="exploit.result", value="blocked", source_ref="parser://3"),
        ],
    )
    supporting_fact, *contradicting_facts = memory.confirmed_facts

    memory = reducer.reduce(
        memory,
        expected_version=2,
        hypothesis_updates=[
            HypothesisUpdate(
                hypothesis_id="hypothesis-1",
                statement="The admin path is exploitable without credentials",
                evidence_effect=HypothesisEvidenceEffect.SUPPORTS,
                fact_ids=[supporting_fact.id],
                initial_confidence=0.3,
                next_validation_action="Attempt a read-only authorization check",
            )
        ],
    )
    hypothesis = memory.hypotheses[0]
    assert hypothesis.status is HypothesisStatus.SUPPORTED
    assert hypothesis.supporting_fact_ids == [supporting_fact.id]

    memory = reducer.reduce(
        memory,
        expected_version=3,
        hypothesis_updates=[
            HypothesisUpdate(
                hypothesis_id="hypothesis-1",
                evidence_effect=HypothesisEvidenceEffect.CONTRADICTS,
                fact_ids=[fact.id for fact in contradicting_facts],
            )
        ],
    )
    hypothesis = memory.hypotheses[0]
    assert hypothesis.status is HypothesisStatus.REJECTED
    assert hypothesis.confidence <= 0.2
    assert hypothesis.contradicting_fact_ids == [fact.id for fact in contradicting_facts]


def test_failed_attempt_blocks_unexplained_duplicate() -> None:
    reducer = WorkingMemoryReducer()
    failed = AttemptRecord(
        id="attempt-1",
        action_signature="scan-directories:v1",
        target="https://192.0.2.10",
        tool_id="feroxbuster",
        normalized_arguments={"wordlist": "large.txt"},
        result_status=AttemptStatus.FAILED,
        result_summary="Noise exceeded the useful result threshold",
        retryable=False,
    )
    memory = reducer.reduce(
        WorkingMemory(run_id="run-1"),
        expected_version=1,
        attempts=[failed],
    )

    with pytest.raises(DuplicateAttemptError, match="already failed"):
        reducer.reduce(
            memory,
            expected_version=2,
            attempts=[failed.model_copy(update={"id": "attempt-2"})],
        )


def test_completed_plan_item_requires_reason_to_reopen() -> None:
    reducer = WorkingMemoryReducer()
    memory = reducer.reduce(
        WorkingMemory(run_id="run-1"),
        expected_version=1,
        plan_update=PlanUpdateProposal(
            item_updates=[
                PlanItemUpdate(
                    item_id="step-1",
                    task="Identify the web stack",
                    status=PlanItemStatus.COMPLETED,
                    completion_summary="Nmap and HTTP parsing completed",
                )
            ]
        ),
    )

    with pytest.raises(PlanRegressionError, match="cannot regress"):
        reducer.reduce(
            memory,
            expected_version=2,
            plan_update=PlanUpdateProposal(
                item_updates=[
                    PlanItemUpdate(item_id="step-1", status=PlanItemStatus.RUNNING)
                ]
            ),
        )


async def test_working_memory_optimistic_lock_rejects_stale_writer(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'working-memory.db'}")
    await database.create_schema()
    try:
        await SQLAlchemyEngagementRepository(database.session_factory).create(
            Engagement(id="engagement-1", name="Authorized engagement")
        )
        await SQLAlchemyRunRepository(database.session_factory).create(
            Run(
                kind="general",
                id="run-1",
                engagement_id="engagement-1",
                node_id="node-1",
                objective=Objective(description="Inspect the authorized target"),
                workspace_path=str(tmp_path / "workspace"),
            )
        )
        repository = SQLAlchemyWorkingMemoryRepository(database.session_factory)
        reducer = WorkingMemoryReducer()
        original = WorkingMemory(id="memory-1", run_id="run-1")
        await repository.create(original)

        first_writer = reducer.reduce(
            original,
            expected_version=1,
            fact_candidates=[fact_candidate()],
        )
        stale_writer = reducer.reduce(
            original,
            expected_version=1,
            fact_candidates=[fact_candidate(predicate="service:443.version", value="1.24")],
        )
        await repository.save(first_writer, expected_version=1)

        with pytest.raises(RepositoryConflictError, match="version conflict"):
            await repository.save(stale_writer, expected_version=1)
        with pytest.raises(WorkingMemoryVersionConflict, match="version conflict"):
            reducer.reduce(first_writer, expected_version=1)

        assert await repository.get("memory-1") == first_writer
        assert await repository.get_for_run("run-1") == first_writer
    finally:
        await database.dispose()
