from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import update

from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
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
    SQLAlchemyReasoningGraphRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.orm import ReasoningNodeRecord
from riftx.reasoning import (
    ReasoningCreatorType,
    ReasoningEdge,
    ReasoningGraph,
    ReasoningNode,
    ReasoningNodeKind,
    ReasoningNodeStatus,
    ReasoningRelationType,
)


def evidence(evidence_id: str, *, run_id: str = "run-1") -> Evidence:
    locator = SourceLocator(uri=f"execution://{evidence_id}/stdout")
    return Evidence(
        id=evidence_id,
        kind=EvidenceKind.EXECUTION_OUTPUT,
        source_uri=locator.source_uri,
        digest="a" * 64,
        run_id=run_id,
        creator_type=EvidenceCreatorType.TOOL,
        created_by="run_shell",
        trust_class=EvidenceTrustClass.UNTRUSTED_TOOL_OUTPUT,
        scope=EvidenceScope(engagement_id="engagement-1", run_id=run_id),
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


def graph(*, evidence_id: str = "evidence-1") -> ReasoningGraph:
    candidate = ReasoningNode(
        id="candidate-1",
        run_id="run-1",
        kind=ReasoningNodeKind.FACT_CANDIDATE,
        status=ReasoningNodeStatus.PROMOTED,
        claim="Service version candidate",
        evidence_ids=(evidence_id,),
        creator_type=ReasoningCreatorType.PARSER,
        created_by="service-parser",
    )
    confirmed = ReasoningNode(
        id="fact-1",
        run_id="run-1",
        kind=ReasoningNodeKind.CONFIRMED_FACT,
        status=ReasoningNodeStatus.CONFIRMED,
        claim="Service version is nginx 1.24",
        evidence_ids=(evidence_id,),
        creator_type=ReasoningCreatorType.REDUCER,
        created_by="reasoning-reducer",
    )
    return ReasoningGraph(
        run_id="run-1",
        nodes=[candidate, confirmed],
        edges=[
            ReasoningEdge(
                id="derived-1",
                run_id="run-1",
                source_node_id=candidate.id,
                target_node_id=confirmed.id,
                relation_type=ReasoningRelationType.DERIVED_FROM,
                evidence_ids=(evidence_id,),
                creator_type=ReasoningCreatorType.REDUCER,
                created_by="reasoning-reducer",
            )
        ],
    )


async def database(tmp_path: Path) -> Database:
    value = Database(f"sqlite+aiosqlite:///{tmp_path / 'reasoning.db'}")
    await value.create_schema()
    await SQLAlchemyEngagementRepository(value.session_factory).create(
        Engagement(id="engagement-1", name="Reasoning")
    )
    await SQLAlchemyRunRepository(value.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Persist reasoning"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    return value


async def test_reasoning_graph_round_trip_and_restart(tmp_path: Path) -> None:
    value = await database(tmp_path)
    try:
        await SQLAlchemyEvidenceLedgerRepository(value.session_factory).create(
            evidence("evidence-1")
        )
        expected = graph()
        repository = SQLAlchemyReasoningGraphRepository(value.session_factory)
        await repository.create(expected)

        restarted = SQLAlchemyReasoningGraphRepository(value.session_factory)
        assert await restarted.get("run-1") == expected
    finally:
        await value.dispose()


async def test_reasoning_graph_rejects_missing_evidence(tmp_path: Path) -> None:
    value = await database(tmp_path)
    try:
        repository = SQLAlchemyReasoningGraphRepository(value.session_factory)
        with pytest.raises(RepositoryConflictError):
            await repository.create(graph(evidence_id="missing-evidence"))
        assert await repository.get("run-1") is None
    finally:
        await value.dispose()


async def test_reasoning_graph_corruption_fails_closed(tmp_path: Path) -> None:
    value = await database(tmp_path)
    try:
        await SQLAlchemyEvidenceLedgerRepository(value.session_factory).create(
            evidence("evidence-1")
        )
        repository = SQLAlchemyReasoningGraphRepository(value.session_factory)
        await repository.create(graph())
        async with value.session_factory() as session, session.begin():
            await session.execute(
                update(ReasoningNodeRecord)
                .where(ReasoningNodeRecord.id == "fact-1")
                .values(status=ReasoningNodeStatus.RECORDED.value)
            )

        with pytest.raises(RepositoryIntegrityError):
            await repository.get("run-1")
    finally:
        await value.dispose()
