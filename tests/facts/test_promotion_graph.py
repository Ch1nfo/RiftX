from __future__ import annotations

from pathlib import Path

from riftx.context import EvidenceSource
from riftx.domain import Engagement, Objective, Run
from riftx.facts import (
    AttackGraphService,
    FactPromotionAction,
    FactPromotionCandidate,
    FactPromotionService,
    FactRelation,
    FactRelationType,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.fact_repositories import (
    SQLAlchemyEngagementFactRepository,
    SQLAlchemyFactRelationRepository,
)
from riftx.persistence.working_memory_repositories import (
    SQLAlchemyWorkingMemoryRepository,
)


async def _services(tmp_path: Path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'facts.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Map the authorized target"),
            workspace_path=str(tmp_path),
        )
    )
    facts = SQLAlchemyEngagementFactRepository(database.session_factory)
    relations = SQLAlchemyFactRelationRepository(database.session_factory)
    return (
        database,
        FactPromotionService(
            runs=runs,
            working_memory=SQLAlchemyWorkingMemoryRepository(database.session_factory),
            facts=facts,
        ),
        AttackGraphService(facts=facts, relations=relations),
        facts,
    )


def _candidate(
    candidate_id: str,
    *,
    value: str,
    source: EvidenceSource = EvidenceSource.DETERMINISTIC_PARSER,
    confidence: float = 0.9,
    user_confirmed: bool = False,
) -> FactPromotionCandidate:
    return FactPromotionCandidate(
        id=candidate_id,
        engagement_id="engagement-1",
        source_run_id="run-1",
        source_session_id="session-1",
        working_fact_id=f"working-{candidate_id}",
        subject="https://10.10.10.20:443",
        predicate="service.version",
        value=value,
        natural_language=f"HTTPS runs {value}",
        evidence_refs=[f"execution://{candidate_id}", f"artifact://{candidate_id}"],
        evidence_sources={
            f"execution://{candidate_id}": source,
            f"artifact://{candidate_id}": source,
        },
        source_execution_ids=[candidate_id],
        artifact_ids=[candidate_id],
        confidence=confidence,
        user_confirmed=user_confirmed,
    )


async def test_model_inference_cannot_directly_become_engagement_fact(
    tmp_path: Path,
) -> None:
    database, promotion, _, facts = await _services(tmp_path)
    try:
        rejected = await promotion.promote(
            _candidate(
                "model-only",
                value="nginx 1.24",
                source=EvidenceSource.MODEL_INFERENCE,
            )
        )

        assert rejected.action is FactPromotionAction.REJECTED
        assert await facts.list_for_engagement("engagement-1") == []

        confirmed = await promotion.promote(
            _candidate(
                "user-confirmed",
                value="nginx 1.24",
                source=EvidenceSource.MODEL_INFERENCE,
                user_confirmed=True,
            )
        )
        assert confirmed.action is FactPromotionAction.CREATED
    finally:
        await database.dispose()


async def test_promotion_preserves_sources_merges_and_supersedes(tmp_path: Path) -> None:
    database, promotion, graph_service, facts = await _services(tmp_path)
    try:
        created = await promotion.promote(_candidate("scan-1", value="nginx 1.24"))
        merged = await promotion.promote(
            _candidate("scan-2", value="nginx 1.24", confidence=0.95)
        )
        replaced = await promotion.promote(
            _candidate("scan-3", value="nginx 1.25", confidence=0.99)
        )
        assert created.action is FactPromotionAction.CREATED
        assert merged.action is FactPromotionAction.MERGED
        assert merged.fact is not None
        assert merged.fact.source_run_ids == ["run-1"]
        assert merged.fact.source_execution_ids == ["scan-1", "scan-2"]
        assert merged.fact.artifact_ids == ["scan-1", "scan-2"]
        assert replaced.action is FactPromotionAction.SUPERSEDED
        assert replaced.fact is not None and created.fact is not None
        assert replaced.fact.supersedes_fact_id == created.fact.id

        target = await promotion.promote(
            _candidate("host", value="reachable").model_copy(
                update={
                    "id": "host-candidate",
                    "working_fact_id": "working-host",
                    "subject": "10.10.10.20",
                    "predicate": "host.status",
                }
            )
        )
        assert target.fact is not None
        relation = await graph_service.add_relation(
            FactRelation(
                engagement_id="engagement-1",
                source_fact_id=target.fact.id,
                target_fact_id=replaced.fact.id,
                relation_type=FactRelationType.DISCOVERED_ON,
                evidence_refs=["execution://scan-3"],
                source_run_id="run-1",
                source_session_id="session-1",
                source_execution_ids=["scan-3"],
                artifact_ids=["scan-3"],
                confidence=0.99,
            )
        )
        graph = await graph_service.graph("engagement-1")
        assert graph.relations == [relation]
        assert graph.successors(target.fact.id) == [replaced.fact]
        all_facts = await facts.list_for_engagement(
            "engagement-1", active_only=False
        )
        assert len(all_facts) == 3
    finally:
        await database.dispose()
