from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import (
    EvidenceApplicationService,
    QueryReasoningGraph,
    ReasoningGraphApplicationService,
    RegisterArtifactSpanEvidence,
    TransitionReasoningNode,
)
from riftx.domain import Artifact, Engagement, Objective, Run, RunKind
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
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyEvidenceLedgerRepository,
    SQLAlchemyReasoningGraphRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTaskGraphRepository,
)
from riftx.reasoning import (
    ReasoningCreatorType,
    ReasoningNode,
    ReasoningNodeKind,
    ReasoningNodeStatus,
    ReasoningRelationType,
    ReproductionContract,
)
from riftx.runtime.types import AgentSession
from riftx.tasks import Task, TaskGraph


@dataclass(frozen=True, slots=True)
class Harness:
    database: Database
    service: ReasoningGraphApplicationService
    graphs: SQLAlchemyReasoningGraphRepository
    runs: SQLAlchemyRunRepository
    sessions: SQLAlchemyAgentSessionRepository
    tasks: SQLAlchemyTaskGraphRepository
    evidence: SQLAlchemyEvidenceLedgerRepository


class _ArtifactSource:
    data = b"tcp/80 open http"
    artifact = SimpleNamespace(
        id="artifact-tool-output",
        run_id="run-1",
        audit_id=None,
        sha256="d" * 64,
    )

    async def read_content_slice(
        self,
        artifact_id: str,
        *,
        expected_run_id: str,
        offset: int,
        max_bytes: int,
    ) -> SimpleNamespace:
        assert artifact_id == self.artifact.id
        assert expected_run_id == self.artifact.run_id
        data = self.data[offset : offset + max_bytes]
        return SimpleNamespace(
            artifact=self.artifact,
            data=data,
            offset=offset,
            next_offset=offset + len(data),
        )

    async def read_audit_content_slice(self, *_: object, **__: object) -> SimpleNamespace:
        raise AssertionError("Public tool output must not use the Audit Artifact path")


def evidence(
    evidence_id: str,
    *,
    run_id: str = "run-1",
    kind: EvidenceKind = EvidenceKind.EXECUTION_OUTPUT,
    session_id: str | None = None,
    task_id: str | None = None,
) -> Evidence:
    locator = SourceLocator(uri=f"evidence://{evidence_id}")
    return Evidence(
        id=evidence_id,
        kind=kind,
        source_uri=locator.source_uri,
        digest="a" * 64,
        run_id=run_id,
        session_id=session_id,
        task_id=task_id,
        creator_type=EvidenceCreatorType.TOOL,
        created_by="test-tool",
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


def node(
    node_id: str,
    kind: ReasoningNodeKind,
    status: ReasoningNodeStatus,
    *,
    evidence_ids: tuple[str, ...] = (),
    session_id: str | None = None,
    task_id: str | None = None,
    reproduction_contract: ReproductionContract | None = None,
) -> ReasoningNode:
    return ReasoningNode(
        id=node_id,
        run_id="run-1",
        session_id=session_id,
        task_id=task_id,
        kind=kind,
        status=status,
        claim=f"Claim for {node_id}",
        evidence_ids=evidence_ids,
        reproduction_contract=reproduction_contract,
        creator_type=ReasoningCreatorType.AGENT,
        created_by="primary-agent",
    )


def reproduction_contract() -> ReproductionContract:
    return ReproductionContract(
        steps=("Send the recorded request", "Observe the recorded response"),
        expected_outcome="The target returns the vulnerable response",
        target_refs=("target://application",),
        parameters_digest="c" * 64,
    )


async def create_harness(tmp_path: Path) -> Harness:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reasoning-service.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Reasoning")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    for run_id in ("run-1", "run-2"):
        await runs.create(
            Run(
                kind=RunKind.GENERAL,
                id=run_id,
                engagement_id="engagement-1",
                node_id="node-1",
                objective=Objective(description=f"Reason about {run_id}"),
                workspace_path=str(tmp_path / run_id),
            )
        )
    await SQLAlchemyArtifactRepository(database.session_factory).create(
        Artifact(
            id=_ArtifactSource.artifact.id,
            run_id="run-1",
            name="stdout.log",
            path=str(tmp_path / "stdout.log"),
            mime_type="application/octet-stream",
            sha256=_ArtifactSource.artifact.sha256,
            size=len(_ArtifactSource.data),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(
        AgentSession(id="session-1", run_id="run-1", model_profile="test")
    )
    await sessions.create(
        AgentSession(id="session-2", run_id="run-1", model_profile="test")
    )
    await sessions.create(
        AgentSession(id="session-foreign", run_id="run-2", model_profile="test")
    )
    tasks = SQLAlchemyTaskGraphRepository(database.session_factory)
    await tasks.create(
        TaskGraph(
            run_id="run-1",
            tasks=[
                Task(id="task-1", run_id="run-1", sequence=1, title="First task"),
                Task(id="task-2", run_id="run-1", sequence=2, title="Second task"),
            ],
        )
    )
    await tasks.create(
        TaskGraph(
            run_id="run-2",
            tasks=[
                Task(id="task-foreign", run_id="run-2", sequence=1, title="Foreign task")
            ],
        )
    )
    ledger = SQLAlchemyEvidenceLedgerRepository(database.session_factory)
    for item in (
        evidence("direct"),
        evidence("direct-2"),
        evidence("external", kind=EvidenceKind.EXTERNAL_RESEARCH_SOURCE),
        evidence("session-2-evidence", session_id="session-2", task_id="task-1"),
        evidence("task-2-evidence", session_id="session-1", task_id="task-2"),
        evidence("foreign-run-evidence", run_id="run-2"),
    ):
        await ledger.create(item)
    graphs = SQLAlchemyReasoningGraphRepository(database.session_factory)
    return Harness(
        database=database,
        service=ReasoningGraphApplicationService(
            runs=runs,
            sessions=sessions,
            tasks=tasks,
            evidence=ledger,
            graphs=graphs,
        ),
        graphs=graphs,
        runs=runs,
        sessions=sessions,
        tasks=tasks,
        evidence=ledger,
    )


async def test_artifact_evidence_is_persisted_and_consumed_by_observation(
    tmp_path: Path,
) -> None:
    harness = await create_harness(tmp_path)
    source = _ArtifactSource()
    evidence_service = EvidenceApplicationService(
        runs=harness.runs,
        sessions=harness.sessions,
        tasks=harness.tasks,
        artifacts=source,  # type: ignore[arg-type]
        code=object(),  # type: ignore[arg-type]
        ledger=harness.evidence,
    )
    try:
        registered = await evidence_service.register_artifact_span(
            RegisterArtifactSpanEvidence(
                evidence_id="artifact-evidence",
                run_id="run-1",
                session_id="session-1",
                task_id="task-1",
                artifact_id=source.artifact.id,
                start_offset=0,
                end_offset=len(source.data),
                creator_type=EvidenceCreatorType.AGENT,
                created_by="primary",
                trust_class=EvidenceTrustClass.UNTRUSTED_TOOL_OUTPUT,
                redaction_status=EvidenceRedactionStatus.METADATA_ONLY,
            )
        )
        graph = await harness.service.create_node(
            node(
                "artifact-observation",
                ReasoningNodeKind.OBSERVATION,
                ReasoningNodeStatus.RECORDED,
                evidence_ids=(registered.id,),
                session_id="session-1",
                task_id="task-1",
            ),
            expected_graph_version=0,
        )

        restarted_ledger = SQLAlchemyEvidenceLedgerRepository(
            harness.database.session_factory
        )
        restarted_graphs = SQLAlchemyReasoningGraphRepository(
            harness.database.session_factory
        )
        assert await restarted_ledger.get(registered.id) == registered
        assert await restarted_graphs.get("run-1") == graph
        assert graph.nodes[0].evidence_ids == (registered.id,)
    finally:
        await harness.database.dispose()


async def test_create_core_reasoning_nodes_and_keep_empty_hypothesis_unverified(
    tmp_path: Path,
) -> None:
    harness = await create_harness(tmp_path)
    try:
        graph = await harness.service.create_node(
            node(
                "observation",
                ReasoningNodeKind.OBSERVATION,
                ReasoningNodeStatus.RECORDED,
                evidence_ids=("direct",),
            ),
            expected_graph_version=0,
        )
        graph = await harness.service.create_node(
            node(
                "fact-candidate",
                ReasoningNodeKind.FACT_CANDIDATE,
                ReasoningNodeStatus.CANDIDATE,
                evidence_ids=("direct",),
            ),
            expected_graph_version=graph.version,
        )
        graph = await harness.service.create_node(
            node(
                "hypothesis",
                ReasoningNodeKind.HYPOTHESIS,
                ReasoningNodeStatus.UNVERIFIED,
            ),
            expected_graph_version=graph.version,
        )

        assert graph.version == 3
        assert [item.kind for item in graph.nodes] == [
            ReasoningNodeKind.OBSERVATION,
            ReasoningNodeKind.FACT_CANDIDATE,
            ReasoningNodeKind.HYPOTHESIS,
        ]
        with pytest.raises(ValueError, match="Evidence-free Hypothesis"):
            await harness.service.create_node(
                node(
                    "unsupported-hypothesis",
                    ReasoningNodeKind.HYPOTHESIS,
                    ReasoningNodeStatus.INVESTIGATING,
                ),
                expected_graph_version=graph.version,
            )
    finally:
        await harness.database.dispose()


async def test_query_reasoning_graph_is_filtered_and_bounded(tmp_path: Path) -> None:
    harness = await create_harness(tmp_path)
    try:
        graph = await harness.service.create_node(
            node(
                "observation-1",
                ReasoningNodeKind.OBSERVATION,
                ReasoningNodeStatus.RECORDED,
                evidence_ids=("direct",),
                task_id="task-1",
            ).model_copy(update={"claim": "Admin endpoint returned HTTP 200"}),
            expected_graph_version=0,
        )
        await harness.service.create_node(
            node(
                "observation-2",
                ReasoningNodeKind.OBSERVATION,
                ReasoningNodeStatus.RECORDED,
                evidence_ids=("direct-2",),
                task_id="task-2",
            ).model_copy(update={"claim": "Health endpoint returned HTTP 200"}),
            expected_graph_version=graph.version,
        )

        result = await harness.service.query(
            QueryReasoningGraph(
                run_id="run-1",
                kinds=(ReasoningNodeKind.OBSERVATION,),
                task_id="task-1",
                query="admin",
                limit=1,
            )
        )
        assert result.graph_version == 2
        assert result.total_matching_nodes == 1
        assert [item.id for item in result.nodes] == ["observation-1"]

        empty = await harness.service.query(QueryReasoningGraph(run_id="run-2"))
        assert empty.graph_version == 0
        assert empty.nodes == ()
    finally:
        await harness.database.dispose()


async def test_fact_promotion_is_atomic_and_version_guarded(tmp_path: Path) -> None:
    harness = await create_harness(tmp_path)
    try:
        graph = await harness.service.create_node(
            node(
                "fact-candidate",
                ReasoningNodeKind.FACT_CANDIDATE,
                ReasoningNodeStatus.CANDIDATE,
                evidence_ids=("direct",),
            ),
            expected_graph_version=0,
        )
        graph = await harness.service.promote_fact(
            run_id="run-1",
            candidate_id="fact-candidate",
            confirmed_fact=node(
                "confirmed-fact",
                ReasoningNodeKind.CONFIRMED_FACT,
                ReasoningNodeStatus.CONFIRMED,
                evidence_ids=("direct",),
            ),
            expected_graph_version=graph.version,
            expected_candidate_version=1,
            edge_id="fact-lineage",
        )

        candidate = next(item for item in graph.nodes if item.id == "fact-candidate")
        assert candidate.status is ReasoningNodeStatus.PROMOTED
        assert candidate.version == 2
        assert graph.edges[0].relation_type is ReasoningRelationType.DERIVED_FROM
        with pytest.raises(ApplicationConflictError) as stale_node:
            await harness.service.promote_fact(
                run_id="run-1",
                candidate_id="fact-candidate",
                confirmed_fact=node(
                    "another-fact",
                    ReasoningNodeKind.CONFIRMED_FACT,
                    ReasoningNodeStatus.CONFIRMED,
                    evidence_ids=("direct",),
                ),
                expected_graph_version=graph.version,
                expected_candidate_version=1,
            )
        assert stale_node.value.code == "reasoning_node_version_conflict"
        with pytest.raises(ApplicationConflictError) as stale_graph:
            await harness.service.create_node(
                node(
                    "observation",
                    ReasoningNodeKind.OBSERVATION,
                    ReasoningNodeStatus.RECORDED,
                    evidence_ids=("direct",),
                ),
                expected_graph_version=1,
            )
        assert stale_graph.value.code == "reasoning_graph_version_conflict"
    finally:
        await harness.database.dispose()


async def test_finding_requires_direct_evidence_and_reproduction_then_accepts_proof_and_negative(
    tmp_path: Path,
) -> None:
    harness = await create_harness(tmp_path)
    try:
        graph = await harness.service.create_node(
            node(
                "vulnerability-candidate",
                ReasoningNodeKind.VULNERABILITY_CANDIDATE,
                ReasoningNodeStatus.CANDIDATE,
                evidence_ids=("external",),
            ),
            expected_graph_version=0,
        )
        graph = await harness.service.promote_vulnerability(
            run_id="run-1",
            candidate_id="vulnerability-candidate",
            finding=node(
                "finding",
                ReasoningNodeKind.FINDING,
                ReasoningNodeStatus.CANDIDATE,
                evidence_ids=("external",),
            ),
            expected_graph_version=graph.version,
            expected_candidate_version=1,
            edge_id="finding-lineage",
        )

        with pytest.raises(ApplicationConflictError) as external_only:
            await harness.service.transition_node(
                TransitionReasoningNode(
                    run_id="run-1",
                    node_id="finding",
                    expected_graph_version=graph.version,
                    expected_node_version=1,
                    target_status=ReasoningNodeStatus.CONFIRMED,
                    evidence_ids=("external",),
                    reproduction_contract=reproduction_contract(),
                )
            )
        assert external_only.value.code == "reasoning_finding_direct_evidence_required"
        with pytest.raises(ValueError, match="Confirmed Finding requires Evidence"):
            await harness.service.transition_node(
                TransitionReasoningNode(
                    run_id="run-1",
                    node_id="finding",
                    expected_graph_version=graph.version,
                    expected_node_version=1,
                    target_status=ReasoningNodeStatus.CONFIRMED,
                    evidence_ids=("external", "direct"),
                )
            )

        graph = await harness.service.transition_node(
            TransitionReasoningNode(
                run_id="run-1",
                node_id="finding",
                expected_graph_version=graph.version,
                expected_node_version=1,
                target_status=ReasoningNodeStatus.CONFIRMED,
                evidence_ids=("external", "direct"),
                reproduction_contract=reproduction_contract(),
            )
        )
        graph = await harness.service.record_proof(
            node(
                "proof",
                ReasoningNodeKind.PROOF,
                ReasoningNodeStatus.VALIDATED,
                evidence_ids=("direct",),
            ),
            finding_id="finding",
            expected_graph_version=graph.version,
            edge_id="proof-validates",
        )
        graph = await harness.service.record_negative_result(
            node(
                "negative-result",
                ReasoningNodeKind.NEGATIVE_RESULT,
                ReasoningNodeStatus.RECORDED,
                evidence_ids=("direct-2",),
            ),
            invalidated_node_id="finding",
            expected_graph_version=graph.version,
            edge_id="negative-invalidates",
        )

        assert any(
            edge.source_node_id == "proof"
            and edge.target_node_id == "finding"
            and edge.relation_type is ReasoningRelationType.VALIDATES
            for edge in graph.edges
        )
        assert any(
            edge.source_node_id == "negative-result"
            and edge.target_node_id == "finding"
            and edge.relation_type is ReasoningRelationType.INVALIDATES
            for edge in graph.edges
        )
        restarted = SQLAlchemyReasoningGraphRepository(harness.database.session_factory)
        assert await restarted.get("run-1") == graph
    finally:
        await harness.database.dispose()


@pytest.mark.parametrize(
    ("evidence_id", "session_id", "task_id", "expected_code"),
    [
        (
            "foreign-run-evidence",
            "session-1",
            "task-1",
            "reasoning_evidence_missing",
        ),
        (
            "session-2-evidence",
            "session-1",
            "task-1",
            "reasoning_evidence_session_mismatch",
        ),
        (
            "task-2-evidence",
            "session-1",
            "task-1",
            "reasoning_evidence_task_mismatch",
        ),
        (
            "direct",
            "session-foreign",
            "task-1",
            "reasoning_session_owner_mismatch",
        ),
        (
            "direct",
            "session-1",
            "task-foreign",
            "reasoning_task_owner_mismatch",
        ),
    ],
)
async def test_rejects_cross_owner_evidence_session_and_task(
    tmp_path: Path,
    evidence_id: str,
    session_id: str,
    task_id: str,
    expected_code: str,
) -> None:
    harness = await create_harness(tmp_path)
    try:
        with pytest.raises(ApplicationConflictError) as raised:
            await harness.service.create_node(
                node(
                    "observation",
                    ReasoningNodeKind.OBSERVATION,
                    ReasoningNodeStatus.RECORDED,
                    evidence_ids=(evidence_id,),
                    session_id=session_id,
                    task_id=task_id,
                ),
                expected_graph_version=0,
            )
        assert raised.value.code == expected_code
        assert await harness.graphs.get("run-1") is None
    finally:
        await harness.database.dispose()
