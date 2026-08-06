"""Bounded, metadata-only SQL projection for Run Graph source snapshots."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from sqlalchemy import Integer, and_, cast, func, literal, select, true, union, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from riftx.application.graphs import (
    GraphActionSource,
    GraphArtifactSource,
    GraphEngagementFactSource,
    GraphEvidenceRefSource,
    GraphExecutionHostSource,
    GraphExecutionSource,
    GraphFactRelationSource,
    GraphFactSource,
    GraphFindingSource,
    GraphHypothesisSource,
    GraphPlanItemSource,
    GraphReasoningEdgeSource,
    GraphReasoningNodeSource,
    GraphRunSource,
    GraphScope,
    GraphSessionSource,
    GraphSourceContractError,
    GraphSourceCoverage,
    GraphSourceSnapshot,
    GraphUserDecisionSource,
    GraphViewKind,
)

from .artifact_visibility import artifact_is_not_target_http_sensitive
from .orm import (
    AgentSessionRecord,
    ArtifactRecord,
    EngagementFactRecord,
    ExecutionRecord,
    FactRelationRecord,
    FindingRecord,
    NodeRecord,
    ReasoningEdgeEvidenceRecord,
    ReasoningEdgeRecord,
    ReasoningGraphRecord,
    ReasoningNodeEvidenceRecord,
    ReasoningNodeRecord,
    RunRecord,
    TaskDependencyRecord,
    TaskGraphRecord,
    TaskRecord,
    ToolCallIntentRecord,
    WorkingMemoryRecord,
)

SessionFactory = async_sessionmaker[AsyncSession]
_MAX_GRAPH_SOURCE_ITEMS = 10_000

_COVERAGE_ORDER = (
    "plan_items",
    "task_dependencies",
    "actions",
    "facts",
    "hypotheses",
    "reasoning_nodes",
    "reasoning_edges",
    "user_decisions",
    "engagement_facts",
    "fact_relations",
    "findings",
    "artifacts",
    "executions",
    "sessions",
    "execution_hosts",
)


@dataclass(frozen=True, slots=True)
class GraphReadLimits:
    """Hard materialization budgets; small values are injectable in tests."""

    plan_items: int = 1_000
    task_dependencies: int = 10_000
    actions: int = 10_000
    facts: int = 10_000
    hypotheses: int = 10_000
    reasoning_nodes: int = 10_000
    reasoning_edges: int = 10_000
    user_decisions: int = 10_000
    engagement_facts: int = 10_000
    fact_relations: int = 10_000
    findings: int = 10_000
    artifacts: int = 10_000
    executions: int = 10_000
    sessions: int = 10_000
    execution_hosts: int = 10_000

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or value < 1 or value > _MAX_GRAPH_SOURCE_ITEMS:
                raise ValueError(
                    f"Graph source limit {item.name!r} must be between "
                    f"1 and {_MAX_GRAPH_SOURCE_ITEMS}"
                )


@dataclass(frozen=True, slots=True)
class _BoundedRows:
    rows: tuple[Mapping[str, object], ...]
    scanned: int
    truncated: bool


class SQLAlchemyGraphReadRepository:
    """Resolve one Run snapshot with a constant, bounded SELECT shape.

    SELECT lists contain only graph topology fields. JSON table projections
    extract individual identity/state leaves, so task/fact/decision/finding
    prose and all command, output, path, credential, or secret bodies never
    cross the persistence boundary.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        limits: GraphReadLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or GraphReadLimits()

    async def resolve_scope(self, run_id: str) -> GraphScope | None:
        statement = select(RunRecord.id, RunRecord.engagement_id).where(RunRecord.id == run_id)
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return GraphScope(run_id=row.id, engagement_id=row.engagement_id)

    async def load(
        self,
        scope: GraphScope,
        view: GraphViewKind,
    ) -> GraphSourceSnapshot:
        limits = self._limits
        async with self._session_factory() as session, session.begin():
            if view is GraphViewKind.TASK:
                run, plan_items, plan_coverage, dependency_coverage = (
                    await _load_run_and_plan_items(
                        session,
                        scope,
                        limits.plan_items,
                        limits.task_dependencies,
                    )
                )
                action_rows, action_coverage = await _load_action_rows(
                    session,
                    scope,
                    limits.actions,
                )
                executions, action_execution_ids, execution_coverage = await _load_executions(
                    session,
                    scope,
                    action_rows,
                    limits.executions,
                )
                artifacts, artifact_coverage = await _load_artifacts(
                    session,
                    scope,
                    limits.artifacts,
                    execution_ids={item.execution_id for item in executions},
                )
                findings, finding_coverage = await _load_findings(
                    session,
                    scope,
                    limits.findings,
                    artifact_ids={item.artifact_id for item in artifacts},
                    execution_ids={item.execution_id for item in executions},
                    decision_ids=set(),
                )
                actions = tuple(
                    GraphActionSource(
                        action_id=_required_string(row, "action_id"),
                        run_id=scope.run_id,
                        session_id=_required_string(row, "session_id"),
                        tool_id=_optional_string(row, "tool_id"),
                        status=_required_string(row, "status"),
                        execution_ids=action_execution_ids.get(
                            _required_string(row, "action_id"),
                            (),
                        ),
                    )
                    for row in action_rows
                )
                return GraphSourceSnapshot(
                    scope=scope,
                    run=run,
                    plan_items=plan_items,
                    actions=actions,
                    findings=findings,
                    artifacts=artifacts,
                    executions=executions,
                    coverage=_ordered_coverage(
                        plan_coverage,
                        dependency_coverage,
                        action_coverage,
                        finding_coverage,
                        artifact_coverage,
                        execution_coverage,
                    ),
                )

            if view is GraphViewKind.EVIDENCE:
                run = await _load_run_only(session, scope)
                executions, _, execution_coverage = await _load_executions(
                    session,
                    scope,
                    (),
                    limits.executions,
                )
                sessions, session_coverage = await _load_sessions(
                    session,
                    scope,
                    limits.sessions,
                )
                artifacts, artifact_coverage = await _load_artifacts(
                    session,
                    scope,
                    limits.artifacts,
                    execution_ids={item.execution_id for item in executions},
                )
                user_decisions, decision_coverage = await _load_user_decisions(
                    session,
                    scope,
                    limits.user_decisions,
                )
                (
                    reasoning_version,
                    reasoning_nodes,
                    reasoning_edges,
                    reasoning_node_coverage,
                    reasoning_edge_coverage,
                ) = await _load_reasoning_graph(
                    session,
                    scope,
                    node_limit=limits.reasoning_nodes,
                    edge_limit=limits.reasoning_edges,
                )
                legacy_coverage: tuple[GraphSourceCoverage, ...] = ()
                reasoning_coverage = (
                    ()
                    if reasoning_version is None
                    else (reasoning_node_coverage, reasoning_edge_coverage)
                )
                facts: tuple[GraphFactSource, ...] = ()
                hypotheses: tuple[GraphHypothesisSource, ...] = ()
                evidence_findings: tuple[GraphFindingSource, ...] = ()
                if reasoning_version is None:
                    facts, fact_coverage = await _load_facts(
                        session,
                        scope,
                        limits.facts,
                        allowed_reference_ids={
                            *(item.artifact_id for item in artifacts),
                            *(item.execution_id for item in executions),
                            *(item.decision_id for item in user_decisions),
                        },
                    )
                    hypotheses, hypothesis_coverage = await _load_hypotheses(
                        session,
                        scope,
                        limits.hypotheses,
                        fact_ids={item.fact_id for item in facts},
                    )
                    evidence_findings, finding_coverage = await _load_findings(
                        session,
                        scope,
                        limits.findings,
                        artifact_ids={item.artifact_id for item in artifacts},
                        execution_ids={item.execution_id for item in executions},
                        decision_ids={item.decision_id for item in user_decisions},
                    )
                    legacy_coverage = (
                        fact_coverage,
                        hypothesis_coverage,
                        finding_coverage,
                    )
                engagement_facts, engagement_fact_coverage = await _load_engagement_facts(
                    session,
                    scope,
                    limits.engagement_facts,
                    session_ids={item.session_id for item in sessions},
                    execution_ids={item.execution_id for item in executions},
                    artifact_ids={item.artifact_id for item in artifacts},
                )
                fact_relations, relation_coverage = await _load_fact_relations(
                    session,
                    scope,
                    limits.fact_relations,
                    fact_ids={item.id for item in engagement_facts},
                    session_ids={item.session_id for item in sessions},
                    execution_ids={item.execution_id for item in executions},
                    artifact_ids={item.artifact_id for item in artifacts},
                )
                return GraphSourceSnapshot(
                    scope=scope,
                    run=run,
                    facts=facts,
                    hypotheses=hypotheses,
                    reasoning_graph_version=reasoning_version,
                    reasoning_nodes=reasoning_nodes,
                    reasoning_edges=reasoning_edges,
                    user_decisions=user_decisions,
                    engagement_facts=engagement_facts,
                    fact_relations=fact_relations,
                    findings=evidence_findings,
                    artifacts=artifacts,
                    executions=executions,
                    sessions=sessions,
                    coverage=_ordered_coverage(
                        *reasoning_coverage,
                        *legacy_coverage,
                        decision_coverage,
                        engagement_fact_coverage,
                        relation_coverage,
                        artifact_coverage,
                        execution_coverage,
                        session_coverage,
                    ),
                )

            if view is GraphViewKind.OPERATION:
                run = await _load_run_only(session, scope)
                executions, _, execution_coverage = await _load_executions(
                    session,
                    scope,
                    (),
                    limits.executions,
                )
                sessions, session_coverage = await _load_sessions(
                    session,
                    scope,
                    limits.sessions,
                )
                execution_hosts, host_coverage = await _load_execution_hosts(
                    session,
                    scope,
                    limits.execution_hosts,
                )
                return GraphSourceSnapshot(
                    scope=scope,
                    run=run,
                    executions=executions,
                    sessions=sessions,
                    execution_hosts=execution_hosts,
                    coverage=_ordered_coverage(
                        execution_coverage,
                        session_coverage,
                        host_coverage,
                    ),
                )

        raise ValueError("Unknown Graph view")


async def _load_run_only(
    session: AsyncSession,
    scope: GraphScope,
) -> GraphRunSource:
    statement = select(
        RunRecord.id.label("run_id"),
        RunRecord.engagement_id,
        RunRecord.node_id,
    ).where(
        RunRecord.id == scope.run_id,
        RunRecord.engagement_id == scope.engagement_id,
    )
    loaded_row = (await session.execute(statement)).mappings().one_or_none()
    if loaded_row is None:
        raise GraphSourceContractError("Graph scope disappeared while loading its snapshot")
    row: Mapping[str, object] = dict(loaded_row)
    return GraphRunSource(
        id=_required_string(row, "run_id"),
        engagement_id=_required_string(row, "engagement_id"),
        node_id=_optional_string(row, "node_id"),
    )


async def _load_run_and_plan_items(
    session: AsyncSession,
    scope: GraphScope,
    plan_limit: int,
    dependency_limit: int,
) -> tuple[
    GraphRunSource,
    tuple[GraphPlanItemSource, ...],
    GraphSourceCoverage,
    GraphSourceCoverage,
]:
    statement = (
        select(
            RunRecord.id.label("run_id"),
            RunRecord.engagement_id,
            RunRecord.node_id,
            TaskGraphRecord.run_id.label("task_graph_run_id"),
            TaskRecord.id.label("plan_item_id"),
            TaskRecord.sequence.label("plan_item_sequence"),
            TaskRecord.status.label("plan_item_status"),
        )
        .select_from(RunRecord)
        .outerjoin(TaskGraphRecord, TaskGraphRecord.run_id == RunRecord.id)
        .outerjoin(TaskRecord, TaskRecord.run_id == TaskGraphRecord.run_id)
        .where(
            RunRecord.id == scope.run_id,
            RunRecord.engagement_id == scope.engagement_id,
        )
        .order_by(TaskRecord.sequence, TaskRecord.id)
    )
    bounded_tasks = await _bounded_rows(session, statement, plan_limit)
    rows = bounded_tasks.rows
    if not rows:
        raise GraphSourceContractError("Graph scope disappeared while loading its snapshot")
    first = rows[0]
    run = GraphRunSource(
        id=_required_string(first, "run_id"),
        engagement_id=_required_string(first, "engagement_id"),
        node_id=_optional_string(first, "node_id"),
    )
    if first["task_graph_run_id"] is None:
        plan_items, plan_coverage = await _load_legacy_plan_items(
            session,
            scope,
            plan_limit,
        )
        return (
            run,
            plan_items,
            plan_coverage,
            _coverage(
                "task_dependencies",
                scanned=0,
                limit=dependency_limit,
                truncated=False,
            ),
        )

    populated = tuple(row for row in rows if row["plan_item_id"] is not None)
    truncated = bounded_tasks.truncated
    materialized = populated
    task_ids = tuple(_required_string(row, "plan_item_id") for row in materialized)
    dependency_statement = (
        select(
            TaskDependencyRecord.task_id,
            TaskDependencyRecord.depends_on_task_id,
        )
        .where(
            TaskDependencyRecord.run_id == scope.run_id,
            TaskDependencyRecord.task_id.in_(task_ids),
        )
        .order_by(
            TaskDependencyRecord.task_id,
            TaskDependencyRecord.depends_on_task_id,
        )
    )
    bounded_dependencies = await _bounded_rows(
        session,
        dependency_statement,
        dependency_limit,
    )
    dependencies_by_task: defaultdict[str, list[str]] = defaultdict(list)
    for row in bounded_dependencies.rows:
        dependencies_by_task[_required_string(row, "task_id")].append(
            _required_string(row, "depends_on_task_id")
        )
    plan_items = tuple(
        GraphPlanItemSource(
            id=task_id,
            run_id=scope.run_id,
            sequence=_required_positive_int(row, "plan_item_sequence"),
            status=_required_string(row, "plan_item_status"),
            dependency_ids=tuple(dependencies_by_task[task_id]),
            provenance="task_graph.tasks",
        )
        for row, task_id in zip(materialized, task_ids, strict=True)
    )
    return (
        run,
        plan_items,
        _coverage(
            "plan_items",
            scanned=bounded_tasks.scanned if truncated else len(populated),
            limit=plan_limit,
            truncated=truncated,
        ),
        _coverage_from_bounded(
            "task_dependencies",
            dependency_limit,
            bounded_dependencies,
        ),
    )


async def _load_legacy_plan_items(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
) -> tuple[tuple[GraphPlanItemSource, ...], GraphSourceCoverage]:
    plan_rows = _json_each(
        WorkingMemoryRecord.state_json,
        "$.run_plan.items",
        "graph_plan_items",
    )
    statement = (
        select(
            func.json_extract(plan_rows.c.value, "$.id").label("plan_item_id"),
            func.json_extract(plan_rows.c.value, "$.sequence").label("plan_item_sequence"),
            func.json_extract(plan_rows.c.value, "$.status").label("plan_item_status"),
        )
        .select_from(WorkingMemoryRecord)
        .outerjoin(plan_rows, true())
        .where(WorkingMemoryRecord.run_id == scope.run_id)
        .order_by(cast(plan_rows.c.key, Integer), plan_rows.c.key)
    )
    bounded = await _bounded_rows(session, statement, limit)
    rows = bounded.rows
    populated = tuple(row for row in rows if row["plan_item_id"] is not None)
    truncated = bounded.truncated
    materialized = populated
    plan_items = tuple(
        GraphPlanItemSource(
            id=_required_string(row, "plan_item_id"),
            run_id=scope.run_id,
            sequence=_required_positive_int(row, "plan_item_sequence"),
            status=_required_string(row, "plan_item_status"),
        )
        for row in materialized
    )
    return (
        plan_items,
        _coverage(
            "plan_items",
            scanned=bounded.scanned if truncated else len(populated),
            limit=limit,
            truncated=truncated,
        ),
    )


async def _load_action_rows(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
) -> tuple[tuple[Mapping[str, object], ...], GraphSourceCoverage]:
    statement = (
        select(
            ToolCallIntentRecord.id.label("action_id"),
            ToolCallIntentRecord.session_id,
            ToolCallIntentRecord.tool_id,
            ToolCallIntentRecord.status,
        )
        .join(
            AgentSessionRecord,
            and_(
                AgentSessionRecord.id == ToolCallIntentRecord.session_id,
                AgentSessionRecord.run_id == ToolCallIntentRecord.run_id,
            ),
        )
        .where(ToolCallIntentRecord.run_id == scope.run_id)
        .order_by(ToolCallIntentRecord.id)
    )
    bounded = await _bounded_rows(session, statement, limit)
    for row in bounded.rows:
        _required_string(row, "action_id")
        _required_string(row, "session_id")
        _optional_string(row, "tool_id")
        _required_string(row, "status")
    return bounded.rows, _coverage_from_bounded("actions", limit, bounded)


async def _load_executions(
    session: AsyncSession,
    scope: GraphScope,
    action_rows: tuple[Mapping[str, object], ...],
    limit: int,
) -> tuple[
    tuple[GraphExecutionSource, ...],
    dict[str, tuple[str, ...]],
    GraphSourceCoverage,
]:
    execution_session = aliased(
        AgentSessionRecord,
        name="graph_execution_session",
    )
    statement = (
        select(
            ExecutionRecord.id.label("execution_id"),
            execution_session.id.label("session_id"),
            ExecutionRecord.node_id,
            ExecutionRecord.status,
            ToolCallIntentRecord.id.label("action_id"),
            AgentSessionRecord.id.label("action_session_id"),
        )
        .select_from(ExecutionRecord)
        .outerjoin(
            execution_session,
            and_(
                execution_session.id == ExecutionRecord.session_id,
                execution_session.run_id == ExecutionRecord.run_id,
            ),
        )
        .outerjoin(
            ToolCallIntentRecord,
            and_(
                ToolCallIntentRecord.id == ExecutionRecord.tool_call_id,
                ToolCallIntentRecord.run_id == ExecutionRecord.run_id,
                ToolCallIntentRecord.session_id == ExecutionRecord.session_id,
            ),
        )
        .outerjoin(
            AgentSessionRecord,
            and_(
                AgentSessionRecord.id == ToolCallIntentRecord.session_id,
                AgentSessionRecord.run_id == ExecutionRecord.run_id,
            ),
        )
        .where(ExecutionRecord.run_id == scope.run_id)
        .order_by(ExecutionRecord.id)
    )
    bounded = await _bounded_rows(session, statement, limit)
    action_sessions = {
        _required_string(row, "action_id"): _required_string(row, "session_id")
        for row in action_rows
    }
    action_execution_ids: defaultdict[str, list[str]] = defaultdict(list)
    executions: list[GraphExecutionSource] = []
    for row in bounded.rows:
        execution_id = _required_string(row, "execution_id")
        session_id = _optional_string(row, "session_id")
        executions.append(
            GraphExecutionSource(
                execution_id=execution_id,
                run_id=scope.run_id,
                session_id=session_id,
                node_id=_required_string(row, "node_id"),
                status=_required_string(row, "status"),
            )
        )
        action_id = _optional_string(row, "action_id")
        action_session_id = _optional_string(row, "action_session_id")
        if (
            action_id is not None
            and action_session_id == session_id
            and action_sessions.get(action_id) == session_id
        ):
            action_execution_ids[action_id].append(execution_id)
    return (
        tuple(executions),
        {key: tuple(values) for key, values in action_execution_ids.items()},
        _coverage_from_bounded("executions", limit, bounded),
    )


async def _load_sessions(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
) -> tuple[tuple[GraphSessionSource, ...], GraphSourceCoverage]:
    parent_session = aliased(
        AgentSessionRecord,
        name="graph_parent_session",
    )
    statement = (
        select(
            AgentSessionRecord.id.label("session_id"),
            AgentSessionRecord.status,
            parent_session.id.label("parent_session_id"),
        )
        .outerjoin(
            parent_session,
            and_(
                parent_session.id == AgentSessionRecord.parent_session_id,
                parent_session.run_id == AgentSessionRecord.run_id,
            ),
        )
        .where(AgentSessionRecord.run_id == scope.run_id)
        .order_by(AgentSessionRecord.id)
    )
    bounded = await _bounded_rows(session, statement, limit)
    sessions = tuple(
        GraphSessionSource(
            session_id=_required_string(row, "session_id"),
            run_id=scope.run_id,
            status=_required_string(row, "status"),
            parent_session_id=_optional_string(row, "parent_session_id"),
        )
        for row in bounded.rows
    )
    return sessions, _coverage_from_bounded("sessions", limit, bounded)


async def _load_execution_hosts(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
) -> tuple[tuple[GraphExecutionHostSource, ...], GraphSourceCoverage]:
    execution_hosts = (
        select(
            NodeRecord.id.label("node_id"),
            NodeRecord.status.label("status"),
        )
        .select_from(NodeRecord)
        .join(ExecutionRecord, ExecutionRecord.node_id == NodeRecord.id)
        .where(ExecutionRecord.run_id == scope.run_id)
    )
    assigned_run_host = (
        select(
            NodeRecord.id.label("node_id"),
            NodeRecord.status.label("status"),
        )
        .select_from(NodeRecord)
        .join(RunRecord, RunRecord.node_id == NodeRecord.id)
        .where(
            RunRecord.id == scope.run_id,
            RunRecord.engagement_id == scope.engagement_id,
        )
    )
    hosts = union(execution_hosts, assigned_run_host).subquery("graph_execution_hosts")
    statement = select(hosts.c.node_id, hosts.c.status).order_by(hosts.c.node_id)
    bounded = await _bounded_rows(session, statement, limit)
    result = tuple(
        GraphExecutionHostSource(
            node_id=_required_string(row, "node_id"),
            run_id=scope.run_id,
            status=_required_string(row, "status"),
        )
        for row in bounded.rows
    )
    return result, _coverage_from_bounded("execution_hosts", limit, bounded)


async def _load_artifacts(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
    *,
    execution_ids: set[str],
) -> tuple[tuple[GraphArtifactSource, ...], GraphSourceCoverage]:
    statement = (
        select(
            ArtifactRecord.id.label("artifact_id"),
            ArtifactRecord.execution_id,
        )
        .where(
            ArtifactRecord.run_id == scope.run_id,
            artifact_is_not_target_http_sensitive(),
        )
        .order_by(ArtifactRecord.id)
    )
    bounded = await _bounded_rows(session, statement, limit)
    artifacts = tuple(
        GraphArtifactSource(
            artifact_id=_required_string(row, "artifact_id"),
            run_id=scope.run_id,
            execution_id=_allow_reference(
                _optional_string(row, "execution_id"),
                execution_ids,
            ),
        )
        for row in bounded.rows
    )
    return artifacts, _coverage_from_bounded("artifacts", limit, bounded)


async def _load_reasoning_graph(
    session: AsyncSession,
    scope: GraphScope,
    *,
    node_limit: int,
    edge_limit: int,
) -> tuple[
    int | None,
    tuple[GraphReasoningNodeSource, ...],
    tuple[GraphReasoningEdgeSource, ...],
    GraphSourceCoverage,
    GraphSourceCoverage,
]:
    graph = await session.get(ReasoningGraphRecord, scope.run_id)
    if graph is None:
        return (
            None,
            (),
            (),
            _coverage("reasoning_nodes", scanned=0, limit=node_limit, truncated=False),
            _coverage("reasoning_edges", scanned=0, limit=edge_limit, truncated=False),
        )

    node_rows = await _bounded_rows(
        session,
        select(
            ReasoningNodeRecord.id.label("node_id"),
            ReasoningNodeRecord.kind,
            ReasoningNodeRecord.status,
        )
        .where(ReasoningNodeRecord.run_id == scope.run_id)
        .order_by(ReasoningNodeRecord.id),
        node_limit,
    )
    root_node_ids = {_required_string(row, "node_id") for row in node_rows.rows}
    node_evidence_rows = (
        await _bounded_rows(
            session,
            select(
                ReasoningNodeEvidenceRecord.node_id,
                ReasoningNodeEvidenceRecord.evidence_id,
            )
            .where(
                ReasoningNodeEvidenceRecord.run_id == scope.run_id,
                ReasoningNodeEvidenceRecord.node_id.in_(root_node_ids),
            )
            .order_by(
                ReasoningNodeEvidenceRecord.node_id,
                ReasoningNodeEvidenceRecord.ordinal,
            ),
            node_limit,
        )
        if root_node_ids
        else _BoundedRows(rows=(), scanned=0, truncated=False)
    )
    visible_node_ids = set(root_node_ids)
    if node_evidence_rows.truncated and node_evidence_rows.rows:
        incomplete = _required_string(node_evidence_rows.rows[-1], "node_id")
        visible_node_ids = {node_id for node_id in visible_node_ids if node_id < incomplete}
    evidence_by_node: defaultdict[str, list[str]] = defaultdict(list)
    for row in node_evidence_rows.rows:
        node_id = _required_string(row, "node_id")
        if node_id in visible_node_ids:
            evidence_by_node[node_id].append(_required_string(row, "evidence_id"))
    nodes = tuple(
        GraphReasoningNodeSource(
            node_id=node_id,
            run_id=scope.run_id,
            kind=_required_string(row, "kind"),
            status=_required_string(row, "status"),
            evidence_ids=tuple(evidence_by_node[node_id]),
        )
        for row in node_rows.rows
        if (node_id := _required_string(row, "node_id")) in visible_node_ids
    )

    edge_rows = await _bounded_rows(
        session,
        select(
            ReasoningEdgeRecord.id.label("edge_id"),
            ReasoningEdgeRecord.source_node_id,
            ReasoningEdgeRecord.target_node_id,
            ReasoningEdgeRecord.relation_type,
        )
        .where(
            ReasoningEdgeRecord.run_id == scope.run_id,
            ReasoningEdgeRecord.source_node_id.in_(visible_node_ids),
            ReasoningEdgeRecord.target_node_id.in_(visible_node_ids),
        )
        .order_by(ReasoningEdgeRecord.id),
        edge_limit,
    )
    root_edge_ids = {_required_string(row, "edge_id") for row in edge_rows.rows}
    edge_evidence_rows = (
        await _bounded_rows(
            session,
            select(
                ReasoningEdgeEvidenceRecord.edge_id,
                ReasoningEdgeEvidenceRecord.evidence_id,
            )
            .where(
                ReasoningEdgeEvidenceRecord.run_id == scope.run_id,
                ReasoningEdgeEvidenceRecord.edge_id.in_(root_edge_ids),
            )
            .order_by(
                ReasoningEdgeEvidenceRecord.edge_id,
                ReasoningEdgeEvidenceRecord.ordinal,
            ),
            edge_limit,
        )
        if root_edge_ids
        else _BoundedRows(rows=(), scanned=0, truncated=False)
    )
    visible_edge_ids = set(root_edge_ids)
    if edge_evidence_rows.truncated and edge_evidence_rows.rows:
        incomplete = _required_string(edge_evidence_rows.rows[-1], "edge_id")
        visible_edge_ids = {edge_id for edge_id in visible_edge_ids if edge_id < incomplete}
    evidence_by_edge: defaultdict[str, list[str]] = defaultdict(list)
    for row in edge_evidence_rows.rows:
        edge_id = _required_string(row, "edge_id")
        if edge_id in visible_edge_ids:
            evidence_by_edge[edge_id].append(_required_string(row, "evidence_id"))
    edges = tuple(
        GraphReasoningEdgeSource(
            edge_id=edge_id,
            run_id=scope.run_id,
            source_node_id=_required_string(row, "source_node_id"),
            target_node_id=_required_string(row, "target_node_id"),
            relation_type=_required_string(row, "relation_type"),
            evidence_ids=tuple(evidence_by_edge[edge_id]),
        )
        for row in edge_rows.rows
        if (edge_id := _required_string(row, "edge_id")) in visible_edge_ids
    )
    node_truncated = node_rows.truncated or node_evidence_rows.truncated
    edge_truncated = edge_rows.truncated or edge_evidence_rows.truncated
    return (
        graph.version,
        nodes,
        edges,
        _coverage(
            "reasoning_nodes",
            scanned=node_limit + 1 if node_truncated else len(nodes),
            limit=node_limit,
            truncated=node_truncated,
        ),
        _coverage(
            "reasoning_edges",
            scanned=edge_limit + 1 if edge_truncated else len(edges),
            limit=edge_limit,
            truncated=edge_truncated,
        ),
    )


async def _load_user_decisions(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
) -> tuple[tuple[GraphUserDecisionSource, ...], GraphSourceCoverage]:
    decisions = _json_each(
        WorkingMemoryRecord.state_json,
        "$.user_decisions",
        "graph_user_decisions",
    )
    statement = (
        select(
            func.json_extract(decisions.c.value, "$.id").label("decision_id"),
        )
        .select_from(WorkingMemoryRecord)
        .join(decisions, true())
        .where(WorkingMemoryRecord.run_id == scope.run_id)
        .order_by(cast(decisions.c.key, Integer), decisions.c.key)
    )
    bounded = await _bounded_rows(session, statement, limit)
    result = tuple(
        GraphUserDecisionSource(
            decision_id=_required_string(row, "decision_id"),
            run_id=scope.run_id,
        )
        for row in bounded.rows
    )
    return result, _coverage_from_bounded("user_decisions", limit, bounded)


async def _load_facts(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
    *,
    allowed_reference_ids: set[str],
) -> tuple[tuple[GraphFactSource, ...], GraphSourceCoverage]:
    facts = _json_each(
        WorkingMemoryRecord.state_json,
        "$.confirmed_facts",
        "graph_facts",
    )
    references = _json_each(facts.c.value, "$.source_refs", "graph_fact_references")
    source_types = _json_each(facts.c.value, "$.source_types", "graph_fact_source_types")
    statement = (
        select(
            facts.c.key.label("fact_order"),
            func.json_extract(facts.c.value, "$.id").label("fact_id"),
            func.json_extract(facts.c.value, "$.status").label("status"),
            references.c.key.label("reference_order"),
            references.c.value.label("reference_id"),
            source_types.c.value.label("source_type"),
        )
        .select_from(WorkingMemoryRecord)
        .join(facts, true())
        .outerjoin(references, true())
        .outerjoin(source_types, source_types.c.key == references.c.value)
        .where(WorkingMemoryRecord.run_id == scope.run_id)
        .order_by(
            cast(facts.c.key, Integer),
            facts.c.key,
            cast(references.c.key, Integer),
            references.c.key,
        )
    )
    bounded = await _bounded_rows(session, statement, limit)
    incomplete_fact_id = None
    if bounded.truncated and bounded.rows:
        incomplete_fact_id = _required_string(bounded.rows[-1], "fact_id")
    grouped: dict[str, tuple[str, list[GraphEvidenceRefSource]]] = {}
    for row in bounded.rows:
        fact_id = _required_string(row, "fact_id")
        if fact_id == incomplete_fact_id:
            continue
        status = _required_string(row, "status")
        stored_status, evidence = grouped.setdefault(fact_id, (status, []))
        if stored_status != status:
            raise GraphSourceContractError("Graph Fact source has conflicting statuses")
        reference_id = _optional_string(row, "reference_id")
        source_type = _optional_string(row, "source_type")
        if (
            reference_id is not None
            and source_type is not None
            and reference_id in allowed_reference_ids
        ):
            evidence.append(
                GraphEvidenceRefSource(
                    reference_id=reference_id,
                    source_type=source_type,
                )
            )
    result = tuple(
        GraphFactSource(
            fact_id=fact_id,
            run_id=scope.run_id,
            status=status,
            evidence_refs=tuple(_deduplicate(evidence, key=lambda item: item.reference_id)),
        )
        for fact_id, (status, evidence) in grouped.items()
    )
    return result, _coverage(
        "facts",
        scanned=limit + 1 if bounded.truncated else len(result),
        limit=limit,
        truncated=bounded.truncated,
    )


async def _load_hypotheses(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
    *,
    fact_ids: set[str],
) -> tuple[tuple[GraphHypothesisSource, ...], GraphSourceCoverage]:
    hypotheses = _json_each(
        WorkingMemoryRecord.state_json,
        "$.hypotheses",
        "graph_hypotheses",
    )
    root_statement = (
        select(
            hypotheses.c.key.label("hypothesis_order"),
            func.json_extract(hypotheses.c.value, "$.id").label("hypothesis_id"),
            func.json_extract(hypotheses.c.value, "$.status").label("status"),
        )
        .select_from(WorkingMemoryRecord)
        .join(hypotheses, true())
        .where(WorkingMemoryRecord.run_id == scope.run_id)
        .order_by(cast(hypotheses.c.key, Integer), hypotheses.c.key)
    )
    roots = await _bounded_rows(session, root_statement, limit)
    supporting = await _load_hypothesis_reference_rows(
        session,
        scope,
        limit,
        path="$.supporting_fact_ids",
        alias="graph_hypothesis_supporting",
    )
    contradicting = await _load_hypothesis_reference_rows(
        session,
        scope,
        limit,
        path="$.contradicting_fact_ids",
        alias="graph_hypothesis_contradicting",
    )
    root_ids = {_required_string(row, "hypothesis_id") for row in roots.rows}
    incomplete: set[str] = set()
    for rows in (supporting, contradicting):
        if rows.truncated and rows.rows:
            candidate = _required_string(rows.rows[-1], "hypothesis_id")
            if candidate in root_ids:
                incomplete.add(candidate)
    support_by_id = _group_valid_references(supporting.rows, fact_ids)
    contradict_by_id = _group_valid_references(contradicting.rows, fact_ids)
    result = tuple(
        GraphHypothesisSource(
            hypothesis_id=hypothesis_id,
            run_id=scope.run_id,
            status=_required_string(row, "status"),
            supporting_fact_ids=support_by_id.get(hypothesis_id, ()),
            contradicting_fact_ids=contradict_by_id.get(hypothesis_id, ()),
        )
        for row in roots.rows
        if (hypothesis_id := _required_string(row, "hypothesis_id")) not in incomplete
    )
    truncated = roots.truncated or supporting.truncated or contradicting.truncated
    return result, _coverage(
        "hypotheses",
        scanned=limit + 1 if truncated else len(result),
        limit=limit,
        truncated=truncated,
    )


async def _load_hypothesis_reference_rows(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
    *,
    path: str,
    alias: str,
) -> _BoundedRows:
    hypotheses = _json_each(
        WorkingMemoryRecord.state_json,
        "$.hypotheses",
        f"{alias}_roots",
    )
    references = _json_each(hypotheses.c.value, path, alias)
    statement = (
        select(
            hypotheses.c.key.label("hypothesis_order"),
            func.json_extract(hypotheses.c.value, "$.id").label("hypothesis_id"),
            references.c.key.label("reference_order"),
            references.c.value.label("reference_id"),
        )
        .select_from(WorkingMemoryRecord)
        .join(hypotheses, true())
        .join(references, true())
        .where(WorkingMemoryRecord.run_id == scope.run_id)
        .order_by(
            cast(hypotheses.c.key, Integer),
            hypotheses.c.key,
            cast(references.c.key, Integer),
            references.c.key,
        )
    )
    return await _bounded_rows(session, statement, limit)


async def _load_findings(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
    *,
    artifact_ids: set[str],
    execution_ids: set[str],
    decision_ids: set[str],
) -> tuple[tuple[GraphFindingSource, ...], GraphSourceCoverage]:
    root_statement = (
        select(
            FindingRecord.id.label("finding_id"),
            FindingRecord.status,
            FindingRecord.severity,
        )
        .where(FindingRecord.run_id == scope.run_id)
        .order_by(FindingRecord.id)
    )
    roots = await _bounded_rows(session, root_statement, limit)
    evidence = _json_each(FindingRecord.evidence_json, None, "graph_finding_evidence")
    evidence_statement = (
        select(
            FindingRecord.id.label("finding_id"),
            evidence.c.key.label("evidence_order"),
            func.json_extract(evidence.c.value, "$.artifact_id").label("artifact_id"),
            func.json_extract(evidence.c.value, "$.execution_id").label("execution_id"),
            func.coalesce(
                func.json_extract(evidence.c.value, "$.user_decision_ref"),
                func.json_extract(evidence.c.value, "$.user_decision_id"),
            ).label("user_decision_ref"),
        )
        .select_from(FindingRecord)
        .join(evidence, true())
        .where(FindingRecord.run_id == scope.run_id)
        .order_by(FindingRecord.id, cast(evidence.c.key, Integer), evidence.c.key)
    )
    evidence_rows = await _bounded_rows(session, evidence_statement, limit)
    root_ids = {_required_string(row, "finding_id") for row in roots.rows}
    incomplete: set[str] = set()
    if evidence_rows.truncated and evidence_rows.rows:
        candidate = _required_string(evidence_rows.rows[-1], "finding_id")
        if candidate in root_ids:
            incomplete.add(candidate)
    artifacts_by_finding: defaultdict[str, list[str]] = defaultdict(list)
    executions_by_finding: defaultdict[str, list[str]] = defaultdict(list)
    decisions_by_finding: defaultdict[str, list[str]] = defaultdict(list)
    for row in evidence_rows.rows:
        finding_id = _required_string(row, "finding_id")
        if finding_id not in root_ids or finding_id in incomplete:
            continue
        _append_allowed(artifacts_by_finding[finding_id], row["artifact_id"], artifact_ids)
        _append_allowed(executions_by_finding[finding_id], row["execution_id"], execution_ids)
        _append_allowed(
            decisions_by_finding[finding_id],
            row["user_decision_ref"],
            decision_ids,
        )
    result = tuple(
        GraphFindingSource(
            finding_id=finding_id,
            run_id=scope.run_id,
            status=_required_string(row, "status"),
            severity=_required_string(row, "severity"),
            artifact_ids=tuple(dict.fromkeys(artifacts_by_finding[finding_id])),
            execution_ids=tuple(dict.fromkeys(executions_by_finding[finding_id])),
            user_decision_refs=tuple(dict.fromkeys(decisions_by_finding[finding_id])),
        )
        for row in roots.rows
        if (finding_id := _required_string(row, "finding_id")) not in incomplete
    )
    truncated = roots.truncated or evidence_rows.truncated
    return result, _coverage(
        "findings",
        scanned=limit + 1 if truncated else len(result),
        limit=limit,
        truncated=truncated,
    )


async def _load_engagement_facts(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
    *,
    session_ids: set[str],
    execution_ids: set[str],
    artifact_ids: set[str],
) -> tuple[tuple[GraphEngagementFactSource, ...], GraphSourceCoverage]:
    scope_runs = _json_each(
        EngagementFactRecord.source_run_ids_json,
        None,
        "graph_engagement_fact_scope_runs",
    )
    root_statement = (
        select(
            EngagementFactRecord.id.label("fact_id"),
            EngagementFactRecord.status,
        )
        .select_from(EngagementFactRecord)
        .join(scope_runs, scope_runs.c.value == scope.run_id)
        .where(EngagementFactRecord.engagement_id == scope.engagement_id)
        .distinct()
        .order_by(EngagementFactRecord.id)
    )
    roots = await _bounded_rows(session, root_statement, limit)
    lineage_statement = _engagement_fact_lineage_statement(scope)
    lineage = await _bounded_rows(session, lineage_statement, limit)
    root_ids = {_required_string(row, "fact_id") for row in roots.rows}
    by_fact: defaultdict[str, defaultdict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in lineage.rows:
        fact_id = _required_string(row, "owner_id")
        if fact_id not in root_ids:
            continue
        kind = _required_string(row, "kind")
        reference_id = _required_string(row, "reference_id")
        by_fact[fact_id][kind].append(reference_id)
    result: list[GraphEngagementFactSource] = []
    for row in roots.rows:
        fact_id = _required_string(row, "fact_id")
        references = by_fact[fact_id]
        source_runs = tuple(dict.fromkeys(references["run"]))
        source_sessions, unresolved_sessions = _resolved_refs(
            references["session"],
            session_ids,
        )
        source_executions, unresolved_executions = _resolved_refs(
            references["execution"],
            execution_ids,
        )
        source_artifacts, unresolved_artifacts = _resolved_refs(
            references["artifact"],
            artifact_ids,
        )
        cross_run = any(run_id != scope.run_id for run_id in source_runs)
        result.append(
            GraphEngagementFactSource(
                id=fact_id,
                engagement_id=scope.engagement_id,
                status=_required_string(row, "status"),
                source_run_ids=(scope.run_id,),
                source_session_ids=source_sessions,
                source_execution_ids=source_executions,
                artifact_ids=source_artifacts,
                unresolved=(
                    lineage.truncated
                    or cross_run
                    or unresolved_sessions
                    or unresolved_executions
                    or unresolved_artifacts
                ),
            )
        )
    truncated = roots.truncated or lineage.truncated
    return tuple(result), _coverage(
        "engagement_facts",
        scanned=limit + 1 if truncated else len(result),
        limit=limit,
        truncated=truncated,
    )


def _engagement_fact_lineage_statement(scope: GraphScope) -> Any:
    parts = []
    paths = (
        ("run", EngagementFactRecord.source_run_ids_json),
        ("session", EngagementFactRecord.source_session_ids_json),
        ("execution", EngagementFactRecord.source_execution_ids_json),
        ("artifact", EngagementFactRecord.artifact_ids_json),
    )
    for index, (kind, column) in enumerate(paths):
        scope_runs = _json_each(
            EngagementFactRecord.source_run_ids_json,
            None,
            f"graph_engagement_lineage_scope_{index}",
        )
        references = _json_each(
            column,
            None,
            f"graph_engagement_lineage_{kind}",
        )
        parts.append(
            select(
                EngagementFactRecord.id.label("owner_id"),
                literal(kind).label("kind"),
                references.c.key.label("reference_order"),
                references.c.value.label("reference_id"),
            )
            .select_from(EngagementFactRecord)
            .join(scope_runs, scope_runs.c.value == scope.run_id)
            .join(references, true())
            .where(EngagementFactRecord.engagement_id == scope.engagement_id)
        )
    lineage = union_all(*parts).subquery("graph_engagement_fact_lineage")
    return select(
        lineage.c.owner_id,
        lineage.c.kind,
        lineage.c.reference_id,
    ).order_by(
        lineage.c.owner_id,
        lineage.c.kind,
        cast(lineage.c.reference_order, Integer),
        lineage.c.reference_order,
    )


async def _load_fact_relations(
    session: AsyncSession,
    scope: GraphScope,
    limit: int,
    *,
    fact_ids: set[str],
    session_ids: set[str],
    execution_ids: set[str],
    artifact_ids: set[str],
) -> tuple[tuple[GraphFactRelationSource, ...], GraphSourceCoverage]:
    root_statement = (
        select(
            FactRelationRecord.id.label("relation_id"),
            FactRelationRecord.source_fact_id,
            FactRelationRecord.target_fact_id,
            FactRelationRecord.relation_type,
            FactRelationRecord.source_session_id,
        )
        .where(
            FactRelationRecord.engagement_id == scope.engagement_id,
            FactRelationRecord.source_run_id == scope.run_id,
        )
        .order_by(FactRelationRecord.id)
    )
    roots = await _bounded_rows(session, root_statement, limit)
    lineage_statement = _fact_relation_lineage_statement(scope)
    lineage = await _bounded_rows(session, lineage_statement, limit)
    root_ids = {_required_string(row, "relation_id") for row in roots.rows}
    by_relation: defaultdict[str, defaultdict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in lineage.rows:
        relation_id = _required_string(row, "owner_id")
        if relation_id not in root_ids:
            continue
        by_relation[relation_id][_required_string(row, "kind")].append(
            _required_string(row, "reference_id")
        )
    result: list[GraphFactRelationSource] = []
    for row in roots.rows:
        relation_id = _required_string(row, "relation_id")
        source_fact_id = _required_string(row, "source_fact_id")
        target_fact_id = _required_string(row, "target_fact_id")
        source_session_id = _optional_string(row, "source_session_id")
        resolved_session = source_session_id if source_session_id in session_ids else None
        source_executions, unresolved_executions = _resolved_refs(
            by_relation[relation_id]["execution"],
            execution_ids,
        )
        source_artifacts, unresolved_artifacts = _resolved_refs(
            by_relation[relation_id]["artifact"],
            artifact_ids,
        )
        result.append(
            GraphFactRelationSource(
                id=relation_id,
                engagement_id=scope.engagement_id,
                source_fact_id=source_fact_id,
                target_fact_id=target_fact_id,
                relation_type=_required_string(row, "relation_type"),
                source_run_id=scope.run_id,
                source_session_id=resolved_session,
                source_execution_ids=source_executions,
                artifact_ids=source_artifacts,
                unresolved=(
                    lineage.truncated
                    or source_fact_id not in fact_ids
                    or target_fact_id not in fact_ids
                    or (source_session_id is not None and resolved_session is None)
                    or unresolved_executions
                    or unresolved_artifacts
                ),
            )
        )
    truncated = roots.truncated or lineage.truncated
    return tuple(result), _coverage(
        "fact_relations",
        scanned=limit + 1 if truncated else len(result),
        limit=limit,
        truncated=truncated,
    )


def _fact_relation_lineage_statement(scope: GraphScope) -> Any:
    parts = []
    paths = (
        ("execution", FactRelationRecord.source_execution_ids_json),
        ("artifact", FactRelationRecord.artifact_ids_json),
    )
    for kind, column in paths:
        references = _json_each(column, None, f"graph_relation_lineage_{kind}")
        parts.append(
            select(
                FactRelationRecord.id.label("owner_id"),
                literal(kind).label("kind"),
                references.c.key.label("reference_order"),
                references.c.value.label("reference_id"),
            )
            .select_from(FactRelationRecord)
            .join(references, true())
            .where(
                FactRelationRecord.engagement_id == scope.engagement_id,
                FactRelationRecord.source_run_id == scope.run_id,
            )
        )
    lineage = union_all(*parts).subquery("graph_fact_relation_lineage")
    return select(
        lineage.c.owner_id,
        lineage.c.kind,
        lineage.c.reference_id,
    ).order_by(
        lineage.c.owner_id,
        lineage.c.kind,
        cast(lineage.c.reference_order, Integer),
        lineage.c.reference_order,
    )


async def _bounded_rows(
    session: AsyncSession,
    statement: Any,
    limit: int,
) -> _BoundedRows:
    rows = tuple((await session.execute(statement.limit(limit + 1))).mappings())
    return _BoundedRows(
        rows=rows[:limit],
        scanned=len(rows),
        truncated=len(rows) > limit,
    )


def _coverage_from_bounded(
    source: str,
    limit: int,
    bounded: _BoundedRows,
) -> GraphSourceCoverage:
    return _coverage(
        source,
        scanned=bounded.scanned,
        limit=limit,
        truncated=bounded.truncated,
    )


def _coverage(
    source: str,
    *,
    scanned: int,
    limit: int,
    truncated: bool,
) -> GraphSourceCoverage:
    normalized = limit + 1 if truncated else scanned
    return GraphSourceCoverage(
        source=source,
        scanned=normalized,
        limit=limit,
        truncated=truncated,
    )


def _ordered_coverage(
    *items: GraphSourceCoverage,
) -> tuple[GraphSourceCoverage, ...]:
    by_source = {item.source: item for item in items}
    return tuple(by_source[name] for name in _COVERAGE_ORDER if name in by_source)


def _json_each(expression: Any, path: str | None, alias: str) -> Any:
    arguments = (expression,) if path is None else (expression, path)
    return func.json_each(*arguments).table_valued("key", "value").alias(alias)


def _group_valid_references(
    rows: tuple[Mapping[str, object], ...],
    allowed: set[str],
) -> dict[str, tuple[str, ...]]:
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for row in rows:
        owner_id = _required_string(row, "hypothesis_id")
        reference_id = _required_string(row, "reference_id")
        if reference_id in allowed:
            grouped[owner_id].append(reference_id)
    return {key: tuple(dict.fromkeys(values)) for key, values in grouped.items()}


def _append_allowed(values: list[str], value: object, allowed: set[str]) -> None:
    if type(value) is str and value and value in allowed:
        values.append(value)


def _allow_reference(value: str | None, allowed: set[str]) -> str | None:
    return value if value in allowed else None


def _resolved_refs(values: list[str], allowed: set[str]) -> tuple[tuple[str, ...], bool]:
    unique = tuple(dict.fromkeys(values))
    resolved = tuple(value for value in unique if value in allowed)
    return resolved, len(resolved) != len(unique)


def _deduplicate[ItemT](items: list[ItemT], *, key: Any) -> list[ItemT]:
    result: list[ItemT] = []
    seen: set[object] = set()
    for item in items:
        identity = key(item)
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def _required_string(row: Mapping[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str or not value:
        raise GraphSourceContractError(f"Graph metadata field {key!r} is invalid")
    return value


def _optional_string(row: Mapping[str, object], key: str) -> str | None:
    value = row[key]
    if value is None:
        return None
    if type(value) is not str or not value:
        raise GraphSourceContractError(f"Graph metadata field {key!r} is invalid")
    return value


def _required_positive_int(row: Mapping[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int or value < 1:
        raise GraphSourceContractError(f"Graph metadata field {key!r} is invalid")
    return value
