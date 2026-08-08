"""SQLAlchemy persistence for the durable Reasoning Graph aggregate."""

from __future__ import annotations

from collections import defaultdict

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from riftx.application.errors import (
    RepositoryConflictError,
    RepositoryIntegrityError,
)
from riftx.reasoning import ReasoningEdge, ReasoningGraph, ReasoningNode

from .orm import (
    ReasoningEdgeEvidenceRecord,
    ReasoningEdgeRecord,
    ReasoningGraphRecord,
    ReasoningNodeEvidenceRecord,
    ReasoningNodeRecord,
    RunRecord,
)
from .transactions import SessionFactory, consistent_read, serialized_write


class SQLAlchemyReasoningGraphRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, graph: ReasoningGraph) -> ReasoningGraph:
        if graph.version != 1 or any(node.version != 1 for node in graph.nodes):
            raise RepositoryConflictError(
                "new Reasoning Graphs and Nodes must start at version 1"
            )
        try:
            async with serialized_write(self._session_factory) as session:
                run = await session.get(RunRecord, graph.run_id, with_for_update=True)
                if run is None:
                    raise RepositoryConflictError(
                        f"cannot create Reasoning Graph for unknown Run {graph.run_id!r}"
                    )
                if await session.get(ReasoningGraphRecord, graph.run_id) is not None:
                    raise RepositoryConflictError(
                        f"Reasoning Graph for Run {graph.run_id!r} already exists"
                    )
                session.add(_graph_record(graph))
                session.add_all(_node_record(node) for node in graph.nodes)
                await session.flush()
                session.add_all(_edge_record(edge) for edge in graph.edges)
                await session.flush()
                session.add_all(
                    ReasoningNodeEvidenceRecord(
                        run_id=node.run_id,
                        node_id=node.id,
                        evidence_id=evidence_id,
                        ordinal=ordinal,
                    )
                    for node in graph.nodes
                    for ordinal, evidence_id in enumerate(node.evidence_ids)
                )
                session.add_all(
                    ReasoningEdgeEvidenceRecord(
                        run_id=edge.run_id,
                        edge_id=edge.id,
                        evidence_id=evidence_id,
                        ordinal=ordinal,
                    )
                    for edge in graph.edges
                    for ordinal, evidence_id in enumerate(edge.evidence_ids)
                )
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not persist Reasoning Graph for Run {graph.run_id!r}"
            ) from exc
        return graph

    async def get(self, run_id: str) -> ReasoningGraph | None:
        async with consistent_read(self._session_factory) as session:
            graph = await session.get(ReasoningGraphRecord, run_id)
            if graph is None:
                return None
            return await _load_graph(session, graph)

    async def save(
        self,
        graph: ReasoningGraph,
        *,
        expected_version: int,
    ) -> ReasoningGraph:
        if graph.version != expected_version + 1:
            raise ValueError("Reasoning Graph save must advance exactly one version")
        try:
            async with serialized_write(self._session_factory) as session:
                record = await session.get(
                    ReasoningGraphRecord,
                    graph.run_id,
                    with_for_update=True,
                )
                if record is None:
                    raise RepositoryConflictError(
                        f"Reasoning Graph for Run {graph.run_id!r} does not exist"
                    )
                if record.version != expected_version:
                    raise RepositoryConflictError(
                        f"Reasoning Graph version conflict: expected {expected_version}, "
                        f"found {record.version}"
                    )
                current = await _load_graph(session, record)
                if (
                    graph.schema_version != current.schema_version
                    or graph.created_at != current.created_at
                ):
                    raise RepositoryConflictError(
                        "Reasoning Graph immutable identity changed"
                    )
                if graph.updated_at <= current.updated_at:
                    raise RepositoryConflictError(
                        "Reasoning Graph updated_at must advance"
                    )
                current_nodes = {node.id: node for node in current.nodes}
                replacement_nodes = {node.id: node for node in graph.nodes}
                current_edges = {edge.id: edge for edge in current.edges}
                replacement_edges = {edge.id: edge for edge in graph.edges}
                if set(current_nodes) - set(replacement_nodes):
                    raise RepositoryConflictError("Reasoning Graph save cannot delete Nodes")
                if set(current_edges) - set(replacement_edges):
                    raise RepositoryConflictError("Reasoning Graph save cannot delete Edges")

                for node_id, current_node in current_nodes.items():
                    replacement = replacement_nodes[node_id]
                    _require_same_node_identity(current_node, replacement)
                    node_record = await session.get(ReasoningNodeRecord, node_id)
                    assert node_record is not None
                    changed = _node_mutable_state(replacement) != _node_mutable_state(
                        current_node
                    )
                    expected_node_version = current_node.version + int(changed)
                    if replacement.version != expected_node_version:
                        raise RepositoryConflictError(
                            f"Reasoning Node {node_id!r} version does not match its mutation"
                        )
                    if not changed:
                        if replacement.updated_at != current_node.updated_at:
                            raise RepositoryConflictError(
                                f"Reasoning Node {node_id!r} changed only its updated_at"
                            )
                        continue
                    if replacement.updated_at < current_node.updated_at:
                        raise RepositoryConflictError(
                            f"Reasoning Node {node_id!r} updated_at moved backwards"
                        )
                    if not set(current_node.evidence_ids).issubset(
                        replacement.evidence_ids
                    ):
                        raise RepositoryConflictError(
                            f"Reasoning Node {node_id!r} cannot remove Evidence lineage"
                        )
                    node_record.status = replacement.status.value
                    node_record.reproduction_contract_json = (
                        replacement.reproduction_contract.model_dump(mode="json")
                        if replacement.reproduction_contract is not None
                        else None
                    )
                    node_record.version = replacement.version
                    node_record.updated_at = replacement.updated_at
                    await session.execute(
                        delete(ReasoningNodeEvidenceRecord).where(
                            ReasoningNodeEvidenceRecord.run_id == graph.run_id,
                            ReasoningNodeEvidenceRecord.node_id == node_id,
                        )
                    )
                    session.add_all(
                        ReasoningNodeEvidenceRecord(
                            run_id=graph.run_id,
                            node_id=node_id,
                            evidence_id=evidence_id,
                            ordinal=ordinal,
                        )
                        for ordinal, evidence_id in enumerate(replacement.evidence_ids)
                    )

                new_nodes = [
                    node for node in graph.nodes if node.id not in current_nodes
                ]
                if any(node.version != 1 for node in new_nodes):
                    raise RepositoryConflictError("new Reasoning Nodes must start at version 1")
                session.add_all(_node_record(node) for node in new_nodes)
                await session.flush()
                session.add_all(
                    ReasoningNodeEvidenceRecord(
                        run_id=node.run_id,
                        node_id=node.id,
                        evidence_id=evidence_id,
                        ordinal=ordinal,
                    )
                    for node in new_nodes
                    for ordinal, evidence_id in enumerate(node.evidence_ids)
                )

                for edge_id, current_edge in current_edges.items():
                    if replacement_edges[edge_id] != current_edge:
                        raise RepositoryConflictError(
                            f"Reasoning Edge {edge_id!r} is immutable"
                        )
                new_edges = [
                    edge for edge in graph.edges if edge.id not in current_edges
                ]
                session.add_all(_edge_record(edge) for edge in new_edges)
                await session.flush()
                session.add_all(
                    ReasoningEdgeEvidenceRecord(
                        run_id=edge.run_id,
                        edge_id=edge.id,
                        evidence_id=evidence_id,
                        ordinal=ordinal,
                    )
                    for edge in new_edges
                    for ordinal, evidence_id in enumerate(edge.evidence_ids)
                )
                record.version = graph.version
                record.updated_at = graph.updated_at
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not save Reasoning Graph for Run {graph.run_id!r}"
            ) from exc
        return graph


async def _load_graph(
    session: AsyncSession,
    graph: ReasoningGraphRecord,
) -> ReasoningGraph:
    run_id = graph.run_id
    nodes = tuple(
        await session.scalars(
            select(ReasoningNodeRecord)
            .where(ReasoningNodeRecord.run_id == run_id)
            .order_by(ReasoningNodeRecord.created_at, ReasoningNodeRecord.id)
        )
    )
    edges = tuple(
        await session.scalars(
            select(ReasoningEdgeRecord)
            .where(ReasoningEdgeRecord.run_id == run_id)
            .order_by(ReasoningEdgeRecord.created_at, ReasoningEdgeRecord.id)
        )
    )
    node_evidence = tuple(
        await session.scalars(
            select(ReasoningNodeEvidenceRecord)
            .where(ReasoningNodeEvidenceRecord.run_id == run_id)
            .order_by(
                ReasoningNodeEvidenceRecord.node_id,
                ReasoningNodeEvidenceRecord.ordinal,
            )
        )
    )
    edge_evidence = tuple(
        await session.scalars(
            select(ReasoningEdgeEvidenceRecord)
            .where(ReasoningEdgeEvidenceRecord.run_id == run_id)
            .order_by(
                ReasoningEdgeEvidenceRecord.edge_id,
                ReasoningEdgeEvidenceRecord.ordinal,
            )
        )
    )
    return _graph(graph, nodes, edges, node_evidence, edge_evidence)


def _require_same_node_identity(
    current: ReasoningNode,
    replacement: ReasoningNode,
) -> None:
    immutable_fields = (
        "schema_version",
        "id",
        "run_id",
        "session_id",
        "task_id",
        "kind",
        "claim",
        "structured_data",
        "creator_type",
        "created_by",
        "created_at",
    )
    if any(
        getattr(current, field) != getattr(replacement, field)
        for field in immutable_fields
    ):
        raise RepositoryConflictError(
            f"Reasoning Node {current.id!r} immutable identity changed"
        )


def _node_mutable_state(node: ReasoningNode) -> tuple[object, ...]:
    return (
        node.status,
        node.evidence_ids,
        node.reproduction_contract,
    )


def _graph_record(graph: ReasoningGraph) -> ReasoningGraphRecord:
    return ReasoningGraphRecord(
        run_id=graph.run_id,
        schema_version=graph.schema_version,
        version=graph.version,
        created_at=graph.created_at,
        updated_at=graph.updated_at,
    )


def _node_record(node: ReasoningNode) -> ReasoningNodeRecord:
    return ReasoningNodeRecord(
        id=node.id,
        run_id=node.run_id,
        schema_version=node.schema_version,
        session_id=node.session_id,
        task_id=node.task_id,
        kind=node.kind.value,
        status=node.status.value,
        claim=node.claim,
        structured_data_json=node.structured_data,
        reproduction_contract_json=(
            node.reproduction_contract.model_dump(mode="json")
            if node.reproduction_contract is not None
            else None
        ),
        creator_type=node.creator_type.value,
        created_by=node.created_by,
        version=node.version,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


def _edge_record(edge: ReasoningEdge) -> ReasoningEdgeRecord:
    return ReasoningEdgeRecord(
        id=edge.id,
        run_id=edge.run_id,
        schema_version=edge.schema_version,
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        relation_type=edge.relation_type.value,
        creator_type=edge.creator_type.value,
        created_by=edge.created_by,
        created_at=edge.created_at,
    )


def _graph(
    graph: ReasoningGraphRecord,
    nodes: tuple[ReasoningNodeRecord, ...],
    edges: tuple[ReasoningEdgeRecord, ...],
    node_evidence: tuple[ReasoningNodeEvidenceRecord, ...],
    edge_evidence: tuple[ReasoningEdgeEvidenceRecord, ...],
) -> ReasoningGraph:
    node_refs: dict[str, list[str]] = defaultdict(list)
    for node_item in node_evidence:
        node_refs[node_item.node_id].append(node_item.evidence_id)
    edge_refs: dict[str, list[str]] = defaultdict(list)
    for edge_item in edge_evidence:
        edge_refs[edge_item.edge_id].append(edge_item.evidence_id)
    try:
        return ReasoningGraph.model_validate(
            {
                "schema_version": graph.schema_version,
                "run_id": graph.run_id,
                "version": graph.version,
                "nodes": [
                    {
                        "schema_version": node.schema_version,
                        "id": node.id,
                        "run_id": node.run_id,
                        "session_id": node.session_id,
                        "task_id": node.task_id,
                        "kind": node.kind,
                        "status": node.status,
                        "claim": node.claim,
                        "structured_data": node.structured_data_json,
                        "evidence_ids": tuple(node_refs[node.id]),
                        "reproduction_contract": node.reproduction_contract_json,
                        "creator_type": node.creator_type,
                        "created_by": node.created_by,
                        "version": node.version,
                        "created_at": node.created_at,
                        "updated_at": node.updated_at,
                    }
                    for node in nodes
                ],
                "edges": [
                    {
                        "schema_version": edge.schema_version,
                        "id": edge.id,
                        "run_id": edge.run_id,
                        "source_node_id": edge.source_node_id,
                        "target_node_id": edge.target_node_id,
                        "relation_type": edge.relation_type,
                        "evidence_ids": tuple(edge_refs[edge.id]),
                        "creator_type": edge.creator_type,
                        "created_by": edge.created_by,
                        "created_at": edge.created_at,
                    }
                    for edge in edges
                ],
                "created_at": graph.created_at,
                "updated_at": graph.updated_at,
            }
        )
    except (TypeError, ValidationError, ValueError):
        raise RepositoryIntegrityError("ReasoningGraph", graph.run_id) from None
