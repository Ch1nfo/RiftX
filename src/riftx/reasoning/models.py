"""Durable, Evidence-driven Reasoning Graph contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from riftx.domain.base import DomainModel, new_id, utc_now

REASONING_GRAPH_SCHEMA_VERSION: Literal["riftx.reasoning-graph/v1"] = (
    "riftx.reasoning-graph/v1"
)
REPRODUCTION_CONTRACT_SCHEMA_VERSION: Literal["riftx.reproduction-contract/v1"] = (
    "riftx.reproduction-contract/v1"
)
_DIGEST = r"^[0-9a-f]{64}$"


class ReasoningNodeKind(StrEnum):
    OBSERVATION = "observation"
    FACT_CANDIDATE = "fact_candidate"
    CONFIRMED_FACT = "confirmed_fact"
    HYPOTHESIS = "hypothesis"
    VULNERABILITY_CANDIDATE = "vulnerability_candidate"
    FINDING = "finding"
    PROOF = "proof"
    NEGATIVE_RESULT = "negative_result"


class ReasoningNodeStatus(StrEnum):
    RECORDED = "recorded"
    CANDIDATE = "candidate"
    PROMOTED = "promoted"
    CONFIRMED = "confirmed"
    UNVERIFIED = "unverified"
    INVESTIGATING = "investigating"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"
    VALIDATED = "validated"
    FAILED = "failed"


class ReasoningRelationType(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DERIVED_FROM = "derived_from"
    DISCOVERED_ON = "discovered_on"
    VALIDATES = "validates"
    EXPLOITS = "exploits"
    INVALIDATES = "invalidates"
    DEPENDS_ON = "depends_on"


class ReasoningCreatorType(StrEnum):
    AGENT = "agent"
    OPERATOR = "operator"
    SYSTEM = "system"
    REDUCER = "reducer"
    TOOL = "tool"
    PARSER = "parser"
    SCANNER = "scanner"


_ALLOWED_STATUSES = {
    ReasoningNodeKind.OBSERVATION: {ReasoningNodeStatus.RECORDED},
    ReasoningNodeKind.FACT_CANDIDATE: {
        ReasoningNodeStatus.CANDIDATE,
        ReasoningNodeStatus.PROMOTED,
        ReasoningNodeStatus.REJECTED,
    },
    ReasoningNodeKind.CONFIRMED_FACT: {
        ReasoningNodeStatus.CONFIRMED,
        ReasoningNodeStatus.INVALIDATED,
    },
    ReasoningNodeKind.HYPOTHESIS: {
        ReasoningNodeStatus.UNVERIFIED,
        ReasoningNodeStatus.INVESTIGATING,
        ReasoningNodeStatus.SUPPORTED,
        ReasoningNodeStatus.REJECTED,
        ReasoningNodeStatus.INVALIDATED,
    },
    ReasoningNodeKind.VULNERABILITY_CANDIDATE: {
        ReasoningNodeStatus.CANDIDATE,
        ReasoningNodeStatus.PROMOTED,
        ReasoningNodeStatus.REJECTED,
    },
    ReasoningNodeKind.FINDING: {
        ReasoningNodeStatus.CANDIDATE,
        ReasoningNodeStatus.CONFIRMED,
        ReasoningNodeStatus.RESOLVED,
        ReasoningNodeStatus.FALSE_POSITIVE,
    },
    ReasoningNodeKind.PROOF: {
        ReasoningNodeStatus.VALIDATED,
        ReasoningNodeStatus.FAILED,
    },
    ReasoningNodeKind.NEGATIVE_RESULT: {ReasoningNodeStatus.RECORDED},
}


class ReproductionContract(DomainModel):
    schema_version: Literal["riftx.reproduction-contract/v1"] = (
        REPRODUCTION_CONTRACT_SCHEMA_VERSION
    )
    steps: tuple[str, ...] = Field(min_length=1, max_length=100)
    expected_outcome: str = Field(min_length=1, max_length=10_000)
    target_refs: tuple[str, ...] = Field(min_length=1, max_length=100)
    preconditions: tuple[str, ...] = Field(default=(), max_length=100)
    parameters_digest: str = Field(pattern=_DIGEST)

    @field_validator("steps", "target_refs", "preconditions")
    @classmethod
    def validate_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("Reproduction Contract entries must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("Reproduction Contract entries must be unique")
        return values


class ReasoningNode(DomainModel):
    schema_version: Literal["riftx.reasoning-graph/v1"] = REASONING_GRAPH_SCHEMA_VERSION
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    kind: ReasoningNodeKind
    status: ReasoningNodeStatus
    claim: str = Field(min_length=1, max_length=20_000)
    structured_data: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=1_000)
    reproduction_contract: ReproductionContract | None = None
    creator_type: ReasoningCreatorType
    created_by: str = Field(min_length=1, max_length=128)
    version: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Reasoning Node Evidence IDs must be unique")
        return values

    @model_validator(mode="after")
    def validate_node(self) -> Self:
        if self.status not in _ALLOWED_STATUSES[self.kind]:
            raise ValueError(
                f"Reasoning Node status {self.status.value!r} is invalid for {self.kind.value!r}"
            )
        if self.reproduction_contract is not None and self.kind is not ReasoningNodeKind.FINDING:
            raise ValueError("only Finding Nodes may carry a Reproduction Contract")
        if self.kind is ReasoningNodeKind.FINDING and self.status is ReasoningNodeStatus.CONFIRMED:
            if not self.evidence_ids or self.reproduction_contract is None:
                raise ValueError(
                    "Confirmed Finding requires Evidence and a Reproduction Contract"
                )
        if self.kind is not ReasoningNodeKind.HYPOTHESIS and not self.evidence_ids:
            raise ValueError(f"{self.kind.value} Reasoning Node requires Evidence")
        return self


class ReasoningEdge(DomainModel):
    schema_version: Literal["riftx.reasoning-graph/v1"] = REASONING_GRAPH_SCHEMA_VERSION
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    source_node_id: str = Field(min_length=1, max_length=64)
    target_node_id: str = Field(min_length=1, max_length=64)
    relation_type: ReasoningRelationType
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=1_000)
    creator_type: ReasoningCreatorType
    created_by: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Reasoning Edge Evidence IDs must be unique")
        return values

    @model_validator(mode="after")
    def reject_self_edge(self) -> Self:
        if self.source_node_id == self.target_node_id:
            raise ValueError("Reasoning Edge cannot point to itself")
        return self


class ReasoningGraph(DomainModel):
    schema_version: Literal["riftx.reasoning-graph/v1"] = REASONING_GRAPH_SCHEMA_VERSION
    run_id: str = Field(min_length=1, max_length=64)
    version: int = Field(default=1, ge=1)
    nodes: list[ReasoningNode] = Field(default_factory=list)
    edges: list[ReasoningEdge] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Reasoning Graph Node IDs must be unique")
        if any(node.run_id != self.run_id for node in self.nodes):
            raise ValueError("Reasoning Graph Nodes must belong to its Run")
        if len({edge.id for edge in self.edges}) != len(self.edges):
            raise ValueError("Reasoning Graph Edge IDs must be unique")
        edge_keys: set[tuple[str, str, ReasoningRelationType]] = set()
        for edge in self.edges:
            if edge.run_id != self.run_id:
                raise ValueError("Reasoning Graph Edges must belong to its Run")
            if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
                raise ValueError("Reasoning Edge references an unknown Node")
            key = (edge.source_node_id, edge.target_node_id, edge.relation_type)
            if key in edge_keys:
                raise ValueError("Reasoning Graph Edges must be structurally unique")
            edge_keys.add(key)
            if edge.relation_type in {
                ReasoningRelationType.SUPPORTS,
                ReasoningRelationType.CONTRADICTS,
                ReasoningRelationType.VALIDATES,
                ReasoningRelationType.EXPLOITS,
                ReasoningRelationType.INVALIDATES,
            } and not (edge.evidence_ids or nodes[edge.source_node_id].evidence_ids):
                raise ValueError("evidentiary Reasoning Edge requires Evidence lineage")

        incoming: dict[str, list[ReasoningEdge]] = {node_id: [] for node_id in nodes}
        outgoing: dict[str, list[ReasoningEdge]] = {node_id: [] for node_id in nodes}
        for edge in self.edges:
            incoming[edge.target_node_id].append(edge)
            outgoing[edge.source_node_id].append(edge)

        for node in self.nodes:
            if node.kind is ReasoningNodeKind.HYPOTHESIS:
                supports = [
                    edge
                    for edge in incoming[node.id]
                    if edge.relation_type is ReasoningRelationType.SUPPORTS
                ]
                contradictions = [
                    edge
                    for edge in incoming[node.id]
                    if edge.relation_type is ReasoningRelationType.CONTRADICTS
                ]
                if not node.evidence_ids and not supports and not contradictions:
                    if node.status is not ReasoningNodeStatus.UNVERIFIED:
                        raise ValueError("Evidence-free Hypothesis must remain unverified")
                if node.status is ReasoningNodeStatus.SUPPORTED and (
                    not supports or contradictions
                ):
                    raise ValueError(
                        "Supported Hypothesis requires support without contradiction"
                    )
            if node.kind is ReasoningNodeKind.CONFIRMED_FACT:
                if not any(
                    edge.relation_type is ReasoningRelationType.DERIVED_FROM
                    and nodes[edge.source_node_id].kind
                    is ReasoningNodeKind.FACT_CANDIDATE
                    for edge in incoming[node.id]
                ):
                    raise ValueError("Confirmed Fact must derive from a Fact Candidate")
            if node.kind is ReasoningNodeKind.FACT_CANDIDATE and (
                node.status is ReasoningNodeStatus.PROMOTED
            ):
                if not any(
                    edge.relation_type is ReasoningRelationType.DERIVED_FROM
                    and nodes[edge.target_node_id].kind
                    is ReasoningNodeKind.CONFIRMED_FACT
                    for edge in outgoing[node.id]
                ):
                    raise ValueError("Promoted Fact Candidate requires Confirmed Fact lineage")
            if node.kind is ReasoningNodeKind.VULNERABILITY_CANDIDATE and (
                node.status is ReasoningNodeStatus.PROMOTED
            ):
                if not any(
                    edge.relation_type is ReasoningRelationType.DERIVED_FROM
                    and nodes[edge.target_node_id].kind is ReasoningNodeKind.FINDING
                    for edge in outgoing[node.id]
                ):
                    raise ValueError("Promoted Vulnerability Candidate requires Finding lineage")
            if node.kind is ReasoningNodeKind.PROOF and (
                node.status is ReasoningNodeStatus.VALIDATED
            ):
                if not any(
                    edge.relation_type is ReasoningRelationType.VALIDATES
                    and nodes[edge.target_node_id].kind is ReasoningNodeKind.FINDING
                    for edge in outgoing[node.id]
                ):
                    raise ValueError("Validated Proof must validate a Finding")
            if node.kind is ReasoningNodeKind.NEGATIVE_RESULT:
                if not any(
                    edge.relation_type
                    in {ReasoningRelationType.INVALIDATES, ReasoningRelationType.CONTRADICTS}
                    for edge in outgoing[node.id]
                ):
                    raise ValueError("Negative Result must invalidate or contradict a claim")
        return self


class ReasoningGraphRepository(Protocol):
    async def create(self, graph: ReasoningGraph) -> ReasoningGraph: ...

    async def get(self, run_id: str) -> ReasoningGraph | None: ...
