"""Authorized deterministic projection of safe Run graph views."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from riftx.application.errors import ResourceNotAccessibleError
from riftx.application.graphs import (
    GraphActionSource,
    GraphArtifactSource,
    GraphEdge,
    GraphEngagementFactSource,
    GraphEvidenceRefSource,
    GraphExecutionHostSource,
    GraphExecutionSource,
    GraphFactRelationSource,
    GraphFactSource,
    GraphFilter,
    GraphFindingSource,
    GraphHypothesisSource,
    GraphNode,
    GraphPlanItemSource,
    GraphRunSource,
    GraphScope,
    GraphSessionSource,
    GraphSnapshot,
    GraphSourceContractError,
    GraphSourceCoverage,
    GraphSourceSnapshot,
    GraphTypeMetadata,
    GraphUserDecisionSource,
    GraphViewKind,
    GraphViewPage,
    InvalidGraphCursorError,
    StaleGraphCursorError,
)
from riftx.domain import LocalPrincipal, OperatorCapability

_MAX_LIMIT = 100
_MAX_FILTER_LENGTH = 512
_MAX_SEARCH_LENGTH = 512
_MAX_SOURCE_ITEMS = 10_000
_CURSOR_DOMAIN = b"riftx-graph-cursor-v1\0"
_PRINCIPAL_DOMAIN = b"riftx-graph-principal-v1\0"
_TOPOLOGY_DOMAIN = b"riftx-graph-topology-v1\0"
_DOMAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+~-]{0,127}")
_TYPE_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")

_TASK_PROJECTION_BASE_SOURCES = (
    "tool_call_intents",
    "findings",
    "executions",
    "artifacts",
)
_EVIDENCE_PROJECTION_SOURCES = (
    "run_facts",
    "engagement_facts",
    "fact_relations",
    "findings",
    "artifacts",
    "executions",
    "user_decisions",
)
_OPERATION_PROJECTION_SOURCES = (
    "executions",
    "runtime_sessions",
    "execution_hosts",
)

_TASK_TYPE_METADATA = (
    GraphTypeMetadata(kind="node", type="plan_item", label="Plan item", color="#2563eb"),
    GraphTypeMetadata(kind="node", type="action", label="Action", color="#7c3aed"),
    GraphTypeMetadata(kind="node", type="finding", label="Finding", color="#dc2626"),
    GraphTypeMetadata(kind="node", type="execution", label="Execution", color="#475569"),
    GraphTypeMetadata(kind="node", type="artifact", label="Artifact", color="#0284c7"),
    GraphTypeMetadata(
        kind="node",
        type="unassigned_actions",
        label="Unassigned actions",
        color="#64748b",
    ),
    GraphTypeMetadata(
        kind="node",
        type="unassigned_findings",
        label="Unassigned findings",
        color="#78716c",
    ),
    GraphTypeMetadata(kind="edge", type="unassigned", label="Unassigned", color="#94a3b8"),
    GraphTypeMetadata(kind="edge", type="depends_on", label="Depends on", color="#64748b"),
    GraphTypeMetadata(kind="edge", type="executed_as", label="Executed as", color="#7c3aed"),
    GraphTypeMetadata(kind="edge", type="produced", label="Produced", color="#0284c7"),
    GraphTypeMetadata(kind="edge", type="supports", label="Supports", color="#16a34a"),
)

_EVIDENCE_TYPE_METADATA = (
    GraphTypeMetadata(kind="node", type="fact", label="Fact", color="#0f766e"),
    GraphTypeMetadata(kind="node", type="hypothesis", label="Hypothesis", color="#9333ea"),
    GraphTypeMetadata(kind="node", type="finding", label="Finding", color="#dc2626"),
    GraphTypeMetadata(kind="node", type="artifact", label="Artifact", color="#0284c7"),
    GraphTypeMetadata(kind="node", type="execution", label="Execution", color="#475569"),
    GraphTypeMetadata(
        kind="node",
        type="user_decision",
        label="User decision",
        color="#4f46e5",
    ),
    GraphTypeMetadata(
        kind="node",
        type="engagement_fact",
        label="Engagement fact",
        color="#115e59",
    ),
    GraphTypeMetadata(kind="edge", type="supports", label="Supports", color="#16a34a"),
    GraphTypeMetadata(kind="edge", type="contradicts", label="Contradicts", color="#dc2626"),
    GraphTypeMetadata(kind="edge", type="produced", label="Produced", color="#0284c7"),
    GraphTypeMetadata(
        kind="edge",
        type="discovered_on",
        label="Discovered on",
        color="#0d9488",
    ),
    GraphTypeMetadata(kind="edge", type="exploits", label="Exploits", color="#e11d48"),
    GraphTypeMetadata(kind="edge", type="enables", label="Enables", color="#ca8a04"),
    GraphTypeMetadata(kind="edge", type="depends_on", label="Depends on", color="#64748b"),
    GraphTypeMetadata(kind="edge", type="leads_to", label="Leads to", color="#ea580c"),
)

_OPERATION_TYPE_METADATA = (
    GraphTypeMetadata(kind="node", type="execution", label="Execution", color="#475569"),
    GraphTypeMetadata(kind="node", type="session", label="Session", color="#7c3aed"),
    GraphTypeMetadata(
        kind="node",
        type="execution_host",
        label="RiftX execution host",
        color="#0369a1",
    ),
    GraphTypeMetadata(kind="edge", type="contains", label="Contains", color="#7c3aed"),
    GraphTypeMetadata(kind="edge", type="parent_of", label="Parent of", color="#6d28d9"),
    GraphTypeMetadata(kind="edge", type="ran_on", label="Ran on", color="#0369a1"),
)

_SAFE_ACTION_STATUSES = frozenset(
    {
        "proposed",
        "awaiting_approval",
        "ready",
        "executing",
        "completed",
        "succeeded",
        "failed",
        "cancelled",
        "partial",
    }
)
_SAFE_PLAN_STATUSES = frozenset(
    {"pending", "running", "blocked", "completed", "failed", "cancelled"}
)
_SAFE_FINDING_STATUSES = frozenset({"draft", "confirmed", "resolved", "false_positive"})
_SAFE_FACT_STATUSES = frozenset({"confirmed", "disputed", "superseded"})
_SAFE_HYPOTHESIS_STATUSES = frozenset(
    {"proposed", "investigating", "supported", "confirmed", "rejected", "stale"}
)
_SAFE_EXECUTION_STATUSES = frozenset(
    {
        "created",
        "queued",
        "starting",
        "running",
        "completed",
        "exited",
        "failed",
        "cancelled",
        "hard_timeout",
        "lost",
    }
)
_SAFE_SESSION_STATUSES = frozenset({"created", "active", "open", "closed", "lost"})
_SAFE_HOST_STATUSES = frozenset({"online", "offline", "degraded", "lost", "unknown"})
_SAFE_RELATION_TYPES = frozenset({"discovered_on", "exploits", "enables", "depends_on", "leads_to"})
_COVERAGE_SOURCES = frozenset(
    {
        "plan_items",
        "task_dependencies",
        "actions",
        "facts",
        "hypotheses",
        "user_decisions",
        "engagement_facts",
        "fact_relations",
        "findings",
        "artifacts",
        "executions",
        "sessions",
        "execution_hosts",
    }
)


class _GraphSnapshotRepository(Protocol):
    async def resolve_scope(self, run_id: str) -> GraphScope | None: ...

    async def load(
        self,
        scope: GraphScope,
        view: GraphViewKind,
    ) -> GraphSourceSnapshot: ...


class _ObjectAuthorizer(Protocol):
    def require_run_engagement(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        parent_engagement_id: str,
        resource_engagement_id: str | None,
        capability: OperatorCapability,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _Projection:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    projection_sources: tuple[str, ...]
    type_metadata: tuple[GraphTypeMetadata, ...]
    partial_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _EvidenceIndex:
    artifacts: dict[str, str]
    executions: dict[str, str]
    user_decisions: dict[str, str]


@dataclass(frozen=True, slots=True)
class _PageUnit:
    key: tuple[str, str]
    nodes: tuple[GraphNode, ...]
    edge: GraphEdge | None


class GraphApplicationService:
    """Build read-only projections without creating a second graph truth."""

    def __init__(
        self,
        repository: _GraphSnapshotRepository,
        *,
        authorizer: _ObjectAuthorizer,
        cursor_signing_key: bytes,
    ) -> None:
        if type(cursor_signing_key) is not bytes or len(cursor_signing_key) < 32:
            raise ValueError("Graph cursor signing key must contain at least 32 bytes")
        self._repository = repository
        self._authorizer = authorizer
        self._cursor_signing_key = cursor_signing_key

    async def get_view(
        self,
        run_id: str,
        *,
        principal: LocalPrincipal,
        view: GraphViewKind,
        limit: int = 100,
        cursor: str | None = None,
        node_type: str | None = None,
        edge_type: str | None = None,
        focus: str | None = None,
        search: str | None = None,
    ) -> GraphViewPage:
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            raise ValueError(f"Graph limit must be between 1 and {_MAX_LIMIT}")
        try:
            selected_view = GraphViewKind(view)
        except (TypeError, ValueError):
            raise ValueError("Unknown Graph view") from None
        graph_filter = _normalize_filter(node_type, edge_type, focus, search)

        scope = await self._repository.resolve_scope(run_id)
        if scope is None:
            raise _resource_not_accessible()
        if scope.run_id != run_id or not scope.engagement_id:
            raise GraphSourceContractError("Graph source contract is invalid")
        self._authorizer.require_run_engagement(
            principal,
            parent_run_id=run_id,
            resource_run_id=scope.run_id,
            parent_engagement_id=scope.engagement_id,
            resource_engagement_id=scope.engagement_id,
            capability=OperatorCapability.READ,
        )

        source = await self._repository.load(scope, selected_view)
        _validate_source(source, scope)
        projection = _project(source, selected_view)
        topology_signature = _topology_signature(
            selected_view,
            projection.nodes,
            projection.edges,
        )
        snapshot_id = _snapshot_signature(
            selected_view,
            topology_signature,
            source.coverage,
        )
        if graph_filter.focus is not None and not any(
            node.id == graph_filter.focus for node in projection.nodes
        ):
            raise _resource_not_accessible()
        nodes, edges = _apply_filter(projection.nodes, projection.edges, graph_filter)
        units = _pagination_units(nodes, edges)

        offset = 0
        if cursor is not None:
            offset = _decode_cursor(
                cursor,
                signing_key=self._cursor_signing_key,
                principal=principal,
                scope=scope,
                view=selected_view,
                graph_filter=graph_filter,
                limit=limit,
                snapshot_id=snapshot_id,
            )
            if offset > len(units):
                raise InvalidGraphCursorError()

        page_units = units[offset : offset + limit]
        page_nodes, page_edges = _materialize_units(page_units)
        end = offset + len(page_units)
        has_more = end < len(units)
        next_cursor = None
        if has_more:
            next_cursor = _encode_cursor(
                signing_key=self._cursor_signing_key,
                principal=principal,
                scope=scope,
                view=selected_view,
                graph_filter=graph_filter,
                limit=limit,
                snapshot_id=snapshot_id,
                offset=end,
            )

        coverage_reasons = {
            f"{item.source}_source_limit" for item in source.coverage if item.truncated
        }
        partial_reasons = tuple(sorted({*projection.partial_reasons, *coverage_reasons}))
        return GraphViewPage(
            scope=scope,
            view=selected_view,
            snapshot=GraphSnapshot(
                id=snapshot_id,
                topology_signature=topology_signature,
            ),
            snapshot_id=snapshot_id,
            projection_sources=projection.projection_sources,
            nodes=page_nodes,
            edges=page_edges,
            type_metadata=projection.type_metadata,
            partial_reasons=partial_reasons,
            truncated=bool(coverage_reasons),
            has_more=has_more,
            next_cursor=next_cursor,
        )


def _project(source: GraphSourceSnapshot, view: GraphViewKind) -> _Projection:
    if view is GraphViewKind.TASK:
        return _project_task(source)
    if view is GraphViewKind.EVIDENCE:
        return _project_evidence(source)
    return _project_operation(source)


def _project_task(source: GraphSourceSnapshot) -> _Projection:
    run_id = source.scope.run_id
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    page_reasons: set[str] = set()

    for item in sorted(source.plan_items, key=lambda item: (item.sequence, item.id)):
        reasons: tuple[str, ...] = ()
        if item.status == "blocked":
            reasons = ("blocker_lineage_unavailable",)
        elif item.status == "completed":
            reasons = ("completion_evidence_unavailable",)
        page_reasons.update(reasons)
        nodes.append(
            _node(
                node_id=f"plan_item:{run_id}:{item.id}",
                node_type="plan_item",
                domain_id=item.id,
                label=f"Plan item {item.sequence}",
                status=item.status,
                provenance=(item.provenance,),
                reasons=reasons,
            )
        )

    plan_item_ids = {item.id for item in source.plan_items}
    for item in source.plan_items:
        for dependency_id in item.dependency_ids:
            if dependency_id not in plan_item_ids:
                page_reasons.add("task_dependency_unresolved")
                continue
            edges.append(
                _edge(
                    edge_id=f"depends_on:{run_id}:{item.id}:{dependency_id}",
                    edge_type="depends_on",
                    source=f"plan_item:{run_id}:{item.id}",
                    target=f"plan_item:{run_id}:{dependency_id}",
                    provenance=("task_graph.dependencies",),
                )
            )

    execution_by_id = {item.execution_id: item for item in source.executions}
    resolved_execution_ids: set[str] = set()
    actions = sorted(source.actions, key=lambda action: action.action_id)
    for action in actions:
        status, status_reasons = _safe_status(
            action.status,
            _SAFE_ACTION_STATUSES,
            "action_status_unknown",
        )
        reason_list = list(status_reasons)
        action_id = f"action:{run_id}:{action.action_id}"
        for execution_id in action.execution_ids:
            execution = execution_by_id.get(execution_id)
            if execution is None or execution.session_id != action.session_id:
                if "action_execution_unresolved" not in reason_list:
                    reason_list.append("action_execution_unresolved")
                continue
            resolved_execution_ids.add(execution_id)
            edges.append(
                _edge(
                    edge_id=f"executed_as:{run_id}:{action.action_id}:{execution_id}",
                    edge_type="executed_as",
                    source=action_id,
                    target=f"execution:{run_id}:{execution_id}",
                    provenance=("tool_call_intents", f"execution:{run_id}:{execution_id}"),
                )
            )
        reasons = tuple(reason_list)
        page_reasons.update(reasons)
        nodes.append(
            _node(
                node_id=action_id,
                node_type="action",
                domain_id=action.action_id,
                label="Action",
                status=status,
                provenance=("tool_call_intents",),
                reasons=reasons,
            )
        )
        edges.append(
            _edge(
                edge_id=f"unassigned:{run_id}:{action.action_id}",
                edge_type="unassigned",
                source=action_id,
                target=f"unassigned_actions:{run_id}",
                provenance=("tool_call_intents",),
            )
        )
    if actions:
        nodes.append(
            _node(
                node_id=f"unassigned_actions:{run_id}",
                node_type="unassigned_actions",
                domain_id=run_id,
                label="Unassigned actions",
                status=None,
                provenance=("tool_call_intents",),
            )
        )

    index = _evidence_index(source)
    task_evidence_ids: set[str] = set()
    for execution_id in sorted(resolved_execution_ids):
        execution = execution_by_id[execution_id]
        status, reasons = _safe_status(
            execution.status,
            _SAFE_EXECUTION_STATUSES,
            "execution_status_unknown",
        )
        page_reasons.update(reasons)
        graph_id = f"execution:{run_id}:{execution_id}"
        task_evidence_ids.add(graph_id)
        nodes.append(
            _node(
                node_id=graph_id,
                node_type="execution",
                domain_id=execution_id,
                label="Execution",
                status=status,
                provenance=(graph_id,),
                reasons=reasons,
            )
        )
    for artifact in sorted(source.artifacts, key=lambda item: item.artifact_id):
        if artifact.execution_id not in resolved_execution_ids:
            continue
        graph_id = f"artifact:{run_id}:{artifact.artifact_id}"
        task_evidence_ids.add(graph_id)
        nodes.append(
            _node(
                node_id=graph_id,
                node_type="artifact",
                domain_id=artifact.artifact_id,
                label="Artifact",
                status="available",
                provenance=(graph_id,),
            )
        )
        edges.append(
            _edge(
                edge_id=(f"produced:{run_id}:{artifact.execution_id}:{artifact.artifact_id}"),
                edge_type="produced",
                source=f"execution:{run_id}:{artifact.execution_id}",
                target=graph_id,
                provenance=(
                    f"execution:{run_id}:{artifact.execution_id}",
                    graph_id,
                ),
            )
        )

    findings = sorted(source.findings, key=lambda finding: finding.finding_id)
    for finding in findings:
        trusted = _finding_evidence_targets(finding, index)
        status, confirmation_reasons = _confirmed_claim_status(
            finding.status,
            bool(trusted),
        )
        support_targets = tuple(item for item in trusted if item in task_evidence_ids)
        unresolved_ref = bool(finding.artifact_ids or finding.execution_ids) and not bool(
            support_targets
        )
        reasons = confirmation_reasons
        if unresolved_ref and "finding_evidence_unresolved" not in reasons:
            reasons = (*reasons, "finding_evidence_unresolved")
        page_reasons.update(reasons)
        finding_id = f"finding:{run_id}:{finding.finding_id}"
        nodes.append(
            _node(
                node_id=finding_id,
                node_type="finding",
                domain_id=finding.finding_id,
                label="Finding",
                status=status,
                provenance=("findings",),
                reasons=reasons,
            )
        )
        edges.append(
            _edge(
                edge_id=f"unassigned_finding:{run_id}:{finding.finding_id}",
                edge_type="unassigned",
                source=finding_id,
                target=f"unassigned_findings:{run_id}",
                provenance=("findings",),
            )
        )
        for evidence_id in support_targets:
            edges.append(
                _edge(
                    edge_id=(
                        f"supports_finding:{run_id}:{finding.finding_id}:"
                        f"{_edge_suffix(evidence_id)}"
                    ),
                    edge_type="supports",
                    source=evidence_id,
                    target=finding_id,
                    provenance=("findings", evidence_id),
                )
            )
    if findings:
        nodes.append(
            _node(
                node_id=f"unassigned_findings:{run_id}",
                node_type="unassigned_findings",
                domain_id=run_id,
                label="Unassigned findings",
                status=None,
                provenance=("findings",),
            )
        )

    return _projection(
        nodes,
        edges,
        _task_projection_sources(source),
        _TASK_TYPE_METADATA,
        page_reasons,
    )


def _task_projection_sources(source: GraphSourceSnapshot) -> tuple[str, ...]:
    plan_sources = tuple(dict.fromkeys(item.provenance for item in source.plan_items))
    dependency_sources = (
        ("task_graph.dependencies",)
        if any(item.dependency_ids for item in source.plan_items)
        else ()
    )
    return (*plan_sources, *dependency_sources, *_TASK_PROJECTION_BASE_SOURCES)


def _project_evidence(source: GraphSourceSnapshot) -> _Projection:
    run_id = source.scope.run_id
    engagement_id = source.scope.engagement_id
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    page_reasons: set[str] = set()
    index = _evidence_index(source)

    for decision in sorted(source.user_decisions, key=lambda item: item.decision_id):
        nodes.append(
            _node(
                node_id=f"user_decision:{run_id}:{decision.decision_id}",
                node_type="user_decision",
                domain_id=decision.decision_id,
                label="User decision",
                status="recorded",
                provenance=(f"user_decision:{run_id}:{decision.decision_id}",),
            )
        )

    for execution in sorted(source.executions, key=lambda item: item.execution_id):
        status, reasons = _safe_status(
            execution.status,
            _SAFE_EXECUTION_STATUSES,
            "execution_status_unknown",
        )
        page_reasons.update(reasons)
        nodes.append(
            _node(
                node_id=f"execution:{run_id}:{execution.execution_id}",
                node_type="execution",
                domain_id=execution.execution_id,
                label="Execution",
                status=status,
                provenance=("executions",),
                reasons=reasons,
            )
        )

    for artifact in sorted(source.artifacts, key=lambda item: item.artifact_id):
        artifact_graph_id = f"artifact:{run_id}:{artifact.artifact_id}"
        artifact_reasons = (
            () if artifact.execution_id in index.executions else ("artifact_execution_unresolved",)
        )
        page_reasons.update(artifact_reasons)
        nodes.append(
            _node(
                node_id=artifact_graph_id,
                node_type="artifact",
                domain_id=artifact.artifact_id,
                label="Artifact",
                status="available",
                provenance=("artifacts",),
                reasons=artifact_reasons,
            )
        )
        if artifact.execution_id in index.executions:
            edges.append(
                _edge(
                    edge_id=f"produced:{run_id}:{artifact.execution_id}:{artifact.artifact_id}",
                    edge_type="produced",
                    source=index.executions[artifact.execution_id],
                    target=artifact_graph_id,
                    provenance=("artifacts", "executions"),
                )
            )

    # Working Memory Fact source refs and their declared source types are model
    # writable. They can preserve candidate lineage, but cannot populate this
    # authoritative set or upgrade either a Fact or a Hypothesis to confirmed.
    authoritatively_confirmed_fact_ids: set[str] = set()
    fact_graph_ids: dict[str, str] = {}
    for fact in sorted(source.facts, key=lambda item: item.fact_id):
        graph_id = f"fact:{run_id}:{fact.fact_id}"
        fact_graph_ids[fact.fact_id] = graph_id
        candidate_targets = _candidate_fact_evidence_targets(fact.evidence_refs, index)
        status, status_reasons = _working_memory_fact_status(fact.status)
        reasons = (
            (*status_reasons, "candidate_evidence_not_authoritative")
            if candidate_targets
            else status_reasons
        )
        page_reasons.update(reasons)
        provenance = ["run_facts", *candidate_targets]
        nodes.append(
            _node(
                node_id=graph_id,
                node_type="fact",
                domain_id=fact.fact_id,
                label="Fact",
                status=status,
                provenance=tuple(provenance),
                reasons=reasons,
            )
        )
        for evidence_id in candidate_targets:
            edges.append(
                _edge(
                    edge_id=f"supports_fact:{engagement_id}:{fact.fact_id}:{_edge_suffix(evidence_id)}",
                    edge_type="supports",
                    source=evidence_id,
                    target=graph_id,
                    provenance=("run_facts",),
                    quality="partial",
                )
            )

    for hypothesis in sorted(source.hypotheses, key=lambda item: item.hypothesis_id):
        graph_id = f"hypothesis:{run_id}:{hypothesis.hypothesis_id}"
        has_trusted_support = any(
            item in authoritatively_confirmed_fact_ids for item in hypothesis.supporting_fact_ids
        )
        has_trusted_contradiction = any(
            item in authoritatively_confirmed_fact_ids for item in hypothesis.contradicting_fact_ids
        )
        trusted = has_trusted_support and not has_trusted_contradiction
        status, reasons = _confirmed_claim_status(hypothesis.status, trusted)
        page_reasons.update(reasons)
        nodes.append(
            _node(
                node_id=graph_id,
                node_type="hypothesis",
                domain_id=hypothesis.hypothesis_id,
                label="Hypothesis",
                status=status,
                provenance=("run_hypotheses",),
                reasons=reasons,
            )
        )
        for fact_id in hypothesis.supporting_fact_ids:
            if fact_id in fact_graph_ids:
                edges.append(
                    _edge(
                        edge_id=f"supports_hypothesis:{engagement_id}:{fact_id}:{hypothesis.hypothesis_id}",
                        edge_type="supports",
                        source=fact_graph_ids[fact_id],
                        target=graph_id,
                        provenance=("run_hypotheses", fact_graph_ids[fact_id]),
                        quality=(
                            "exact" if fact_id in authoritatively_confirmed_fact_ids else "partial"
                        ),
                    )
                )
        for fact_id in hypothesis.contradicting_fact_ids:
            if fact_id in fact_graph_ids:
                edges.append(
                    _edge(
                        edge_id=f"contradicts:{engagement_id}:{fact_id}:{hypothesis.hypothesis_id}",
                        edge_type="contradicts",
                        source=fact_graph_ids[fact_id],
                        target=graph_id,
                        provenance=("run_hypotheses", fact_graph_ids[fact_id]),
                        quality=(
                            "exact" if fact_id in authoritatively_confirmed_fact_ids else "partial"
                        ),
                    )
                )

    for finding in sorted(source.findings, key=lambda item: item.finding_id):
        graph_id = f"finding:{run_id}:{finding.finding_id}"
        trusted_targets = _finding_evidence_targets(finding, index)
        status, reasons = _confirmed_claim_status(finding.status, bool(trusted_targets))
        page_reasons.update(reasons)
        nodes.append(
            _node(
                node_id=graph_id,
                node_type="finding",
                domain_id=finding.finding_id,
                label="Finding",
                status=status,
                provenance=("findings", *trusted_targets),
                reasons=reasons,
            )
        )
        for evidence_id in trusted_targets:
            edges.append(
                _edge(
                    edge_id=f"supports_finding:{engagement_id}:{finding.finding_id}:{_edge_suffix(evidence_id)}",
                    edge_type="supports",
                    source=evidence_id,
                    target=graph_id,
                    provenance=("findings",),
                )
            )

    active_engagement_facts: dict[str, str] = {}
    for fact in sorted(source.engagement_facts, key=lambda item: item.id):
        if fact.engagement_id != engagement_id:
            page_reasons.add("cross_engagement_source_omitted")
            continue
        if run_id not in fact.source_run_ids:
            page_reasons.add("cross_run_source_omitted")
            continue
        if fact.status != "active":
            page_reasons.add("superseded_fact_omitted")
            continue
        graph_id = f"engagement_fact:{engagement_id}:{fact.id}"
        active_engagement_facts[fact.id] = graph_id
        reasons = ["engagement_fact_evidence_type_unavailable"]
        if fact.unresolved or not _engagement_fact_refs_resolve(fact, source, index):
            reasons.append("engagement_fact_provenance_unresolved")
        page_reasons.update(reasons)
        nodes.append(
            _node(
                node_id=graph_id,
                node_type="engagement_fact",
                domain_id=fact.id,
                label="Engagement fact",
                status="unverified",
                provenance=("engagement_facts",),
                reasons=tuple(reasons),
            )
        )

    for relation in sorted(source.fact_relations, key=lambda item: item.id):
        if relation.engagement_id != engagement_id:
            page_reasons.add("cross_engagement_source_omitted")
            continue
        if relation.unresolved or not _relation_refs_resolve(relation, source, index):
            page_reasons.add("unresolved_relation_omitted")
            continue
        if relation.relation_type not in _SAFE_RELATION_TYPES:
            page_reasons.add("invalid_relation_type_omitted")
            continue
        source_id = active_engagement_facts.get(relation.source_fact_id)
        target_id = active_engagement_facts.get(relation.target_fact_id)
        if source_id is None or target_id is None:
            page_reasons.add("relation_endpoint_unavailable")
            continue
        page_reasons.add("fact_relation_provenance_type_unavailable")
        edges.append(
            _edge(
                edge_id=f"fact_relation:{engagement_id}:{relation.id}",
                edge_type=relation.relation_type,
                source=source_id,
                target=target_id,
                provenance=("fact_relations",),
                quality="partial",
            )
        )

    return _projection(
        nodes,
        edges,
        _EVIDENCE_PROJECTION_SOURCES,
        _EVIDENCE_TYPE_METADATA,
        page_reasons,
    )


def _project_operation(source: GraphSourceSnapshot) -> _Projection:
    run_id = source.scope.run_id
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    page_reasons: set[str] = set()
    sessions = {item.session_id: item for item in source.sessions}
    hosts = {item.node_id: item for item in source.execution_hosts}

    for session in sorted(source.sessions, key=lambda item: item.session_id):
        status, reasons = _safe_status(
            session.status,
            _SAFE_SESSION_STATUSES,
            "session_status_unknown",
        )
        page_reasons.update(reasons)
        nodes.append(
            _node(
                node_id=f"session:{run_id}:{session.session_id}",
                node_type="session",
                domain_id=session.session_id,
                label="Session",
                status=status,
                provenance=("runtime_sessions",),
                reasons=reasons,
            )
        )
        if session.parent_session_id is not None:
            if session.parent_session_id in sessions:
                edges.append(
                    _edge(
                        edge_id=(
                            f"parent_of:{run_id}:{session.parent_session_id}:{session.session_id}"
                        ),
                        edge_type="parent_of",
                        source=f"session:{run_id}:{session.parent_session_id}",
                        target=f"session:{run_id}:{session.session_id}",
                        provenance=("runtime_sessions",),
                    )
                )
            else:
                page_reasons.add("session_parent_unresolved")

    for host in sorted(source.execution_hosts, key=lambda item: item.node_id):
        status, reasons = _safe_status(
            host.status,
            _SAFE_HOST_STATUSES,
            "execution_host_status_unknown",
        )
        page_reasons.update(reasons)
        nodes.append(
            _node(
                node_id=f"execution_host:{run_id}:{host.node_id}",
                node_type="execution_host",
                domain_id=host.node_id,
                label="RiftX execution host",
                status=status,
                provenance=("execution_hosts",),
                reasons=reasons,
            )
        )

    for execution in sorted(source.executions, key=lambda item: item.execution_id):
        status, reasons = _safe_status(
            execution.status,
            _SAFE_EXECUTION_STATUSES,
            "execution_status_unknown",
        )
        page_reasons.update(reasons)
        execution_graph_id = f"execution:{run_id}:{execution.execution_id}"
        nodes.append(
            _node(
                node_id=execution_graph_id,
                node_type="execution",
                domain_id=execution.execution_id,
                label="Execution",
                status=status,
                provenance=("executions",),
                reasons=reasons,
            )
        )
        if execution.session_id in sessions:
            edges.append(
                _edge(
                    edge_id=f"contains:{run_id}:{execution.session_id}:{execution.execution_id}",
                    edge_type="contains",
                    source=f"session:{run_id}:{execution.session_id}",
                    target=execution_graph_id,
                    provenance=("runtime_sessions", "executions"),
                )
            )
        if execution.node_id in hosts:
            edges.append(
                _edge(
                    edge_id=f"ran_on:{run_id}:{execution.execution_id}:{execution.node_id}",
                    edge_type="ran_on",
                    source=execution_graph_id,
                    target=f"execution_host:{run_id}:{execution.node_id}",
                    provenance=("executions", "execution_hosts"),
                )
            )

    return _projection(
        nodes,
        edges,
        _OPERATION_PROJECTION_SOURCES,
        _OPERATION_TYPE_METADATA,
        page_reasons,
    )


def _projection(
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    sources: tuple[str, ...],
    metadata: tuple[GraphTypeMetadata, ...],
    reasons: set[str],
) -> _Projection:
    ordered_nodes = tuple(sorted(nodes, key=lambda node: (node.type, node.id)))
    ordered_edges = tuple(sorted(edges, key=lambda edge: edge.id))
    node_ids = [node.id for node in ordered_nodes]
    edge_ids = [edge.id for edge in ordered_edges]
    if len(node_ids) != len(set(node_ids)) or len(edge_ids) != len(set(edge_ids)):
        raise GraphSourceContractError("Graph source contract is invalid")
    available = set(node_ids)
    if any(edge.source not in available or edge.target not in available for edge in ordered_edges):
        raise GraphSourceContractError("Graph source contract is invalid")
    return _Projection(
        nodes=ordered_nodes,
        edges=ordered_edges,
        projection_sources=sources,
        type_metadata=metadata,
        partial_reasons=tuple(sorted(reasons)),
    )


def _node(
    *,
    node_id: str,
    node_type: str,
    domain_id: str,
    label: str,
    status: str | None,
    provenance: tuple[str, ...],
    reasons: tuple[str, ...] = (),
) -> GraphNode:
    unique_provenance = tuple(dict.fromkeys(provenance))
    unique_reasons = tuple(dict.fromkeys(reasons))
    return GraphNode(
        id=node_id,
        type=node_type,
        domain_id=domain_id,
        label=label,
        status=status,
        provenance_refs=unique_provenance,
        projection_quality="partial" if unique_reasons else "exact",
        partial_reasons=unique_reasons,
    )


def _edge(
    *,
    edge_id: str,
    edge_type: str,
    source: str,
    target: str,
    provenance: tuple[str, ...],
    quality: str = "exact",
) -> GraphEdge:
    return GraphEdge(
        id=edge_id,
        type=edge_type,
        source=source,
        target=target,
        provenance_refs=tuple(dict.fromkeys(provenance)),
        projection_quality=quality,
    )


def _safe_status(
    value: str,
    allowed: frozenset[str],
    reason: str,
) -> tuple[str | None, tuple[str, ...]]:
    if value in allowed:
        return value, ()
    return None, (reason,)


def _confirmed_claim_status(value: str, trusted: bool) -> tuple[str | None, tuple[str, ...]]:
    if value == "confirmed":
        if trusted:
            return "confirmed", ()
        return "unverified", ("confirmation_evidence_unresolved",)
    allowed = _SAFE_FINDING_STATUSES | _SAFE_FACT_STATUSES | _SAFE_HYPOTHESIS_STATUSES
    if value in allowed:
        return value, ()
    return None, ("source_status_unknown",)


def _working_memory_fact_status(value: str) -> tuple[str | None, tuple[str, ...]]:
    if value == "confirmed":
        return "unverified", ("authoritative_evidence_association_unavailable",)
    if value in _SAFE_FACT_STATUSES:
        return value, ()
    return None, ("source_status_unknown",)


def _evidence_index(source: GraphSourceSnapshot) -> _EvidenceIndex:
    artifacts: dict[str, str] = {}
    executions: dict[str, str] = {}
    run_id = source.scope.run_id
    for execution in source.executions:
        graph_id = f"execution:{run_id}:{execution.execution_id}"
        _add_reference_aliases(executions, "execution", execution.execution_id, graph_id)
    for artifact in source.artifacts:
        if artifact.execution_id not in executions:
            continue
        graph_id = f"artifact:{run_id}:{artifact.artifact_id}"
        _add_reference_aliases(artifacts, "artifact", artifact.artifact_id, graph_id)
    decisions: dict[str, str] = {}
    for decision in source.user_decisions:
        graph_id = f"user_decision:{run_id}:{decision.decision_id}"
        decisions[decision.decision_id] = graph_id
        decisions[graph_id] = graph_id
    return _EvidenceIndex(
        artifacts=artifacts,
        executions=executions,
        user_decisions=decisions,
    )


def _add_reference_aliases(
    target: dict[str, str],
    kind: str,
    raw_id: str,
    graph_id: str,
) -> None:
    for alias in (raw_id, f"{kind}:{raw_id}", f"{kind}://{raw_id}", graph_id):
        target[alias] = graph_id


def _candidate_fact_evidence_targets(
    refs: tuple[GraphEvidenceRefSource, ...],
    index: _EvidenceIndex,
) -> tuple[str, ...]:
    targets: set[str] = set()
    for ref in refs:
        if ref.source_type == "deterministic_parser":
            target = index.artifacts.get(ref.reference_id) or index.executions.get(ref.reference_id)
            if target is not None:
                targets.add(target)
        elif ref.source_type == "user_decision":
            target = index.user_decisions.get(ref.reference_id)
            if target is not None:
                targets.add(target)
    return tuple(sorted(targets))


def _finding_evidence_targets(
    finding: GraphFindingSource,
    index: _EvidenceIndex,
) -> tuple[str, ...]:
    targets = {index.artifacts[item] for item in finding.artifact_ids if item in index.artifacts}
    targets.update(
        index.executions[item] for item in finding.execution_ids if item in index.executions
    )
    targets.update(
        index.user_decisions[item]
        for item in finding.user_decision_refs
        if item in index.user_decisions
    )
    return tuple(sorted(targets))


def _engagement_fact_refs_resolve(
    fact: GraphEngagementFactSource,
    source: GraphSourceSnapshot,
    index: _EvidenceIndex,
) -> bool:
    session_ids = {item.session_id for item in source.sessions}
    return (
        bool(fact.source_run_ids)
        and all(item == source.scope.run_id for item in fact.source_run_ids)
        and all(item in session_ids for item in fact.source_session_ids)
        and all(item in index.executions for item in fact.source_execution_ids)
        and all(item in index.artifacts for item in fact.artifact_ids)
    )


def _relation_refs_resolve(
    relation: GraphFactRelationSource,
    source: GraphSourceSnapshot,
    index: _EvidenceIndex,
) -> bool:
    session_ids = {item.session_id for item in source.sessions}
    return (
        relation.source_run_id == source.scope.run_id
        and (relation.source_session_id is None or relation.source_session_id in session_ids)
        and all(item in index.executions for item in relation.source_execution_ids)
        and all(item in index.artifacts for item in relation.artifact_ids)
    )


def _edge_suffix(graph_id: str) -> str:
    return hashlib.sha256(graph_id.encode()).hexdigest()[:16]


def _validate_source(source: GraphSourceSnapshot, scope: GraphScope) -> None:
    try:
        if type(source) is not GraphSourceSnapshot or source.scope != scope:
            raise ValueError
        if type(source.run) is not GraphRunSource:
            raise ValueError
        _require_id(source.run.id)
        _require_id(source.run.engagement_id)
        _require_optional_id(source.run.node_id)
        if source.run.id != scope.run_id or source.run.engagement_id != scope.engagement_id:
            raise ValueError

        _require_tuple(source.plan_items, GraphPlanItemSource)
        _require_tuple(source.actions, GraphActionSource)
        _require_tuple(source.facts, GraphFactSource)
        _require_tuple(source.hypotheses, GraphHypothesisSource)
        _require_tuple(source.user_decisions, GraphUserDecisionSource)
        _require_tuple(source.engagement_facts, GraphEngagementFactSource)
        _require_tuple(source.fact_relations, GraphFactRelationSource)
        _require_tuple(source.findings, GraphFindingSource)
        _require_tuple(source.artifacts, GraphArtifactSource)
        _require_tuple(source.executions, GraphExecutionSource)
        _require_tuple(source.sessions, GraphSessionSource)
        _require_tuple(source.execution_hosts, GraphExecutionHostSource)
        _require_tuple(source.coverage, GraphSourceCoverage)

        _validate_plan_items(source.plan_items, scope)
        _validate_actions(source.actions, scope)
        _validate_run_facts(source.facts, scope)
        _validate_hypotheses(source.hypotheses, scope)
        _validate_decisions(source.user_decisions, scope)
        _validate_engagement_facts(source.engagement_facts)
        _validate_fact_relations(source.fact_relations)
        _validate_findings(source.findings, scope)
        _validate_artifacts(source.artifacts, scope)
        _validate_executions(source.executions, scope)
        _validate_sessions(source.sessions, scope)
        _validate_hosts(source.execution_hosts, scope)
        _validate_coverage(source.coverage)
    except (AttributeError, TypeError, ValueError):
        raise GraphSourceContractError("Graph source contract is invalid") from None


def _require_tuple(values: object, item_type: type[object]) -> None:
    if type(values) is not tuple or len(values) > _MAX_SOURCE_ITEMS:
        raise ValueError
    if any(type(item) is not item_type for item in values):
        raise ValueError


def _require_id(value: object) -> None:
    if type(value) is not str or _DOMAIN_ID.fullmatch(value) is None:
        raise ValueError


def _require_optional_id(value: object) -> None:
    if value is not None:
        _require_id(value)


def _require_status(value: object) -> None:
    if type(value) is not str or _TYPE_TOKEN.fullmatch(value) is None:
        raise ValueError


def _require_reference(value: object) -> None:
    if type(value) is not str or not value or len(value) > 256 or _has_unsafe_unicode(value):
        raise ValueError


def _require_id_tuple(values: object) -> None:
    if type(values) is not tuple or len(values) > _MAX_SOURCE_ITEMS:
        raise ValueError
    for value in values:
        _require_id(value)
    if len(values) != len(set(values)):
        raise ValueError


def _require_unique(ids: Sequence[str]) -> None:
    if len(ids) != len(set(ids)):
        raise ValueError


def _validate_plan_items(items: tuple[GraphPlanItemSource, ...], scope: GraphScope) -> None:
    _require_unique([item.id for item in items])
    _require_unique([str(item.sequence) for item in items])
    for item in items:
        _require_id(item.id)
        if item.run_id != scope.run_id or type(item.sequence) is not int or item.sequence < 1:
            raise ValueError
        if item.status not in _SAFE_PLAN_STATUSES:
            raise ValueError
        _require_id_tuple(item.dependency_ids)
        if item.provenance not in {"task_graph.tasks", "working_memory.run_plan"}:
            raise ValueError


def _validate_actions(items: tuple[GraphActionSource, ...], scope: GraphScope) -> None:
    _require_unique([item.action_id for item in items])
    for item in items:
        _require_id(item.action_id)
        _require_id(item.session_id)
        _require_optional_id(item.tool_id)
        _require_status(item.status)
        _require_id_tuple(item.execution_ids)
        if item.run_id != scope.run_id:
            raise ValueError


def _validate_run_facts(items: tuple[GraphFactSource, ...], scope: GraphScope) -> None:
    _require_unique([item.fact_id for item in items])
    for item in items:
        _require_id(item.fact_id)
        _require_status(item.status)
        if item.run_id != scope.run_id:
            raise ValueError
        _require_tuple(item.evidence_refs, GraphEvidenceRefSource)
        for ref in item.evidence_refs:
            _require_reference(ref.reference_id)
            _require_status(ref.source_type)


def _validate_hypotheses(
    items: tuple[GraphHypothesisSource, ...],
    scope: GraphScope,
) -> None:
    _require_unique([item.hypothesis_id for item in items])
    for item in items:
        _require_id(item.hypothesis_id)
        _require_status(item.status)
        _require_id_tuple(item.supporting_fact_ids)
        _require_id_tuple(item.contradicting_fact_ids)
        if item.run_id != scope.run_id:
            raise ValueError
        if set(item.supporting_fact_ids) & set(item.contradicting_fact_ids):
            raise ValueError


def _validate_decisions(
    items: tuple[GraphUserDecisionSource, ...],
    scope: GraphScope,
) -> None:
    _require_unique([item.decision_id for item in items])
    for item in items:
        _require_id(item.decision_id)
        if item.run_id != scope.run_id:
            raise ValueError


def _validate_engagement_facts(items: tuple[GraphEngagementFactSource, ...]) -> None:
    _require_unique([item.id for item in items])
    for item in items:
        _require_id(item.id)
        _require_id(item.engagement_id)
        _require_status(item.status)
        _require_id_tuple(item.source_run_ids)
        _require_id_tuple(item.source_session_ids)
        _require_id_tuple(item.source_execution_ids)
        _require_id_tuple(item.artifact_ids)
        if type(item.unresolved) is not bool:
            raise ValueError


def _validate_fact_relations(items: tuple[GraphFactRelationSource, ...]) -> None:
    _require_unique([item.id for item in items])
    for item in items:
        _require_id(item.id)
        _require_id(item.engagement_id)
        _require_id(item.source_fact_id)
        _require_id(item.target_fact_id)
        _require_id(item.source_run_id)
        _require_optional_id(item.source_session_id)
        _require_status(item.relation_type)
        _require_id_tuple(item.source_execution_ids)
        _require_id_tuple(item.artifact_ids)
        if item.source_fact_id == item.target_fact_id or type(item.unresolved) is not bool:
            raise ValueError


def _validate_findings(items: tuple[GraphFindingSource, ...], scope: GraphScope) -> None:
    _require_unique([item.finding_id for item in items])
    for item in items:
        _require_id(item.finding_id)
        _require_status(item.status)
        _require_status(item.severity)
        _require_id_tuple(item.artifact_ids)
        _require_id_tuple(item.execution_ids)
        _require_id_tuple(item.user_decision_refs)
        if item.run_id != scope.run_id:
            raise ValueError


def _validate_artifacts(items: tuple[GraphArtifactSource, ...], scope: GraphScope) -> None:
    _require_unique([item.artifact_id for item in items])
    for item in items:
        _require_id(item.artifact_id)
        _require_optional_id(item.execution_id)
        if item.run_id != scope.run_id:
            raise ValueError


def _validate_executions(items: tuple[GraphExecutionSource, ...], scope: GraphScope) -> None:
    _require_unique([item.execution_id for item in items])
    for item in items:
        _require_id(item.execution_id)
        _require_optional_id(item.session_id)
        _require_id(item.node_id)
        _require_status(item.status)
        if item.run_id != scope.run_id:
            raise ValueError


def _validate_sessions(items: tuple[GraphSessionSource, ...], scope: GraphScope) -> None:
    _require_unique([item.session_id for item in items])
    for item in items:
        _require_id(item.session_id)
        _require_optional_id(item.parent_session_id)
        _require_status(item.status)
        if item.parent_session_id == item.session_id:
            raise ValueError
        if item.run_id != scope.run_id:
            raise ValueError


def _validate_hosts(items: tuple[GraphExecutionHostSource, ...], scope: GraphScope) -> None:
    _require_unique([item.node_id for item in items])
    for item in items:
        _require_id(item.node_id)
        _require_status(item.status)
        if item.run_id != scope.run_id:
            raise ValueError


def _validate_coverage(items: tuple[GraphSourceCoverage, ...]) -> None:
    _require_unique([item.source for item in items])
    for item in items:
        if item.source not in _COVERAGE_SOURCES:
            raise ValueError
        if type(item.scanned) is not int or item.scanned < 0:
            raise ValueError
        if type(item.limit) is not int or item.limit < 1:
            raise ValueError
        if type(item.truncated) is not bool:
            raise ValueError
        if item.truncated is not (item.scanned > item.limit):
            raise ValueError


def _resource_not_accessible() -> ResourceNotAccessibleError:
    return ResourceNotAccessibleError(
        "resource_not_accessible",
        "The requested resource was not found",
    )


def _normalize_filter(
    node_type: str | None,
    edge_type: str | None,
    focus: str | None,
    search: str | None,
) -> GraphFilter:
    values = (node_type, edge_type, focus, search)
    if any(value is not None and type(value) is not str for value in values):
        raise ValueError("Graph filters must be strings")
    if node_type is not None and _TYPE_TOKEN.fullmatch(node_type) is None:
        raise ValueError("Graph filter is invalid")
    if edge_type is not None and _TYPE_TOKEN.fullmatch(edge_type) is None:
        raise ValueError("Graph filter is invalid")
    if focus is not None and (
        not focus
        or len(focus) > _MAX_FILTER_LENGTH
        or _has_unsafe_unicode(focus)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@+~-]{0,511}", focus) is None
    ):
        raise ValueError("Graph filter is invalid")
    if search is not None and (
        not search or len(search) > _MAX_SEARCH_LENGTH or _has_unsafe_unicode(search)
    ):
        raise ValueError("Graph filter is invalid")
    return GraphFilter(
        node_type=node_type,
        edge_type=edge_type,
        focus=focus,
        search=search.casefold() if search is not None else None,
    )


def _has_unsafe_unicode(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _apply_filter(
    nodes: tuple[GraphNode, ...],
    edges: tuple[GraphEdge, ...],
    graph_filter: GraphFilter,
) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
    selected = list(nodes)
    if graph_filter.node_type is not None:
        selected = [node for node in selected if node.type == graph_filter.node_type]
    if graph_filter.search is not None:
        needle = graph_filter.search
        selected = [
            node
            for node in selected
            if needle
            in " ".join((node.id, node.domain_id, node.label, node.status or "")).casefold()
        ]
    if graph_filter.focus is not None:
        neighbors = {graph_filter.focus}
        for edge in edges:
            if edge.source == graph_filter.focus:
                neighbors.add(edge.target)
            if edge.target == graph_filter.focus:
                neighbors.add(edge.source)
        selected = [node for node in selected if node.id in neighbors]
    selected_ids = {node.id for node in selected}
    selected_edges = tuple(
        edge
        for edge in edges
        if edge.source in selected_ids
        and edge.target in selected_ids
        and (graph_filter.edge_type is None or edge.type == graph_filter.edge_type)
    )
    return tuple(selected), selected_edges


def _pagination_units(
    nodes: tuple[GraphNode, ...],
    edges: tuple[GraphEdge, ...],
) -> tuple[_PageUnit, ...]:
    by_id = {node.id: node for node in nodes}
    incident_ids: set[str] = set()
    units: list[_PageUnit] = []
    for edge in edges:
        source = by_id.get(edge.source)
        target = by_id.get(edge.target)
        if source is None or target is None:
            raise GraphSourceContractError("Graph source contract is invalid")
        incident_ids.update((source.id, target.id))
        units.append(_PageUnit(key=("edge", edge.id), nodes=(source, target), edge=edge))
    units.extend(
        _PageUnit(key=("node", node.id), nodes=(node,), edge=None)
        for node in nodes
        if node.id not in incident_ids
    )
    return tuple(sorted(units, key=lambda unit: unit.key))


def _materialize_units(
    units: Sequence[_PageUnit],
) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
    nodes_by_id: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for unit in units:
        for node in unit.nodes:
            nodes_by_id[node.id] = node
        if unit.edge is not None:
            edges.append(unit.edge)
    nodes = tuple(sorted(nodes_by_id.values(), key=lambda node: (node.type, node.id)))
    return nodes, tuple(sorted(edges, key=lambda edge: edge.id))


def _topology_signature(
    view: GraphViewKind,
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
) -> str:
    body = {
        "view": view.value,
        "nodes": [(node.id, node.type) for node in nodes],
        "edges": [(edge.id, edge.type, edge.source, edge.target) for edge in edges],
    }
    return hashlib.sha256(_TOPOLOGY_DOMAIN + _canonical_json(body)).hexdigest()


def _snapshot_signature(
    view: GraphViewKind,
    topology_signature: str,
    coverage: tuple[GraphSourceCoverage, ...],
) -> str:
    body = {
        "view": view.value,
        "topology_signature": topology_signature,
        "coverage": [
            (item.source, item.scanned, item.limit, item.truncated)
            for item in sorted(coverage, key=lambda item: item.source)
        ],
    }
    return hashlib.sha256(_TOPOLOGY_DOMAIN + b"snapshot\0" + _canonical_json(body)).hexdigest()


def _cursor_filter(graph_filter: GraphFilter) -> dict[str, str | None]:
    return {
        "node_type": graph_filter.node_type,
        "edge_type": graph_filter.edge_type,
        "focus": graph_filter.focus,
        "search": graph_filter.search,
    }


def _encode_cursor(
    *,
    signing_key: bytes,
    principal: LocalPrincipal,
    scope: GraphScope,
    view: GraphViewKind,
    graph_filter: GraphFilter,
    limit: int,
    snapshot_id: str,
    offset: int,
) -> str:
    body = {
        "v": 1,
        "principal": _principal_binding(principal),
        "run_id": scope.run_id,
        "engagement_id": scope.engagement_id,
        "view": view.value,
        "filter": _cursor_filter(graph_filter),
        "limit": limit,
        "snapshot_id": snapshot_id,
        "offset": offset,
    }
    canonical = _canonical_json(body)
    envelope = {
        "body": body,
        "signature": hmac.new(
            signing_key,
            _CURSOR_DOMAIN + canonical,
            hashlib.sha256,
        ).hexdigest(),
    }
    return base64.urlsafe_b64encode(_canonical_json(envelope)).decode().rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    signing_key: bytes,
    principal: LocalPrincipal,
    scope: GraphScope,
    view: GraphViewKind,
    graph_filter: GraphFilter,
    limit: int,
    snapshot_id: str,
) -> int:
    try:
        if type(cursor) is not str or not cursor or len(cursor) > 4096:
            raise ValueError
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4),
            altchars=b"-_",
            validate=True,
        )
        if len(raw) > 3072:
            raise ValueError
        envelope = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
        if type(envelope) is not dict or set(envelope) != {"body", "signature"}:
            raise ValueError
        body = envelope["body"]
        signature = envelope["signature"]
        if (
            type(body) is not dict
            or type(signature) is not str
            or re.fullmatch(r"[0-9a-f]{64}", signature) is None
        ):
            raise ValueError
        expected_signature = hmac.new(
            signing_key,
            _CURSOR_DOMAIN + _canonical_json(body),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError
        _validate_cursor_body(body)
        expected = {
            "principal": _principal_binding(principal),
            "run_id": scope.run_id,
            "engagement_id": scope.engagement_id,
            "view": view.value,
            "filter": _cursor_filter(graph_filter),
            "limit": limit,
        }
        if any(body[key] != value for key, value in expected.items()):
            raise InvalidGraphCursorError()
        if body["snapshot_id"] != snapshot_id:
            raise StaleGraphCursorError()
        offset = body["offset"]
        if type(offset) is not int or offset < 1:
            raise ValueError
        return offset
    except (InvalidGraphCursorError, StaleGraphCursorError):
        raise
    except (KeyError, TypeError, ValueError):
        raise InvalidGraphCursorError() from None


_CURSOR_BODY_FIELDS = {
    "v",
    "principal",
    "run_id",
    "engagement_id",
    "view",
    "filter",
    "limit",
    "snapshot_id",
    "offset",
}
_CURSOR_FILTER_FIELDS = {"node_type", "edge_type", "focus", "search"}


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _principal_binding(principal: LocalPrincipal) -> str:
    identity = {
        "id": principal.id,
        "namespace_id": principal.namespace_id,
        "profile": principal.profile.value,
    }
    return hashlib.sha256(_PRINCIPAL_DOMAIN + _canonical_json(identity)).hexdigest()


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _validate_cursor_body(body: dict[str, object]) -> None:
    if set(body) != _CURSOR_BODY_FIELDS or type(body.get("v")) is not int:
        raise ValueError
    if body["v"] != 1:
        raise ValueError
    bounded_strings = {
        "principal": 64,
        "run_id": 128,
        "engagement_id": 128,
        "view": 32,
        "snapshot_id": 64,
    }
    for name, maximum in bounded_strings.items():
        value = body[name]
        if type(value) is not str or not value or len(value) > maximum:
            raise ValueError
    if re.fullmatch(r"[0-9a-f]{64}", body["principal"]) is None:
        raise ValueError
    if re.fullmatch(r"[0-9a-f]{64}", body["snapshot_id"]) is None:
        raise ValueError
    if type(body["limit"]) is not int or not 1 <= body["limit"] <= _MAX_LIMIT:
        raise ValueError
    if type(body["offset"]) is not int or not 1 <= body["offset"] <= _MAX_SOURCE_ITEMS * 10:
        raise ValueError
    graph_filter = body["filter"]
    if type(graph_filter) is not dict or set(graph_filter) != _CURSOR_FILTER_FIELDS:
        raise ValueError
    for value in graph_filter.values():
        if value is not None and (
            type(value) is not str
            or not value
            or len(value) > _MAX_FILTER_LENGTH
            or _has_unsafe_unicode(value)
        ):
            raise ValueError
