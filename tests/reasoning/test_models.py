from __future__ import annotations

import pytest
from pydantic import ValidationError

from riftx.reasoning import (
    ReasoningCreatorType,
    ReasoningEdge,
    ReasoningGraph,
    ReasoningNode,
    ReasoningNodeKind,
    ReasoningNodeStatus,
    ReasoningRelationType,
    ReproductionContract,
)


def node(
    node_id: str,
    kind: ReasoningNodeKind,
    status: ReasoningNodeStatus,
    *,
    evidence_ids: tuple[str, ...] = ("evidence-1",),
    reproduction_contract: ReproductionContract | None = None,
) -> ReasoningNode:
    return ReasoningNode(
        id=node_id,
        run_id="run-1",
        kind=kind,
        status=status,
        claim=node_id,
        evidence_ids=evidence_ids,
        reproduction_contract=reproduction_contract,
        creator_type=ReasoningCreatorType.REDUCER,
        created_by="reasoning-reducer",
    )


def edge(
    edge_id: str,
    source: str,
    target: str,
    relation_type: ReasoningRelationType,
) -> ReasoningEdge:
    return ReasoningEdge(
        id=edge_id,
        run_id="run-1",
        source_node_id=source,
        target_node_id=target,
        relation_type=relation_type,
        creator_type=ReasoningCreatorType.REDUCER,
        created_by="reasoning-reducer",
    )


def test_confirmed_fact_preserves_candidate_promotion_lineage() -> None:
    candidate = node(
        "candidate-1",
        ReasoningNodeKind.FACT_CANDIDATE,
        ReasoningNodeStatus.PROMOTED,
    )
    confirmed = node(
        "fact-1",
        ReasoningNodeKind.CONFIRMED_FACT,
        ReasoningNodeStatus.CONFIRMED,
    )

    graph = ReasoningGraph(
        run_id="run-1",
        nodes=[candidate, confirmed],
        edges=[
            edge(
                "derived-1",
                candidate.id,
                confirmed.id,
                ReasoningRelationType.DERIVED_FROM,
            )
        ],
    )

    assert graph.nodes == [candidate, confirmed]


def test_evidence_free_hypothesis_must_remain_unverified() -> None:
    hypothesis = node(
        "hypothesis-1",
        ReasoningNodeKind.HYPOTHESIS,
        ReasoningNodeStatus.INVESTIGATING,
        evidence_ids=(),
    )

    with pytest.raises(ValidationError, match="must remain unverified"):
        ReasoningGraph(run_id="run-1", nodes=[hypothesis])

    accepted = hypothesis.model_copy(update={"status": ReasoningNodeStatus.UNVERIFIED})
    assert ReasoningGraph(run_id="run-1", nodes=[accepted]).nodes == [accepted]


def test_confirmed_finding_requires_evidence_and_reproduction_contract() -> None:
    with pytest.raises(ValidationError, match="Confirmed Finding"):
        node(
            "finding-1",
            ReasoningNodeKind.FINDING,
            ReasoningNodeStatus.CONFIRMED,
            evidence_ids=(),
        )

    contract = ReproductionContract(
        steps=("Send the authorized probe",),
        expected_outcome="The target returns the vulnerable behavior",
        target_refs=("target://service/example",),
        parameters_digest="a" * 64,
    )
    finding = node(
        "finding-1",
        ReasoningNodeKind.FINDING,
        ReasoningNodeStatus.CONFIRMED,
        reproduction_contract=contract,
    )
    assert finding.reproduction_contract == contract


def test_negative_result_must_invalidate_a_graph_claim() -> None:
    hypothesis = node(
        "hypothesis-1",
        ReasoningNodeKind.HYPOTHESIS,
        ReasoningNodeStatus.UNVERIFIED,
        evidence_ids=(),
    )
    negative = node(
        "negative-1",
        ReasoningNodeKind.NEGATIVE_RESULT,
        ReasoningNodeStatus.RECORDED,
    )
    with pytest.raises(ValidationError, match="must invalidate or contradict"):
        ReasoningGraph(run_id="run-1", nodes=[hypothesis, negative])

    graph = ReasoningGraph(
        run_id="run-1",
        nodes=[hypothesis, negative],
        edges=[
            edge(
                "invalidates-1",
                negative.id,
                hypothesis.id,
                ReasoningRelationType.INVALIDATES,
            )
        ],
    )
    assert graph.edges[0].relation_type is ReasoningRelationType.INVALIDATES
