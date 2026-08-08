"""Deterministic Reducer for the durable Reasoning Graph."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
)
from riftx.application.ports import RunRepository
from riftx.domain.base import new_id, utc_now
from riftx.evidence import Evidence, EvidenceKind, EvidenceLedgerRepository
from riftx.reasoning import (
    ReasoningCreatorType,
    ReasoningEdge,
    ReasoningGraph,
    ReasoningGraphRepository,
    ReasoningNode,
    ReasoningNodeKind,
    ReasoningNodeStatus,
    ReasoningRelationType,
    ReproductionContract,
)
from riftx.runtime.types import AgentSession
from riftx.tasks import TaskGraphRepository

_DIRECT_FINDING_EVIDENCE = {
    EvidenceKind.EXECUTION_OUTPUT,
    EvidenceKind.ARTIFACT_SPAN,
    EvidenceKind.HTTP_REQUEST_RESPONSE,
    EvidenceKind.BROWSER_OBSERVATION,
    EvidenceKind.CODE_LOCATION,
    EvidenceKind.CODE_FLOW,
    EvidenceKind.SCANNER_SIGNAL,
    EvidenceKind.DETERMINISTIC_PARSER_RESULT,
}

_STATUS_TRANSITIONS = {
    ReasoningNodeKind.FACT_CANDIDATE: {
        ReasoningNodeStatus.CANDIDATE: {ReasoningNodeStatus.REJECTED},
    },
    ReasoningNodeKind.CONFIRMED_FACT: {
        ReasoningNodeStatus.CONFIRMED: {ReasoningNodeStatus.INVALIDATED},
    },
    ReasoningNodeKind.HYPOTHESIS: {
        ReasoningNodeStatus.UNVERIFIED: {
            ReasoningNodeStatus.INVESTIGATING,
            ReasoningNodeStatus.SUPPORTED,
            ReasoningNodeStatus.REJECTED,
            ReasoningNodeStatus.INVALIDATED,
        },
        ReasoningNodeStatus.INVESTIGATING: {
            ReasoningNodeStatus.SUPPORTED,
            ReasoningNodeStatus.REJECTED,
            ReasoningNodeStatus.INVALIDATED,
        },
        ReasoningNodeStatus.SUPPORTED: {
            ReasoningNodeStatus.INVESTIGATING,
            ReasoningNodeStatus.REJECTED,
            ReasoningNodeStatus.INVALIDATED,
        },
    },
    ReasoningNodeKind.VULNERABILITY_CANDIDATE: {
        ReasoningNodeStatus.CANDIDATE: {ReasoningNodeStatus.REJECTED},
    },
    ReasoningNodeKind.FINDING: {
        ReasoningNodeStatus.CANDIDATE: {
            ReasoningNodeStatus.CONFIRMED,
            ReasoningNodeStatus.FALSE_POSITIVE,
        },
        ReasoningNodeStatus.CONFIRMED: {
            ReasoningNodeStatus.RESOLVED,
            ReasoningNodeStatus.FALSE_POSITIVE,
        },
    },
}


class AgentSessionReader(Protocol):
    async def get(self, session_id: str) -> AgentSession | None: ...


@dataclass(frozen=True, slots=True)
class TransitionReasoningNode:
    run_id: str
    node_id: str
    expected_graph_version: int
    expected_node_version: int
    target_status: ReasoningNodeStatus
    evidence_ids: tuple[str, ...] | None = None
    reproduction_contract: ReproductionContract | None = None


class QueryReasoningGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=64)
    node_ids: tuple[str, ...] = Field(default=(), max_length=100)
    kinds: tuple[ReasoningNodeKind, ...] = Field(default=(), max_length=8)
    statuses: tuple[ReasoningNodeStatus, ...] = Field(default=(), max_length=16)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=100)
    query: str = Field(default="", max_length=2_000)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
    edge_limit: int = Field(default=100, ge=0, le=200)


class ReasoningGraphQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    graph_version: int = Field(ge=0)
    total_matching_nodes: int = Field(ge=0)
    offset: int = Field(ge=0)
    nodes: tuple[ReasoningNode, ...] = ()
    edges: tuple[ReasoningEdge, ...] = ()
    nodes_truncated: bool = False
    edges_truncated: bool = False


class ReasoningGraphApplicationService:
    def __init__(
        self,
        *,
        runs: RunRepository,
        sessions: AgentSessionReader,
        tasks: TaskGraphRepository,
        evidence: EvidenceLedgerRepository,
        graphs: ReasoningGraphRepository,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._tasks = tasks
        self._evidence = evidence
        self._graphs = graphs

    async def create_node(
        self,
        node: ReasoningNode,
        *,
        expected_graph_version: int,
    ) -> ReasoningGraph:
        if node.kind in {
            ReasoningNodeKind.CONFIRMED_FACT,
            ReasoningNodeKind.PROOF,
            ReasoningNodeKind.NEGATIVE_RESULT,
        } or node.status in {ReasoningNodeStatus.PROMOTED, ReasoningNodeStatus.CONFIRMED}:
            raise _conflict(
                "reasoning_atomic_transition_required",
                "This Reasoning Node must be created by its atomic Reducer transition",
            )
        await self._validate_node(node)
        graph = await self._graphs.get(node.run_id)
        if graph is None:
            if expected_graph_version != 0:
                raise _version_conflict(expected_graph_version, 0)
            try:
                return await self._graphs.create(
                    ReasoningGraph(run_id=node.run_id, nodes=[node])
                )
            except RepositoryConflictError as exc:
                raise _conflict(
                    "reasoning_graph_write_conflict",
                    "Reasoning Graph changed before the Reducer committed",
                ) from exc
        _require_graph_version(graph, expected_graph_version)
        return await self._save(
            _replace_graph(graph, nodes=[*graph.nodes, node]),
            expected_version=expected_graph_version,
        )

    async def query(self, command: QueryReasoningGraph) -> ReasoningGraphQueryResult:
        if await self._runs.get(command.run_id) is None:
            raise EntityNotFoundError("Run", command.run_id)
        graph = await self._graphs.get(command.run_id)
        if graph is None:
            return ReasoningGraphQueryResult(
                run_id=command.run_id,
                graph_version=0,
                total_matching_nodes=0,
                offset=command.offset,
            )

        node_ids = set(command.node_ids)
        kinds = set(command.kinds)
        statuses = set(command.statuses)
        evidence_ids = set(command.evidence_ids)
        query = command.query.casefold().strip()
        matching = [
            node
            for node in graph.nodes
            if (not node_ids or node.id in node_ids)
            and (not kinds or node.kind in kinds)
            and (not statuses or node.status in statuses)
            and (command.task_id is None or node.task_id == command.task_id)
            and (not evidence_ids or evidence_ids.intersection(node.evidence_ids))
            and (
                not query
                or query in node.claim.casefold()
                or query
                in json.dumps(
                    node.structured_data,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).casefold()
            )
        ]
        selected = matching[command.offset : command.offset + command.limit]
        selected_ids = {node.id for node in selected}
        related_edges = [
            edge
            for edge in graph.edges
            if edge.source_node_id in selected_ids or edge.target_node_id in selected_ids
        ]
        return ReasoningGraphQueryResult(
            run_id=command.run_id,
            graph_version=graph.version,
            total_matching_nodes=len(matching),
            offset=command.offset,
            nodes=tuple(selected),
            edges=tuple(related_edges[: command.edge_limit]),
            nodes_truncated=command.offset + len(selected) < len(matching),
            edges_truncated=len(related_edges) > command.edge_limit,
        )

    async def create_edge(
        self,
        edge: ReasoningEdge,
        *,
        expected_graph_version: int,
    ) -> ReasoningGraph:
        graph = await self._require_graph(edge.run_id)
        _require_graph_version(graph, expected_graph_version)
        await self._require_evidence(edge.run_id, edge.evidence_ids)
        return await self._save(
            _replace_graph(graph, edges=[*graph.edges, edge]),
            expected_version=expected_graph_version,
        )

    async def promote_fact(
        self,
        *,
        run_id: str,
        candidate_id: str,
        confirmed_fact: ReasoningNode,
        expected_graph_version: int,
        expected_candidate_version: int,
        edge_id: str | None = None,
    ) -> ReasoningGraph:
        graph = await self._require_graph(run_id)
        _require_graph_version(graph, expected_graph_version)
        candidate = _require_node(graph, candidate_id)
        _require_node_version(candidate, expected_candidate_version)
        if (
            candidate.kind is not ReasoningNodeKind.FACT_CANDIDATE
            or candidate.status is not ReasoningNodeStatus.CANDIDATE
            or confirmed_fact.kind is not ReasoningNodeKind.CONFIRMED_FACT
            or confirmed_fact.status is not ReasoningNodeStatus.CONFIRMED
            or confirmed_fact.run_id != run_id
        ):
            raise _conflict(
                "reasoning_fact_promotion_invalid",
                "Fact promotion requires a candidate and a Confirmed Fact in one Run",
            )
        await self._validate_node(confirmed_fact)
        now = utc_now()
        promoted = candidate.model_copy(
            update={
                "status": ReasoningNodeStatus.PROMOTED,
                "version": candidate.version + 1,
                "updated_at": now,
            }
        )
        lineage = _derived_edge(
            run_id,
            source_id=candidate.id,
            target_id=confirmed_fact.id,
            evidence_ids=confirmed_fact.evidence_ids,
            edge_id=edge_id,
        )
        return await self._save(
            _replace_graph(
                graph,
                nodes=[
                    *(promoted if node.id == candidate.id else node for node in graph.nodes),
                    confirmed_fact,
                ],
                edges=[*graph.edges, lineage],
            ),
            expected_version=expected_graph_version,
        )

    async def promote_vulnerability(
        self,
        *,
        run_id: str,
        candidate_id: str,
        finding: ReasoningNode,
        expected_graph_version: int,
        expected_candidate_version: int,
        edge_id: str | None = None,
    ) -> ReasoningGraph:
        graph = await self._require_graph(run_id)
        _require_graph_version(graph, expected_graph_version)
        candidate = _require_node(graph, candidate_id)
        _require_node_version(candidate, expected_candidate_version)
        if (
            candidate.kind is not ReasoningNodeKind.VULNERABILITY_CANDIDATE
            or candidate.status is not ReasoningNodeStatus.CANDIDATE
            or finding.kind is not ReasoningNodeKind.FINDING
            or finding.status is not ReasoningNodeStatus.CANDIDATE
            or finding.run_id != run_id
        ):
            raise _conflict(
                "reasoning_vulnerability_promotion_invalid",
                "Vulnerability promotion requires a candidate and Finding Candidate",
            )
        await self._validate_node(finding)
        now = utc_now()
        promoted = candidate.model_copy(
            update={
                "status": ReasoningNodeStatus.PROMOTED,
                "version": candidate.version + 1,
                "updated_at": now,
            }
        )
        lineage = _derived_edge(
            run_id,
            source_id=candidate.id,
            target_id=finding.id,
            evidence_ids=finding.evidence_ids,
            edge_id=edge_id,
        )
        return await self._save(
            _replace_graph(
                graph,
                nodes=[
                    *(promoted if node.id == candidate.id else node for node in graph.nodes),
                    finding,
                ],
                edges=[*graph.edges, lineage],
            ),
            expected_version=expected_graph_version,
        )

    async def record_proof(
        self,
        proof: ReasoningNode,
        *,
        finding_id: str,
        expected_graph_version: int,
        edge_id: str | None = None,
    ) -> ReasoningGraph:
        graph = await self._require_graph(proof.run_id)
        _require_graph_version(graph, expected_graph_version)
        finding = _require_node(graph, finding_id)
        if (
            proof.kind is not ReasoningNodeKind.PROOF
            or proof.status is not ReasoningNodeStatus.VALIDATED
            or finding.kind is not ReasoningNodeKind.FINDING
        ):
            raise _conflict(
                "reasoning_proof_invalid",
                "Validated Proof must target a Finding",
            )
        await self._validate_node(proof)
        validates = ReasoningEdge(
            id=edge_id or new_id(),
            run_id=proof.run_id,
            source_node_id=proof.id,
            target_node_id=finding.id,
            relation_type=ReasoningRelationType.VALIDATES,
            evidence_ids=proof.evidence_ids,
            creator_type=ReasoningCreatorType.REDUCER,
            created_by="reasoning-reducer",
        )
        return await self._save(
            _replace_graph(
                graph,
                nodes=[*graph.nodes, proof],
                edges=[*graph.edges, validates],
            ),
            expected_version=expected_graph_version,
        )

    async def record_negative_result(
        self,
        negative_result: ReasoningNode,
        *,
        invalidated_node_id: str,
        expected_graph_version: int,
        edge_id: str | None = None,
    ) -> ReasoningGraph:
        graph = await self._require_graph(negative_result.run_id)
        _require_graph_version(graph, expected_graph_version)
        target = _require_node(graph, invalidated_node_id)
        if (
            negative_result.kind is not ReasoningNodeKind.NEGATIVE_RESULT
            or negative_result.status is not ReasoningNodeStatus.RECORDED
            or target.kind is ReasoningNodeKind.NEGATIVE_RESULT
        ):
            raise _conflict(
                "reasoning_negative_result_invalid",
                "Negative Result must invalidate a non-negative claim",
            )
        await self._validate_node(negative_result)
        invalidates = ReasoningEdge(
            id=edge_id or new_id(),
            run_id=negative_result.run_id,
            source_node_id=negative_result.id,
            target_node_id=target.id,
            relation_type=ReasoningRelationType.INVALIDATES,
            evidence_ids=negative_result.evidence_ids,
            creator_type=ReasoningCreatorType.REDUCER,
            created_by="reasoning-reducer",
        )
        return await self._save(
            _replace_graph(
                graph,
                nodes=[*graph.nodes, negative_result],
                edges=[*graph.edges, invalidates],
            ),
            expected_version=expected_graph_version,
        )

    async def transition_node(
        self,
        command: TransitionReasoningNode,
    ) -> ReasoningGraph:
        graph = await self._require_graph(command.run_id)
        _require_graph_version(graph, command.expected_graph_version)
        current = _require_node(graph, command.node_id)
        if current.version != command.expected_node_version:
            raise _conflict(
                "reasoning_node_version_conflict",
                "Reasoning Node version changed before transition",
            )
        allowed = _STATUS_TRANSITIONS.get(current.kind, {}).get(current.status, set())
        if command.target_status not in allowed:
            raise _conflict(
                "reasoning_status_transition_invalid",
                "Reasoning Node status transition is invalid",
            )
        evidence_ids = command.evidence_ids or current.evidence_ids
        reproduction = command.reproduction_contract or current.reproduction_contract
        replacement = ReasoningNode.model_validate(
            {
                **current.model_dump(mode="json"),
                "status": command.target_status,
                "evidence_ids": evidence_ids,
                "reproduction_contract": (
                    reproduction.model_dump(mode="json")
                    if reproduction is not None
                    else None
                ),
                "version": current.version + 1,
                "updated_at": utc_now(),
            }
        )
        evidence = await self._validate_node(replacement)
        if (
            replacement.kind is ReasoningNodeKind.FINDING
            and replacement.status is ReasoningNodeStatus.CONFIRMED
        ):
            _require_direct_finding_evidence(evidence)
        return await self._save(
            _replace_graph(
                graph,
                nodes=[
                    replacement if node.id == replacement.id else node
                    for node in graph.nodes
                ],
            ),
            expected_version=command.expected_graph_version,
        )

    async def _validate_node(self, node: ReasoningNode) -> tuple[Evidence, ...]:
        run = await self._runs.get(node.run_id)
        if run is None:
            raise EntityNotFoundError("Run", node.run_id)
        if node.session_id is not None:
            session = await self._sessions.get(node.session_id)
            if session is None or session.run_id != node.run_id:
                raise _conflict(
                    "reasoning_session_owner_mismatch",
                    "Reasoning Node Session does not belong to its Run",
                )
        if node.task_id is not None:
            graph = await self._tasks.get(node.run_id)
            if graph is None or node.task_id not in {task.id for task in graph.tasks}:
                raise _conflict(
                    "reasoning_task_owner_mismatch",
                    "Reasoning Node Task does not belong to its Run",
                )
        evidence = await self._require_evidence(node.run_id, node.evidence_ids)
        if node.session_id is not None and any(
            item.session_id not in {None, node.session_id} for item in evidence
        ):
            raise _conflict(
                "reasoning_evidence_session_mismatch",
                "Reasoning Node Evidence belongs to another Session",
            )
        if node.task_id is not None and any(
            item.task_id not in {None, node.task_id} for item in evidence
        ):
            raise _conflict(
                "reasoning_evidence_task_mismatch",
                "Reasoning Node Evidence belongs to another Task",
            )
        return evidence

    async def _require_evidence(
        self,
        run_id: str,
        evidence_ids: tuple[str, ...],
    ) -> tuple[Evidence, ...]:
        if not evidence_ids:
            return ()
        evidence = await self._evidence.list_by_ids(run_id, evidence_ids)
        if tuple(item.id for item in evidence) != evidence_ids:
            raise _conflict(
                "reasoning_evidence_missing",
                "Reasoning Graph references Evidence outside its Run",
            )
        return evidence

    async def _require_graph(self, run_id: str) -> ReasoningGraph:
        graph = await self._graphs.get(run_id)
        if graph is None:
            raise EntityNotFoundError("ReasoningGraph", run_id)
        return graph

    async def _save(
        self,
        graph: ReasoningGraph,
        *,
        expected_version: int,
    ) -> ReasoningGraph:
        try:
            return await self._graphs.save(graph, expected_version=expected_version)
        except RepositoryConflictError as exc:
            raise _conflict(
                "reasoning_graph_write_conflict",
                "Reasoning Graph changed before the Reducer committed",
            ) from exc


def _replace_graph(
    graph: ReasoningGraph,
    *,
    nodes: list[ReasoningNode] | None = None,
    edges: list[ReasoningEdge] | None = None,
) -> ReasoningGraph:
    return ReasoningGraph(
        schema_version=graph.schema_version,
        run_id=graph.run_id,
        version=graph.version + 1,
        nodes=nodes if nodes is not None else graph.nodes,
        edges=edges if edges is not None else graph.edges,
        created_at=graph.created_at,
        updated_at=utc_now(),
    )


def _require_node(graph: ReasoningGraph, node_id: str) -> ReasoningNode:
    node = next((item for item in graph.nodes if item.id == node_id), None)
    if node is None:
        raise EntityNotFoundError("ReasoningNode", node_id)
    return node


def _require_graph_version(graph: ReasoningGraph, expected_version: int) -> None:
    if graph.version != expected_version:
        raise _version_conflict(expected_version, graph.version)


def _require_node_version(node: ReasoningNode, expected_version: int) -> None:
    if node.version != expected_version:
        raise _conflict(
            "reasoning_node_version_conflict",
            "Reasoning Node version changed before transition",
        )


def _version_conflict(expected: int, actual: int) -> ApplicationConflictError:
    return _conflict(
        "reasoning_graph_version_conflict",
        f"Reasoning Graph version conflict: expected {expected}, found {actual}",
    )


def _derived_edge(
    run_id: str,
    *,
    source_id: str,
    target_id: str,
    evidence_ids: tuple[str, ...],
    edge_id: str | None,
) -> ReasoningEdge:
    return ReasoningEdge(
        id=edge_id or new_id(),
        run_id=run_id,
        source_node_id=source_id,
        target_node_id=target_id,
        relation_type=ReasoningRelationType.DERIVED_FROM,
        evidence_ids=evidence_ids,
        creator_type=ReasoningCreatorType.REDUCER,
        created_by="reasoning-reducer",
    )


def _require_direct_finding_evidence(evidence: tuple[Evidence, ...]) -> None:
    if not any(item.kind in _DIRECT_FINDING_EVIDENCE for item in evidence):
        raise _conflict(
            "reasoning_finding_direct_evidence_required",
            "External research, CVE pages, PoC descriptions, and user decisions "
            "cannot independently confirm a target Finding",
        )


def _conflict(code: str, message: str) -> ApplicationConflictError:
    return ApplicationConflictError(code, message)
