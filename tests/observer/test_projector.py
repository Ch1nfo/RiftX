from __future__ import annotations

from riftx.application.graphs import (
    GraphEdge,
    GraphNode,
    GraphScope,
    GraphSnapshot,
    GraphViewKind,
    GraphViewPage,
)
from riftx.application.services.reports import ReportSource
from riftx.domain import LocalPrincipal, OperatorCapability, RunEvent
from riftx.observer import ObserverProjectorApplicationService


def node(
    node_id: str,
    node_type: str,
    *,
    provenance: tuple[str, ...],
) -> GraphNode:
    return GraphNode(
        id=node_id,
        type=node_type,
        domain_id=node_id.rsplit(":", 1)[-1],
        label=node_type.replace("_", " ").title(),
        status="recorded",
        provenance_refs=provenance,
        projection_quality="exact",
        partial_reasons=(),
    )


def page(
    view: GraphViewKind,
    *,
    nodes: tuple[GraphNode, ...] = (),
    edges: tuple[GraphEdge, ...] = (),
    has_more: bool = False,
) -> GraphViewPage:
    signature = "a" * 64
    return GraphViewPage(
        scope=GraphScope(run_id="run-1", engagement_id="engagement-1"),
        view=view,
        snapshot=GraphSnapshot(id=signature, topology_signature=signature),
        snapshot_id=signature,
        projection_sources=(f"{view.value}_source",),
        nodes=nodes,
        edges=edges,
        type_metadata=(),
        partial_reasons=(),
        truncated=False,
        has_more=has_more,
        next_cursor="cursor" if has_more else None,
    )


class GraphViews:
    def __init__(self) -> None:
        reasoning_a = node(
            "fact:run-1:reasoning-a",
            "fact",
            provenance=("reasoning_nodes",),
        )
        reasoning_b = node(
            "proof:run-1:reasoning-b",
            "proof",
            provenance=("reasoning_nodes",),
        )
        attack_a = node(
            "engagement_fact:engagement-1:attack-a",
            "engagement_fact",
            provenance=("engagement_facts",),
        )
        attack_b = node(
            "engagement_fact:engagement-1:attack-b",
            "engagement_fact",
            provenance=("engagement_facts",),
        )
        evidence_nodes = (reasoning_a, reasoning_b, attack_a, attack_b)
        evidence_edges = (
            GraphEdge(
                id="reasoning_edge:run-1:edge-a",
                type="supports",
                source=reasoning_a.id,
                target=reasoning_b.id,
                provenance_refs=("reasoning_edges",),
                projection_quality="exact",
            ),
            GraphEdge(
                id="fact_relation:engagement-1:edge-b",
                type="depends_on",
                source=attack_a.id,
                target=attack_b.id,
                provenance_refs=("fact_relations",),
                projection_quality="exact",
            ),
        )
        self.pages = {
            GraphViewKind.TASK: page(
                GraphViewKind.TASK,
                nodes=(
                    node(
                        "plan_item:run-1:task-a",
                        "plan_item",
                        provenance=("task_graph",),
                    ),
                ),
            ),
            GraphViewKind.EVIDENCE: page(
                GraphViewKind.EVIDENCE,
                nodes=evidence_nodes,
                edges=evidence_edges,
            ),
            GraphViewKind.OPERATION: page(GraphViewKind.OPERATION),
        }
        self.calls: list[GraphViewKind] = []

    async def get_view(
        self,
        run_id: str,
        *,
        principal: LocalPrincipal,
        view: GraphViewKind,
        limit: int = 100,
    ) -> GraphViewPage:
        assert run_id == "run-1"
        assert principal.id == "operator"
        assert limit == 25
        self.calls.append(view)
        return self.pages[view]


class Events:
    async def list_recent(self, run_id: str, *, limit: int = 100):
        assert run_id == "run-1"
        assert limit == 10
        return [
            RunEvent(
                run_id=run_id,
                sequence=1,
                event_type="runtime.engine_event",
                payload={"arguments": {"secret": "must-not-project"}},
            )
        ]


class ReportDrafts:
    async def build_source(self, run: str) -> ReportSource:
        assert run == "run-1"
        return ReportSource(
            run_id=run,
            objective="Authorized projection",
            scope={},
            run_status="running",
            run_summary="Projection is in progress",
        )


async def test_projector_reuses_bounded_views_and_marks_missing_code_graph_partial() -> None:
    graphs = GraphViews()
    service = ObserverProjectorApplicationService(
        graph_views=graphs,
        events=Events(),  # type: ignore[arg-type]
        report_drafts=ReportDrafts(),
    )

    projection = await service.project(
        "run-1",
        principal=LocalPrincipal(
            id="operator",
            capabilities=frozenset({OperatorCapability.READ}),
        ),
        graph_limit=25,
        timeline_limit=10,
    )

    assert graphs.calls[0] is GraphViewKind.TASK
    assert set(graphs.calls[1:]) == {GraphViewKind.EVIDENCE, GraphViewKind.OPERATION}
    assert [item.domain_id for item in projection.reasoning_graph.nodes] == [
        "reasoning-a",
        "reasoning-b",
    ]
    assert len(projection.reasoning_graph.edges) == 1
    assert [item.domain_id for item in projection.attack_graph.nodes] == [
        "attack-a",
        "attack-b",
    ]
    assert len(projection.attack_graph.edges) == 1
    assert projection.code_graph.partial is True
    assert projection.code_graph.partial_reasons == (
        "code_graph_authoritative_source_unavailable",
    )
    assert projection.coverage.metrics["task_nodes"] == 1
    assert projection.coverage.metrics["reasoning_nodes"] == 2
    assert projection.coverage.metrics["attack_nodes"] == 2
    assert projection.timeline[0].event_type == "runtime.engine_event"
    assert "must-not-project" not in projection.model_dump_json()
    assert projection.report_draft.source.run_status == "running"
