"""Bounded, read-only Observer projections over existing authoritative views."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from typing import Protocol

from pydantic import AwareDatetime, Field

from riftx.application.graphs import (
    GraphEdge,
    GraphNode,
    GraphViewKind,
    GraphViewPage,
)
from riftx.application.ports import RunEventRepository
from riftx.application.services.reports import (
    DeterministicReportComposer,
    ReportComposer,
    ReportSource,
    StructuredReport,
)
from riftx.domain import LocalPrincipal
from riftx.domain.base import DomainModel

_REASONING_NODE_TYPES = frozenset(
    {
        "observation",
        "fact_candidate",
        "fact",
        "hypothesis",
        "vulnerability_candidate",
        "finding",
        "proof",
        "negative_result",
    }
)
_ATTACK_NODE_TYPES = frozenset({"engagement_fact"})


class GraphViewReader(Protocol):
    async def get_view(
        self,
        run_id: str,
        *,
        principal: LocalPrincipal,
        view: GraphViewKind,
        limit: int = 100,
    ) -> GraphViewPage: ...


class ReportDraftReader(Protocol):
    async def build_source(self, run: str) -> ReportSource: ...


class ProjectedGraph(DomainModel):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_view: GraphViewKind | None = None
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    partial: bool = False
    partial_reasons: tuple[str, ...] = ()


class ProjectionCoverage(DomainModel):
    metrics: dict[str, int] = Field(default_factory=dict)
    graph_page_limit: int = Field(ge=1, le=100)
    timeline_limit: int = Field(ge=1, le=1_000)
    partial: bool = False
    partial_reasons: tuple[str, ...] = ()


class TimelineEntry(DomainModel):
    event_id: str
    sequence: int = Field(ge=1)
    event_type: str
    created_at: AwareDatetime


class ObserverProjection(DomainModel):
    run_id: str
    task_graph: GraphViewPage
    reasoning_graph: ProjectedGraph
    evidence_graph: GraphViewPage
    attack_graph: ProjectedGraph
    code_graph: ProjectedGraph
    operation_graph: GraphViewPage
    coverage: ProjectionCoverage
    timeline: tuple[TimelineEntry, ...] = ()
    report_draft: StructuredReport
    partial_reasons: tuple[str, ...] = ()


class ObserverProjectorApplicationService:
    def __init__(
        self,
        *,
        graph_views: GraphViewReader,
        events: RunEventRepository,
        report_drafts: ReportDraftReader,
        report_composer: ReportComposer | None = None,
    ) -> None:
        self._graph_views = graph_views
        self._events = events
        self._report_drafts = report_drafts
        self._report_composer = report_composer or DeterministicReportComposer()

    async def project(
        self,
        run_id: str,
        *,
        principal: LocalPrincipal,
        graph_limit: int = 100,
        timeline_limit: int = 100,
    ) -> ObserverProjection:
        if graph_limit < 1 or graph_limit > 100:
            raise ValueError("graph limit must be between 1 and 100")
        if timeline_limit < 1 or timeline_limit > 1_000:
            raise ValueError("timeline limit must be between 1 and 1000")

        # The first authorized Graph read is the access gate before report data
        # or other projections are materialized.
        task_graph = await self._graph_views.get_view(
            run_id,
            principal=principal,
            view=GraphViewKind.TASK,
            limit=graph_limit,
        )
        evidence_read = self._graph_views.get_view(
            run_id,
            principal=principal,
            view=GraphViewKind.EVIDENCE,
            limit=graph_limit,
        )
        operation_read = self._graph_views.get_view(
            run_id,
            principal=principal,
            view=GraphViewKind.OPERATION,
            limit=graph_limit,
        )
        evidence_graph, operation_graph, events, report_source = await asyncio.gather(
            evidence_read,
            operation_read,
            self._events.list_recent(run_id, limit=timeline_limit),
            self._report_drafts.build_source(run_id),
        )
        report_draft = await self._report_composer.compose(report_source)
        reasoning_graph = _slice_graph(
            evidence_graph,
            kind="reasoning",
            node_types=_REASONING_NODE_TYPES,
            required_provenance="reasoning_nodes",
        )
        attack_graph = _slice_graph(
            evidence_graph,
            kind="attack",
            node_types=_ATTACK_NODE_TYPES,
            empty_reason="attack_graph_authoritative_source_unavailable",
        )
        code_graph = ProjectedGraph(
            kind="code",
            partial=True,
            partial_reasons=("code_graph_authoritative_source_unavailable",),
        )
        timeline = tuple(
            TimelineEntry(
                event_id=event.id,
                sequence=event.sequence,
                event_type=event.event_type,
                created_at=event.created_at,
            )
            for event in events
        )
        partial_reasons = {
            *task_graph.partial_reasons,
            *evidence_graph.partial_reasons,
            *operation_graph.partial_reasons,
            *reasoning_graph.partial_reasons,
            *attack_graph.partial_reasons,
            *code_graph.partial_reasons,
        }
        if task_graph.has_more or evidence_graph.has_more or operation_graph.has_more:
            partial_reasons.add("graph_page_limit_reached")
        if len(events) == timeline_limit:
            partial_reasons.add("timeline_limit_reached")
        coverage = _coverage(
            task_graph,
            reasoning_graph,
            evidence_graph,
            attack_graph,
            code_graph,
            operation_graph,
            timeline_count=len(timeline),
            report_finding_count=len(report_draft.source.findings),
            graph_limit=graph_limit,
            timeline_limit=timeline_limit,
            partial_reasons=partial_reasons,
        )
        return ObserverProjection(
            run_id=run_id,
            task_graph=task_graph,
            reasoning_graph=reasoning_graph,
            evidence_graph=evidence_graph,
            attack_graph=attack_graph,
            code_graph=code_graph,
            operation_graph=operation_graph,
            coverage=coverage,
            timeline=timeline,
            report_draft=report_draft,
            partial_reasons=tuple(sorted(partial_reasons)),
        )


def _slice_graph(
    source: GraphViewPage,
    *,
    kind: str,
    node_types: frozenset[str],
    required_provenance: str | None = None,
    empty_reason: str | None = None,
) -> ProjectedGraph:
    nodes = tuple(
        node
        for node in source.nodes
        if node.type in node_types
        and (
            required_provenance is None
            or required_provenance in node.provenance_refs
        )
    )
    node_ids = {node.id for node in nodes}
    edges = tuple(
        edge
        for edge in source.edges
        if edge.source in node_ids and edge.target in node_ids
    )
    reasons = set(source.partial_reasons)
    if source.has_more:
        reasons.add("source_graph_page_incomplete")
    if not nodes and empty_reason is not None:
        reasons.add(empty_reason)
    return ProjectedGraph(
        kind=kind,
        source_view=source.view,
        nodes=nodes,
        edges=edges,
        partial=bool(reasons),
        partial_reasons=tuple(sorted(reasons)),
    )


def _coverage(
    task: GraphViewPage,
    reasoning: ProjectedGraph,
    evidence: GraphViewPage,
    attack: ProjectedGraph,
    code: ProjectedGraph,
    operation: GraphViewPage,
    *,
    timeline_count: int,
    report_finding_count: int,
    graph_limit: int,
    timeline_limit: int,
    partial_reasons: Collection[str],
) -> ProjectionCoverage:
    reasons = tuple(sorted(set(partial_reasons)))
    return ProjectionCoverage(
        metrics={
            "task_nodes": len(task.nodes),
            "task_edges": len(task.edges),
            "reasoning_nodes": len(reasoning.nodes),
            "reasoning_edges": len(reasoning.edges),
            "evidence_nodes": len(evidence.nodes),
            "evidence_edges": len(evidence.edges),
            "attack_nodes": len(attack.nodes),
            "attack_edges": len(attack.edges),
            "code_nodes": len(code.nodes),
            "code_edges": len(code.edges),
            "operation_nodes": len(operation.nodes),
            "operation_edges": len(operation.edges),
            "timeline_events": timeline_count,
            "report_findings": report_finding_count,
        },
        graph_page_limit=graph_limit,
        timeline_limit=timeline_limit,
        partial=bool(reasons),
        partial_reasons=reasons,
    )
