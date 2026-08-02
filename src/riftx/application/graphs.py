"""Safe application-layer contracts for deterministic Run graph projections."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class GraphViewKind(StrEnum):
    TASK = "task"
    EVIDENCE = "evidence"
    OPERATION = "operation"


class GraphScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    engagement_id: str

    @field_validator("run_id", "engagement_id")
    @classmethod
    def validate_scope_id(cls, value: str) -> str:
        return _validate_domain_id(value)


@dataclass(frozen=True, slots=True)
class GraphRunSource:
    """Run identity only; objective and workspace path are impossible."""

    id: str
    engagement_id: str
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphPlanItemSource:
    """Plan topology only; task and completion text are impossible."""

    id: str
    run_id: str
    sequence: int
    status: str


@dataclass(frozen=True, slots=True)
class GraphActionSource:
    """Allowlisted Action fields accepted by the graph projector."""

    action_id: str
    run_id: str
    session_id: str
    tool_id: str | None
    status: str
    execution_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphEvidenceRefSource:
    """A value-free candidate association copied from model-writable Working Memory."""

    reference_id: str
    source_type: str


@dataclass(frozen=True, slots=True)
class GraphFactSource:
    """Fact state and evidence identity; fact value and prose are impossible."""

    fact_id: str
    run_id: str
    status: str
    evidence_refs: tuple[GraphEvidenceRefSource, ...]


@dataclass(frozen=True, slots=True)
class GraphHypothesisSource:
    """Hypothesis state and Fact lineage; statement text is impossible."""

    hypothesis_id: str
    run_id: str
    status: str
    supporting_fact_ids: tuple[str, ...] = ()
    contradicting_fact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphUserDecisionSource:
    """Decision identity only; question, decision, and reason text are impossible."""

    decision_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class GraphEngagementFactSource:
    """Attack-graph Fact identity and resolved lineage without fact content."""

    id: str
    engagement_id: str
    status: str
    source_run_ids: tuple[str, ...]
    source_session_ids: tuple[str, ...] = ()
    source_execution_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    unresolved: bool = False


@dataclass(frozen=True, slots=True)
class GraphFactRelationSource:
    """Allowlisted AttackGraph edge with value-free provenance identities."""

    id: str
    engagement_id: str
    source_fact_id: str
    target_fact_id: str
    relation_type: str
    source_run_id: str
    source_session_id: str | None = None
    source_execution_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    unresolved: bool = False


@dataclass(frozen=True, slots=True)
class GraphArtifactSource:
    """Artifact identity and lineage only; paths and descriptions are impossible."""

    artifact_id: str
    run_id: str
    execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphExecutionSource:
    """Execution topology only; command, argv, env, cwd, and output are impossible."""

    execution_id: str
    run_id: str
    session_id: str | None
    node_id: str
    status: str


@dataclass(frozen=True, slots=True)
class GraphFindingSource:
    """Finding identity and typed evidence refs without descriptive content."""

    finding_id: str
    run_id: str
    status: str
    severity: str
    artifact_ids: tuple[str, ...] = ()
    execution_ids: tuple[str, ...] = ()
    user_decision_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphSessionSource:
    """RiftX runtime Session identity and state only."""

    session_id: str
    run_id: str
    status: str
    parent_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphExecutionHostSource:
    """A RiftX execution Node association, never an inferred target host."""

    node_id: str
    run_id: str
    status: str


@dataclass(frozen=True, slots=True)
class GraphSourceCoverage:
    """Repository-issued hard-budget coverage for one allowlisted source."""

    source: str
    scanned: int
    limit: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class GraphSourceSnapshot:
    """One server-resolved, same-scope input snapshot for a graph projection."""

    scope: GraphScope
    run: GraphRunSource
    plan_items: tuple[GraphPlanItemSource, ...] = ()
    actions: tuple[GraphActionSource, ...] = ()
    facts: tuple[GraphFactSource, ...] = ()
    hypotheses: tuple[GraphHypothesisSource, ...] = ()
    user_decisions: tuple[GraphUserDecisionSource, ...] = ()
    engagement_facts: tuple[GraphEngagementFactSource, ...] = ()
    fact_relations: tuple[GraphFactRelationSource, ...] = ()
    findings: tuple[GraphFindingSource, ...] = ()
    artifacts: tuple[GraphArtifactSource, ...] = ()
    executions: tuple[GraphExecutionSource, ...] = ()
    sessions: tuple[GraphSessionSource, ...] = ()
    execution_hosts: tuple[GraphExecutionHostSource, ...] = ()
    coverage: tuple[GraphSourceCoverage, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphFilter:
    node_type: str | None = None
    edge_type: str | None = None
    focus: str | None = None
    search: str | None = None


class _GraphViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GraphSnapshot(_GraphViewModel):
    id: str
    stale: bool = False
    generated_at: None = None
    topology_signature: str

    @field_validator("id", "topology_signature")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Graph topology signature is invalid")
        return value


class GraphNode(_GraphViewModel):
    """Strict output allowlist; raw source payloads cannot be represented here."""

    id: str
    type: str
    domain_id: str
    label: str
    status: str | None
    provenance_refs: tuple[str, ...]
    projection_quality: str
    partial_reasons: tuple[str, ...]

    @field_validator("id")
    @classmethod
    def validate_graph_id(cls, value: str) -> str:
        return _validate_graph_id(value)

    @field_validator("domain_id")
    @classmethod
    def validate_node_domain_id(cls, value: str) -> str:
        return _validate_domain_id(value)

    @field_validator("type", "projection_quality")
    @classmethod
    def validate_node_token(cls, value: str) -> str:
        return _validate_token(value)

    @field_validator("status")
    @classmethod
    def validate_node_status(cls, value: str | None) -> str | None:
        return None if value is None else _validate_token(value)

    @field_validator("label")
    @classmethod
    def validate_node_label(cls, value: str) -> str:
        return _validate_safe_text(value, maximum=128)

    @field_validator("provenance_refs")
    @classmethod
    def validate_node_provenance(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reference_codes(values)

    @field_validator("partial_reasons")
    @classmethod
    def validate_node_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_tokens(values)


class GraphEdge(_GraphViewModel):
    id: str
    type: str
    source: str
    target: str
    provenance_refs: tuple[str, ...]
    projection_quality: str

    @field_validator("id", "source", "target")
    @classmethod
    def validate_edge_graph_id(cls, value: str) -> str:
        return _validate_graph_id(value)

    @field_validator("type", "projection_quality")
    @classmethod
    def validate_edge_token(cls, value: str) -> str:
        return _validate_token(value)

    @field_validator("provenance_refs")
    @classmethod
    def validate_edge_provenance(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reference_codes(values)


class GraphTypeMetadata(_GraphViewModel):
    kind: str
    type: str
    label: str
    color: str
    description: str | None = None

    @field_validator("kind", "type")
    @classmethod
    def validate_metadata_token(cls, value: str) -> str:
        return _validate_token(value)

    @field_validator("label", "color")
    @classmethod
    def validate_metadata_text(cls, value: str) -> str:
        return _validate_safe_text(value, maximum=128)

    @field_validator("description")
    @classmethod
    def validate_metadata_description(cls, value: str | None) -> str | None:
        return None if value is None else _validate_safe_text(value, maximum=256)


class GraphViewPage(_GraphViewModel):
    scope: GraphScope
    view: GraphViewKind
    snapshot: GraphSnapshot
    snapshot_id: str
    projection_sources: tuple[str, ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    type_metadata: tuple[GraphTypeMetadata, ...]
    partial_reasons: tuple[str, ...]
    truncated: bool
    has_more: bool
    next_cursor: str | None

    @field_validator("projection_sources")
    @classmethod
    def validate_projection_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reference_codes(values)

    @field_validator("partial_reasons")
    @classmethod
    def validate_page_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_tokens(values)

    @model_validator(mode="after")
    def validate_page_contract(self) -> GraphViewPage:
        node_ids = [node.id for node in self.nodes]
        edge_ids = [edge.id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)) or len(edge_ids) != len(set(edge_ids)):
            raise ValueError("Graph page contains duplicate records")
        available = set(node_ids)
        if any(edge.source not in available or edge.target not in available for edge in self.edges):
            raise ValueError("Graph page contains an orphaned edge")
        if self.truncated and not self.partial_reasons:
            raise ValueError("A truncated Graph page requires a partial reason")
        if self.has_more is not (self.next_cursor is not None):
            raise ValueError("Graph pagination state is inconsistent")
        return self


class InvalidGraphCursorError(ValueError):
    code = "invalid_graph_cursor"

    def __init__(self) -> None:
        super().__init__("The Graph cursor is invalid for this scope and query")


class StaleGraphCursorError(ValueError):
    code = "stale_graph_cursor"

    def __init__(self) -> None:
        super().__init__("The Graph topology changed after this cursor was issued")


class GraphSourceContractError(RuntimeError):
    """Raised when a repository returns cross-scope or structurally invalid input."""


class UnsupportedGraphViewError(ValueError):
    def __init__(self, view: GraphViewKind) -> None:
        super().__init__(f"Graph view {view.value!r} is not available from this source snapshot")


_DOMAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+~-]{0,127}")
_GRAPH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+~-]{0,511}")
_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _validate_safe_text(value: str, *, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError("Graph text is invalid")
    if len(value.encode("utf-8")) > maximum * 4:
        raise ValueError("Graph text is invalid")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("Graph text contains unsafe Unicode")
    return value


def _validate_domain_id(value: str) -> str:
    if type(value) is not str or _DOMAIN_ID.fullmatch(value) is None:
        raise ValueError("Graph domain ID is invalid")
    return value


def _validate_graph_id(value: str) -> str:
    if type(value) is not str or _GRAPH_ID.fullmatch(value) is None:
        raise ValueError("Graph projection ID is invalid")
    return value


def _validate_token(value: str) -> str:
    if type(value) is not str or _TOKEN.fullmatch(value) is None:
        raise ValueError("Graph type token is invalid")
    return value


def _validate_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError("Graph reason codes must be unique")
    return tuple(_validate_token(value) for value in values)


def _validate_reference_codes(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) != len(set(values)) or len(values) > 32:
        raise ValueError("Graph provenance is invalid")
    return tuple(_validate_safe_text(value, maximum=256) for value in values)
