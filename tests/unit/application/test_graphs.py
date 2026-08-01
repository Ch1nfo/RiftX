from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import replace

import pytest

from riftx.application.errors import ResourceNotAccessibleError
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
    GraphNode,
    GraphPlanItemSource,
    GraphRunSource,
    GraphScope,
    GraphSessionSource,
    GraphSourceContractError,
    GraphSourceCoverage,
    GraphSourceSnapshot,
    GraphUserDecisionSource,
    GraphViewKind,
    InvalidGraphCursorError,
    StaleGraphCursorError,
)
from riftx.application.services.graphs import GraphApplicationService
from riftx.domain import (
    LocalPrincipal,
    OperatorCapability,
    TrustProfile,
)

CURSOR_SIGNING_KEY = b"riftx-graph-tests-only-key-32bytes"

PRINCIPAL = LocalPrincipal(
    id="operator-1",
    profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
    capabilities=frozenset({OperatorCapability.READ}),
)


class RecordingAuthorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, str, OperatorCapability]] = []

    def require_run_engagement(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        parent_engagement_id: str,
        resource_engagement_id: str | None,
        capability: OperatorCapability,
    ) -> None:
        self.calls.append(
            (
                principal.id,
                parent_run_id,
                resource_run_id or "",
                parent_engagement_id,
                resource_engagement_id or "",
                capability,
            )
        )
        assert resource_run_id == parent_run_id
        assert resource_engagement_id == parent_engagement_id
        assert capability is OperatorCapability.READ


class SnapshotRepository:
    def __init__(self, snapshot: GraphSourceSnapshot) -> None:
        self.snapshot = snapshot
        self.load_calls: list[tuple[GraphScope, GraphViewKind]] = []

    async def resolve_scope(self, run_id: str) -> GraphScope | None:
        if run_id != self.snapshot.scope.run_id:
            return None
        return self.snapshot.scope

    async def load(
        self,
        scope: GraphScope,
        view: GraphViewKind,
    ) -> GraphSourceSnapshot:
        assert scope == self.snapshot.scope
        self.load_calls.append((scope, view))
        return self.snapshot


def source_snapshot(
    *,
    run_id: str = "run-1",
    engagement_id: str = "engagement-1",
) -> GraphSourceSnapshot:
    run = GraphRunSource(
        id=run_id,
        engagement_id=engagement_id,
        node_id="node-1",
    )
    return GraphSourceSnapshot(
        scope=GraphScope(run_id=run.id, engagement_id=run.engagement_id),
        run=run,
        plan_items=(
            GraphPlanItemSource(
                id="plan-1",
                run_id=run.id,
                sequence=1,
                status="pending",
            ),
        ),
        actions=(
            GraphActionSource(
                action_id="action-1",
                run_id=run.id,
                session_id="session-1",
                tool_id="nmap",
                status="completed",
                execution_ids=(),
            ),
        ),
    )


async def test_task_view_never_guesses_action_plan_lineage() -> None:
    snapshot = source_snapshot()
    authorizer = RecordingAuthorizer()
    repository = SnapshotRepository(snapshot)
    service = GraphApplicationService(
        repository,
        authorizer=authorizer,
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )

    page = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.TASK,
        limit=100,
    )

    assert {node.id for node in page.nodes} >= {
        "plan_item:run-1:plan-1",
        "action:run-1:action-1",
        "unassigned_actions:run-1",
    }
    action_edges = [edge for edge in page.edges if edge.source == "action:run-1:action-1"]
    assert [(edge.type, edge.target) for edge in action_edges] == [
        ("unassigned", "unassigned_actions:run-1")
    ]
    assert all(edge.target != "plan_item:run-1:plan-1" for edge in action_edges)
    assert page.projection_sources == (
        "working_memory.run_plan",
        "tool_call_intents",
        "findings",
        "executions",
        "artifacts",
    )
    assert authorizer.calls == [
        (
            "operator-1",
            "run-1",
            "run-1",
            "engagement-1",
            "engagement-1",
            OperatorCapability.READ,
        )
    ]
    assert repository.load_calls == [(snapshot.scope, GraphViewKind.TASK)]


async def test_untrusted_view_is_enumerated_before_repository_load() -> None:
    snapshot = source_snapshot()
    repository = SnapshotRepository(snapshot)
    authorizer = RecordingAuthorizer()
    service = GraphApplicationService(
        repository,
        authorizer=authorizer,
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )

    with pytest.raises(ValueError, match="Unknown Graph view"):
        await service.get_view(
            "run-1",
            principal=PRINCIPAL,
            view="not-a-view",  # type: ignore[arg-type]
        )

    assert repository.load_calls == []
    assert authorizer.calls == []


async def test_task_projection_is_deterministic_for_identical_sources() -> None:
    snapshot = source_snapshot()
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )

    first = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.TASK,
        limit=100,
    )
    second = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.TASK,
        limit=100,
    )

    assert first == second
    assert first.snapshot_id == second.snapshot_id


async def test_cursor_snapshot_changes_only_when_topology_changes() -> None:
    snapshot = source_snapshot()
    repository = SnapshotRepository(snapshot)
    service = GraphApplicationService(
        repository,
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    first = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.TASK,
        limit=1,
    )
    assert first.has_more is True
    assert first.next_cursor is not None

    repository.snapshot = replace(
        snapshot,
        actions=(
            *snapshot.actions,
            GraphActionSource(
                action_id="action-2",
                run_id="run-1",
                session_id="session-1",
                tool_id="curl",
                status="proposed",
                execution_ids=(),
            ),
        ),
    )

    # A cursor is bound to one exact topology. The service must reject a mixed
    # snapshot instead of returning duplicate records or orphaned edges.
    with pytest.raises(StaleGraphCursorError):
        await service.get_view(
            "run-1",
            principal=PRINCIPAL,
            view=GraphViewKind.TASK,
            cursor=first.next_cursor,
            limit=1,
        )


def _snapshot_with_actions(count: int) -> GraphSourceSnapshot:
    snapshot = source_snapshot()
    return replace(
        snapshot,
        actions=tuple(
            GraphActionSource(
                action_id=f"action-{index}",
                run_id="run-1",
                session_id="session-1",
                tool_id="safe-tool",
                status="completed",
                execution_ids=(),
            )
            for index in range(1, count + 1)
        ),
    )


@pytest.mark.parametrize("limit", [1, 2])
async def test_edge_unit_pagination_never_loses_or_orphans_edges(limit: int) -> None:
    snapshot = _snapshot_with_actions(4)
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    cursor: str | None = None
    edge_ids: list[str] = []
    pages = 0

    while True:
        page = await service.get_view(
            "run-1",
            principal=PRINCIPAL,
            view=GraphViewKind.TASK,
            limit=limit,
            cursor=cursor,
        )
        pages += 1
        assert page.truncated is False
        assert page.partial_reasons == ()
        node_ids = {node.id for node in page.nodes}
        assert all(edge.source in node_ids and edge.target in node_ids for edge in page.edges)
        edge_ids.extend(edge.id for edge in page.edges)
        if not page.has_more:
            assert page.next_cursor is None
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor

    assert pages > 1
    assert len(edge_ids) == len(set(edge_ids)) == 4
    assert set(edge_ids) == {f"unassigned:run-1:action-{index}" for index in range(1, 5)}


def _tamper_cursor(cursor: str, **updates: object) -> str:
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    envelope = json.loads(raw)
    envelope["body"].update(updates)
    return (
        base64.urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )


async def test_cursor_hmac_rejects_forgery_and_query_or_principal_replay() -> None:
    snapshot = _snapshot_with_actions(3)
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    first = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.TASK,
        limit=1,
    )
    assert first.next_cursor is not None

    other_principal = LocalPrincipal(
        id="operator-2",
        profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        capabilities=frozenset({OperatorCapability.READ}),
    )
    invalid_requests = (
        {"cursor": _tamper_cursor(first.next_cursor, offset=99), "limit": 1},
        {"cursor": first.next_cursor, "limit": 2},
        {"cursor": first.next_cursor, "limit": 1, "search": "action"},
    )
    for request in invalid_requests:
        with pytest.raises(InvalidGraphCursorError):
            await service.get_view(
                "run-1",
                principal=PRINCIPAL,
                view=GraphViewKind.TASK,
                **request,  # type: ignore[arg-type]
            )
    with pytest.raises(InvalidGraphCursorError):
        await service.get_view(
            "run-1",
            principal=other_principal,
            view=GraphViewKind.TASK,
            cursor=first.next_cursor,
            limit=1,
        )

    run_two_service = GraphApplicationService(
        SnapshotRepository(source_snapshot(run_id="run-2", engagement_id="engagement-2")),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    with pytest.raises(InvalidGraphCursorError):
        await run_two_service.get_view(
            "run-2",
            principal=PRINCIPAL,
            view=GraphViewKind.TASK,
            cursor=first.next_cursor,
            limit=1,
        )


@pytest.mark.parametrize(
    "focus",
    ["action:run-2:action-1", "finding:run-2:missing", "not-a-typed-id"],
)
async def test_focus_must_resolve_to_an_exact_node_in_authorized_scope(focus: str) -> None:
    service = GraphApplicationService(
        SnapshotRepository(source_snapshot()),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )

    with pytest.raises(ResourceNotAccessibleError) as captured:
        await service.get_view(
            "run-1",
            principal=PRINCIPAL,
            view=GraphViewKind.TASK,
            focus=focus,
        )

    assert captured.value.code == "resource_not_accessible"
    assert focus not in str(captured.value)


def _decode_cursor_envelope(cursor: str) -> dict[str, object]:
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    return json.loads(raw)


def _signed_cursor(envelope: dict[str, object]) -> str:
    body = envelope["body"]
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    envelope["signature"] = hmac.new(
        CURSOR_SIGNING_KEY,
        b"riftx-graph-cursor-v1\0" + canonical,
        hashlib.sha256,
    ).hexdigest()
    return (
        base64.urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )


async def test_cursor_json_schema_is_strict_even_with_a_valid_hmac() -> None:
    service = GraphApplicationService(
        SnapshotRepository(_snapshot_with_actions(3)),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    first = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.TASK, limit=1)
    assert first.next_cursor is not None

    extra_body = _decode_cursor_envelope(first.next_cursor)
    assert isinstance(extra_body["body"], dict)
    extra_body["body"]["unexpected"] = "field"

    wrong_type = _decode_cursor_envelope(first.next_cursor)
    assert isinstance(wrong_type["body"], dict)
    wrong_type["body"]["offset"] = True

    extra_filter = _decode_cursor_envelope(first.next_cursor)
    assert isinstance(extra_filter["body"], dict)
    assert isinstance(extra_filter["body"]["filter"], dict)
    extra_filter["body"]["filter"]["unexpected"] = None

    extra_envelope = _decode_cursor_envelope(first.next_cursor)
    extra_envelope["unexpected"] = "field"

    for cursor in (
        _signed_cursor(extra_body),
        _signed_cursor(wrong_type),
        _signed_cursor(extra_filter),
        _signed_cursor(extra_envelope),
        "A" * 4097,
    ):
        with pytest.raises(InvalidGraphCursorError):
            await service.get_view(
                "run-1",
                principal=PRINCIPAL,
                view=GraphViewKind.TASK,
                limit=1,
                cursor=cursor,
            )

    valid = _decode_cursor_envelope(first.next_cursor)
    duplicate = (
        '{"body":'
        + json.dumps(valid["body"], separators=(",", ":"))
        + ',"body":'
        + json.dumps(valid["body"], separators=(",", ":"))
        + ',"signature":'
        + json.dumps(valid["signature"])
        + "}"
    )
    duplicate_cursor = base64.urlsafe_b64encode(duplicate.encode()).decode().rstrip("=")
    with pytest.raises(InvalidGraphCursorError):
        await service.get_view(
            "run-1",
            principal=PRINCIPAL,
            view=GraphViewKind.TASK,
            limit=1,
            cursor=duplicate_cursor,
        )


async def test_graph_inputs_reject_unsafe_or_oversized_text_without_reflection() -> None:
    unsafe_snapshot = replace(
        source_snapshot(),
        actions=(
            GraphActionSource(
                action_id="action-\u202esecret",
                run_id="run-1",
                session_id="session-1",
                tool_id="tool",
                status="completed",
                execution_ids=(),
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(unsafe_snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    with pytest.raises(GraphSourceContractError) as captured:
        await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.TASK)
    assert "secret" not in str(captured.value)

    safe_service = GraphApplicationService(
        SnapshotRepository(source_snapshot()),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    for filters in (
        {"node_type": "action\u202e"},
        {"search": "unsafe\x00search"},
        {"search": "x" * 513},
    ):
        with pytest.raises(ValueError) as filter_error:
            await safe_service.get_view(
                "run-1",
                principal=PRINCIPAL,
                view=GraphViewKind.TASK,
                **filters,
            )
        assert "unsafe" not in str(filter_error.value)

    with pytest.raises(ValueError):
        GraphNode(
            id="action:run-1:action-1",
            type="action",
            domain_id="action-1",
            label="unsafe\u202elabel",
            status="completed",
            provenance_refs=("tool_call_intents",),
            projection_quality="exact",
            partial_reasons=(),
        )


def test_graph_source_contract_structurally_excludes_sensitive_body_fields() -> None:
    with pytest.raises(TypeError):
        GraphRunSource(  # type: ignore[call-arg]
            id="run-1",
            engagement_id="engagement-1",
            workspace_path="SECRET-WORKSPACE-PATH",
        )
    with pytest.raises(TypeError):
        GraphPlanItemSource(  # type: ignore[call-arg]
            id="plan-1",
            run_id="run-1",
            sequence=1,
            status="pending",
            task="SECRET-PLAN-TASK",
        )
    with pytest.raises(TypeError):
        GraphFactSource(  # type: ignore[call-arg]
            fact_id="fact-1",
            run_id="run-1",
            status="confirmed",
            evidence_refs=(),
            value="SECRET-FACT-VALUE",
        )
    with pytest.raises(TypeError):
        GraphFindingSource(  # type: ignore[call-arg]
            finding_id="finding-1",
            run_id="run-1",
            status="draft",
            severity="low",
            description="SECRET-FINDING-DESCRIPTION",
        )
    with pytest.raises(TypeError):
        GraphArtifactSource(  # type: ignore[call-arg]
            artifact_id="artifact-1",
            run_id="run-1",
            path="SECRET-ARTIFACT-PATH",
        )
    with pytest.raises(TypeError):
        GraphExecutionSource(  # type: ignore[call-arg]
            execution_id="execution-1",
            run_id="run-1",
            session_id=None,
            node_id="node-1",
            status="completed",
            command="SECRET-EXECUTION-COMMAND",
        )
    with pytest.raises(TypeError):
        GraphUserDecisionSource(  # type: ignore[call-arg]
            decision_id="decision-1",
            run_id="run-1",
            source_ref="user_confirmed",
        )

    snapshot_fields = set(GraphSourceSnapshot.__dataclass_fields__)
    assert "working_memory" not in snapshot_fields
    assert {"workspace_path", "objective", "fact_values", "decision_text"}.isdisjoint(
        snapshot_fields
    )


def _fact(
    fact_id: str,
    source_ref: str,
    source_type: str,
) -> GraphFactSource:
    return GraphFactSource(
        fact_id=fact_id,
        run_id="run-1",
        status="confirmed",
        evidence_refs=(
            GraphEvidenceRefSource(
                reference_id=source_ref,
                source_type=source_type,
            ),
        ),
    )


def evidence_snapshot() -> GraphSourceSnapshot:
    snapshot = source_snapshot()
    return replace(
        snapshot,
        facts=(
            _fact("fact-artifact", "artifact-1", "deterministic_parser"),
            _fact("fact-execution", "execution-1", "deterministic_parser"),
            _fact("fact-model", "artifact-1", "model_inference"),
            _fact("fact-unresolved", "foreign-artifact", "deterministic_parser"),
            _fact("fact-user", "decision-1", "user_decision"),
            _fact("fact-naked-user", "user_confirmed", "user_decision"),
        ),
        hypotheses=(
            GraphHypothesisSource(
                hypothesis_id="hypothesis-verified",
                run_id="run-1",
                status="confirmed",
                supporting_fact_ids=("fact-artifact",),
            ),
            GraphHypothesisSource(
                hypothesis_id="hypothesis-unverified",
                run_id="run-1",
                status="confirmed",
                supporting_fact_ids=("fact-model",),
            ),
        ),
        user_decisions=(
            GraphUserDecisionSource(
                decision_id="decision-1",
                run_id="run-1",
            ),
        ),
        artifacts=(
            GraphArtifactSource(
                artifact_id="artifact-1",
                run_id="run-1",
                execution_id="execution-1",
            ),
        ),
        executions=(
            GraphExecutionSource(
                execution_id="execution-1",
                run_id="run-1",
                session_id="session-1",
                node_id="node-1",
                status="completed",
            ),
        ),
        findings=(
            GraphFindingSource(
                finding_id="finding-verified",
                run_id="run-1",
                status="confirmed",
                severity="high",
                artifact_ids=("artifact-1",),
            ),
            GraphFindingSource(
                finding_id="finding-unverified",
                run_id="run-1",
                status="confirmed",
                severity="medium",
            ),
        ),
    )


async def test_working_memory_candidate_evidence_never_confirms_facts_or_hypotheses() -> None:
    snapshot = evidence_snapshot()
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.EVIDENCE,
        limit=100,
    )
    statuses = {node.id: node.status for node in page.nodes}

    assert statuses["fact:run-1:fact-artifact"] == "unverified"
    assert statuses["fact:run-1:fact-execution"] == "unverified"
    assert statuses["fact:run-1:fact-user"] == "unverified"
    assert statuses["fact:run-1:fact-model"] == "unverified"
    assert statuses["fact:run-1:fact-unresolved"] == "unverified"
    assert statuses["fact:run-1:fact-naked-user"] == "unverified"
    assert statuses["hypothesis:run-1:hypothesis-verified"] == "unverified"
    assert statuses["hypothesis:run-1:hypothesis-unverified"] == "unverified"
    assert statuses["finding:run-1:finding-verified"] == "confirmed"
    assert statuses["finding:run-1:finding-unverified"] == "unverified"
    assert statuses["user_decision:run-1:decision-1"] == "recorded"
    candidate_edges = {
        (edge.source, edge.target): edge.projection_quality
        for edge in page.edges
        if edge.target.startswith("fact:run-1:")
    }
    assert candidate_edges == {
        ("artifact:run-1:artifact-1", "fact:run-1:fact-artifact"): "partial",
        ("execution:run-1:execution-1", "fact:run-1:fact-execution"): "partial",
        ("user_decision:run-1:decision-1", "fact:run-1:fact-user"): "partial",
    }
    assert "candidate_evidence_not_authoritative" in page.partial_reasons
    assert "authoritative_evidence_association_unavailable" in page.partial_reasons

    serialized = page.model_dump_json()
    for canary in (
        "SECRET-SUBJECT",
        "SECRET-PREDICATE",
        "SECRET-FACT-VALUE",
        "SECRET-FACT-NATURAL-LANGUAGE",
        "SECRET-HYPOTHESIS",
        "SECRET-USER-QUESTION",
        "SECRET-USER-DECISION",
        "/tmp/run-1",
        "Authorized objective",
    ):
        assert canary not in serialized
    node_ids = {node.id for node in page.nodes}
    assert all(edge.source in node_ids and edge.target in node_ids for edge in page.edges)


async def test_working_memory_fact_lifecycle_statuses_survive_confirmation_downgrade() -> None:
    snapshot = replace(
        source_snapshot(),
        facts=(
            GraphFactSource(
                fact_id="fact-disputed",
                run_id="run-1",
                status="disputed",
                evidence_refs=(),
            ),
            GraphFactSource(
                fact_id="fact-superseded",
                run_id="run-1",
                status="superseded",
                evidence_refs=(),
            ),
            GraphFactSource(
                fact_id="fact-unknown",
                run_id="run-1",
                status="future_status",
                evidence_refs=(),
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )

    page = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.EVIDENCE,
    )
    nodes = {node.domain_id: node for node in page.nodes if node.type == "fact"}

    assert nodes["fact-disputed"].status == "disputed"
    assert nodes["fact-disputed"].partial_reasons == ()
    assert nodes["fact-superseded"].status == "superseded"
    assert nodes["fact-superseded"].partial_reasons == ()
    assert nodes["fact-unknown"].status is None
    assert nodes["fact-unknown"].partial_reasons == ("source_status_unknown",)


async def test_evidence_type_metadata_is_complete_after_filtering() -> None:
    snapshot = evidence_snapshot()
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    full = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.EVIDENCE)
    filtered = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.EVIDENCE,
        node_type="fact",
    )

    assert filtered.type_metadata == full.type_metadata
    assert {item.type for item in filtered.type_metadata} >= {
        "fact",
        "hypothesis",
        "finding",
        "artifact",
        "execution",
        "supports",
        "contradicts",
        "produced",
        "user_decision",
        "engagement_fact",
    }


async def test_engagement_fact_filter_matches_its_distinct_node_type() -> None:
    snapshot = replace(
        evidence_snapshot(),
        engagement_facts=(
            GraphEngagementFactSource(
                id="engagement-fact-1",
                engagement_id="engagement-1",
                status="active",
                source_run_ids=("run-1",),
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )

    engagement_facts = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.EVIDENCE,
        node_type="engagement_fact",
    )
    run_facts = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.EVIDENCE,
        node_type="fact",
    )

    engagement_id = "engagement_fact:engagement-1:engagement-fact-1"
    assert [(node.id, node.type) for node in engagement_facts.nodes] == [
        (engagement_id, "engagement_fact")
    ]
    assert engagement_id not in {node.id for node in run_facts.nodes}


async def test_evidence_namespaces_prevent_same_raw_id_collisions() -> None:
    snapshot = replace(
        evidence_snapshot(),
        facts=(_fact("same-id", "artifact-1", "deterministic_parser"),),
        engagement_facts=(
            GraphEngagementFactSource(
                id="same-id",
                engagement_id="engagement-1",
                status="active",
                source_run_ids=("run-1",),
                source_execution_ids=("execution-1",),
            ),
        ),
        findings=(
            GraphFindingSource(
                finding_id="same-id",
                run_id="run-1",
                status="confirmed",
                severity="high",
                execution_ids=("execution-1",),
            ),
        ),
        artifacts=(
            GraphArtifactSource(
                artifact_id="same-id",
                run_id="run-1",
                execution_id="execution-1",
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.EVIDENCE)
    ids = {node.id for node in page.nodes}
    assert {
        "fact:run-1:same-id",
        "engagement_fact:engagement-1:same-id",
        "finding:run-1:same-id",
        "artifact:run-1:same-id",
    } <= ids


async def test_orphan_artifact_cannot_confirm_a_finding() -> None:
    snapshot = replace(
        source_snapshot(),
        artifacts=(
            GraphArtifactSource(
                artifact_id="orphan-artifact",
                run_id="run-1",
                execution_id="missing-execution",
            ),
        ),
        findings=(
            GraphFindingSource(
                finding_id="finding-orphan",
                run_id="run-1",
                status="confirmed",
                severity="high",
                artifact_ids=("orphan-artifact",),
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.EVIDENCE)
    nodes = {node.id: node for node in page.nodes}
    assert nodes["finding:run-1:finding-orphan"].status == "unverified"
    assert nodes["artifact:run-1:orphan-artifact"].projection_quality == "partial"
    assert all(edge.target != "finding:run-1:finding-orphan" for edge in page.edges)


async def test_candidate_fact_relations_cannot_confirm_a_hypothesis() -> None:
    snapshot = replace(
        evidence_snapshot(),
        hypotheses=(
            GraphHypothesisSource(
                hypothesis_id="hypothesis-conflicted",
                run_id="run-1",
                status="confirmed",
                supporting_fact_ids=("fact-artifact",),
                contradicting_fact_ids=("fact-user", "fact-model"),
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.EVIDENCE)
    nodes = {node.id: node for node in page.nodes}
    hypothesis_id = "hypothesis:run-1:hypothesis-conflicted"
    assert nodes[hypothesis_id].status == "unverified"
    qualities = {
        edge.source: edge.projection_quality for edge in page.edges if edge.target == hypothesis_id
    }
    assert qualities["fact:run-1:fact-artifact"] == "partial"
    assert qualities["fact:run-1:fact-user"] == "partial"
    assert qualities["fact:run-1:fact-model"] == "partial"


async def test_operation_projection_contains_only_riftx_execution_topology() -> None:
    snapshot = replace(
        source_snapshot(),
        executions=(
            GraphExecutionSource(
                execution_id="execution-1",
                run_id="run-1",
                session_id="session-1",
                node_id="node-1",
                status="completed",
            ),
        ),
        sessions=(
            GraphSessionSource(
                session_id="session-1",
                run_id="run-1",
                status="closed",
            ),
        ),
        execution_hosts=(
            GraphExecutionHostSource(
                node_id="node-1",
                run_id="run-1",
                status="online",
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.OPERATION)

    assert {node.id for node in page.nodes} == {
        "execution:run-1:execution-1",
        "session:run-1:session-1",
        "execution_host:run-1:node-1",
    }
    assert {(edge.type, edge.source, edge.target) for edge in page.edges} == {
        (
            "contains",
            "session:run-1:session-1",
            "execution:run-1:execution-1",
        ),
        (
            "ran_on",
            "execution:run-1:execution-1",
            "execution_host:run-1:node-1",
        ),
    }
    assert {node.type for node in page.nodes}.isdisjoint(
        {"host", "service", "endpoint", "credential"}
    )
    assert page.projection_sources == (
        "executions",
        "runtime_sessions",
        "execution_hosts",
    )


async def test_operation_projects_only_resolved_same_run_session_parent_lineage() -> None:
    snapshot = replace(
        source_snapshot(),
        sessions=(
            GraphSessionSource(
                session_id="parent-session",
                run_id="run-1",
                status="closed",
            ),
            GraphSessionSource(
                session_id="child-session",
                run_id="run-1",
                status="closed",
                parent_session_id="parent-session",
            ),
            GraphSessionSource(
                session_id="orphan-session",
                run_id="run-1",
                status="closed",
                parent_session_id="foreign-session",
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.OPERATION)

    parent_edges = [edge for edge in page.edges if edge.type == "parent_of"]
    assert [(edge.source, edge.target) for edge in parent_edges] == [
        (
            "session:run-1:parent-session",
            "session:run-1:child-session",
        )
    ]
    assert "session_parent_unresolved" in page.partial_reasons
    assert "parent_of" in {item.type for item in page.type_metadata}


async def test_task_findings_are_explicitly_unassigned_and_plan_gaps_are_partial() -> None:
    snapshot = replace(
        source_snapshot(),
        plan_items=(
            GraphPlanItemSource(
                id="plan-blocked",
                run_id="run-1",
                sequence=1,
                status="blocked",
            ),
            GraphPlanItemSource(
                id="plan-completed",
                run_id="run-1",
                sequence=2,
                status="completed",
            ),
        ),
        findings=(
            GraphFindingSource(
                finding_id="finding-1",
                run_id="run-1",
                status="confirmed",
                severity="high",
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.TASK)

    finding_id = "finding:run-1:finding-1"
    finding_edges = [edge for edge in page.edges if edge.source == finding_id]
    assert [(edge.type, edge.target) for edge in finding_edges] == [
        ("unassigned", "unassigned_findings:run-1")
    ]
    assert all(not edge.target.startswith("plan_item:") for edge in finding_edges)
    reasons = {node.id: node.partial_reasons for node in page.nodes}
    assert reasons["plan_item:run-1:plan-blocked"] == ("blocker_lineage_unavailable",)
    assert reasons["plan_item:run-1:plan-completed"] == ("completion_evidence_unavailable",)
    assert set(page.partial_reasons) >= {
        "blocker_lineage_unavailable",
        "completion_evidence_unavailable",
    }


async def test_task_exact_action_evidence_chain_is_traversable_without_plan_guessing() -> None:
    snapshot = replace(
        source_snapshot(),
        actions=(
            GraphActionSource(
                action_id="action-1",
                run_id="run-1",
                session_id="session-1",
                tool_id="safe-tool",
                status="completed",
                execution_ids=("execution-1",),
            ),
        ),
        executions=(
            GraphExecutionSource(
                execution_id="execution-1",
                run_id="run-1",
                session_id="session-1",
                node_id="node-1",
                status="completed",
            ),
        ),
        artifacts=(
            GraphArtifactSource(
                artifact_id="artifact-1",
                run_id="run-1",
                execution_id="execution-1",
            ),
        ),
        findings=(
            GraphFindingSource(
                finding_id="finding-1",
                run_id="run-1",
                status="confirmed",
                severity="high",
                artifact_ids=("artifact-1",),
                execution_ids=("execution-1",),
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.TASK)
    triples = {(edge.type, edge.source, edge.target) for edge in page.edges}

    assert (
        "executed_as",
        "action:run-1:action-1",
        "execution:run-1:execution-1",
    ) in triples
    assert (
        "produced",
        "execution:run-1:execution-1",
        "artifact:run-1:artifact-1",
    ) in triples
    assert (
        "supports",
        "artifact:run-1:artifact-1",
        "finding:run-1:finding-1",
    ) in triples
    assert (
        "supports",
        "execution:run-1:execution-1",
        "finding:run-1:finding-1",
    ) in triples
    finding_edges = [edge for edge in page.edges if edge.source == "finding:run-1:finding-1"]
    assert all(not edge.target.startswith("plan_item:") for edge in finding_edges)


async def test_task_unresolved_or_cross_session_execution_refs_are_partial() -> None:
    snapshot = replace(
        source_snapshot(),
        actions=(
            GraphActionSource(
                action_id="action-1",
                run_id="run-1",
                session_id="session-1",
                tool_id="safe-tool",
                status="completed",
                execution_ids=("missing-execution", "cross-session-execution"),
            ),
        ),
        executions=(
            GraphExecutionSource(
                execution_id="cross-session-execution",
                run_id="run-1",
                session_id="session-2",
                node_id="node-1",
                status="completed",
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.TASK)
    action = next(node for node in page.nodes if node.id == "action:run-1:action-1")

    assert "action_execution_unresolved" in action.partial_reasons
    assert "action_execution_unresolved" in page.partial_reasons
    assert all(edge.type != "executed_as" for edge in page.edges)


async def test_attack_graph_omits_cross_scope_or_non_active_relations_without_orphans() -> None:
    snapshot = replace(
        evidence_snapshot(),
        engagement_facts=(
            GraphEngagementFactSource(
                id="eng-fact-source",
                engagement_id="engagement-1",
                status="active",
                source_run_ids=("run-1",),
                source_execution_ids=("execution-1",),
            ),
            GraphEngagementFactSource(
                id="eng-fact-target",
                engagement_id="engagement-1",
                status="active",
                source_run_ids=("run-1",),
                artifact_ids=("artifact-1",),
            ),
            GraphEngagementFactSource(
                id="eng-fact-unresolved",
                engagement_id="engagement-1",
                status="active",
                source_run_ids=("run-1",),
                artifact_ids=("artifact-1",),
                unresolved=True,
            ),
            GraphEngagementFactSource(
                id="eng-fact-old",
                engagement_id="engagement-1",
                status="superseded",
                source_run_ids=("run-1",),
            ),
            GraphEngagementFactSource(
                id="eng-fact-foreign",
                engagement_id="engagement-2",
                status="active",
                source_run_ids=("run-1",),
            ),
            GraphEngagementFactSource(
                id="foreign-run-canary",
                engagement_id="engagement-1",
                status="active",
                source_run_ids=("run-foreign",),
            ),
        ),
        fact_relations=(
            GraphFactRelationSource(
                id="relation-valid",
                engagement_id="engagement-1",
                source_fact_id="eng-fact-source",
                target_fact_id="eng-fact-target",
                relation_type="discovered_on",
                source_run_id="run-1",
                source_execution_ids=("execution-1",),
            ),
            GraphFactRelationSource(
                id="relation-superseded",
                engagement_id="engagement-1",
                source_fact_id="eng-fact-source",
                target_fact_id="eng-fact-old",
                relation_type="leads_to",
                source_run_id="run-1",
            ),
            GraphFactRelationSource(
                id="relation-cross-engagement",
                engagement_id="engagement-2",
                source_fact_id="eng-fact-source",
                target_fact_id="eng-fact-target",
                relation_type="enables",
                source_run_id="run-1",
            ),
            GraphFactRelationSource(
                id="relation-unresolved",
                engagement_id="engagement-1",
                source_fact_id="eng-fact-source",
                target_fact_id="eng-fact-target",
                relation_type="exploits",
                source_run_id="run-1",
                unresolved=True,
            ),
            GraphFactRelationSource(
                id="relation-foreign-run-canary",
                engagement_id="engagement-1",
                source_fact_id="eng-fact-source",
                target_fact_id="foreign-run-canary",
                relation_type="leads_to",
                source_run_id="run-1",
            ),
        ),
    )
    service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.EVIDENCE)

    statuses = {node.id: node.status for node in page.nodes}
    assert statuses["engagement_fact:engagement-1:eng-fact-source"] == "unverified"
    assert statuses["engagement_fact:engagement-1:eng-fact-target"] == "unverified"
    assert statuses["engagement_fact:engagement-1:eng-fact-unresolved"] == "unverified"
    assert all(
        node.type == "engagement_fact"
        for node in page.nodes
        if node.id.startswith("engagement_fact:")
    )
    assert "engagement_fact:engagement-1:eng-fact-old" not in statuses
    assert "engagement_fact:engagement-2:eng-fact-foreign" not in statuses
    assert "engagement_fact:engagement-1:foreign-run-canary" not in statuses
    attack_edges = [edge for edge in page.edges if edge.id.startswith("fact_relation:")]
    assert [(edge.id, edge.type) for edge in attack_edges] == [
        ("fact_relation:engagement-1:relation-valid", "discovered_on")
    ]
    node_ids = set(statuses)
    assert all(edge.source in node_ids and edge.target in node_ids for edge in page.edges)
    assert set(page.partial_reasons) >= {
        "cross_engagement_source_omitted",
        "cross_run_source_omitted",
        "superseded_fact_omitted",
        "relation_endpoint_unavailable",
        "unresolved_relation_omitted",
        "engagement_fact_evidence_type_unavailable",
        "fact_relation_provenance_type_unavailable",
    }
    assert "foreign-run-canary" not in page.model_dump_json()


def test_attack_graph_sources_cannot_carry_fact_or_evidence_text() -> None:
    with pytest.raises(TypeError):
        GraphEngagementFactSource(  # type: ignore[call-arg]
            id="fact-1",
            engagement_id="engagement-1",
            status="active",
            source_run_ids=("run-1",),
            natural_language="SECRET-ENGAGEMENT-FACT-TEXT",
        )
    with pytest.raises(TypeError):
        GraphFactRelationSource(  # type: ignore[call-arg]
            id="relation-1",
            engagement_id="engagement-1",
            source_fact_id="fact-1",
            target_fact_id="fact-2",
            relation_type="enables",
            source_run_id="run-1",
            evidence_text="SECRET-RELATION-EVIDENCE",
        )


async def test_topology_signature_ignores_safe_status_changes() -> None:
    snapshot = source_snapshot()
    changed = replace(
        snapshot,
        actions=(replace(snapshot.actions[0], status="failed", tool_id="changed-tool"),),
    )
    first_service = GraphApplicationService(
        SnapshotRepository(snapshot),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    changed_service = GraphApplicationService(
        SnapshotRepository(changed),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )

    first = await first_service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.TASK)
    second = await changed_service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.TASK)
    assert first.snapshot_id == second.snapshot_id


async def test_source_hard_cap_is_distinct_from_normal_pagination_and_cursor_bound() -> None:
    snapshot = replace(
        _snapshot_with_actions(3),
        coverage=(
            GraphSourceCoverage(
                source="actions",
                scanned=4,
                limit=3,
                truncated=True,
            ),
        ),
    )
    repository = SnapshotRepository(snapshot)
    service = GraphApplicationService(
        repository,
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    page = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.TASK,
        limit=1,
    )

    assert page.has_more is True
    assert page.next_cursor is not None
    assert page.truncated is True
    assert "actions_source_limit" in page.partial_reasons

    repository.snapshot = replace(
        snapshot,
        coverage=(
            GraphSourceCoverage(
                source="actions",
                scanned=3,
                limit=3,
                truncated=False,
            ),
        ),
    )
    unchanged_topology = await service.get_view(
        "run-1",
        principal=PRINCIPAL,
        view=GraphViewKind.TASK,
        limit=100,
    )
    assert unchanged_topology.snapshot.topology_signature == page.snapshot.topology_signature
    assert unchanged_topology.snapshot_id != page.snapshot_id
    assert unchanged_topology.truncated is False
    with pytest.raises(StaleGraphCursorError):
        await service.get_view(
            "run-1",
            principal=PRINCIPAL,
            view=GraphViewKind.TASK,
            limit=1,
            cursor=page.next_cursor,
        )


@pytest.mark.parametrize(
    "coverage",
    [
        (GraphSourceCoverage(source="unknown", scanned=1, limit=1, truncated=False),),
        (GraphSourceCoverage(source="actions", scanned=True, limit=1, truncated=False),),
        (GraphSourceCoverage(source="actions", scanned=0, limit=1, truncated=True),),
        (
            GraphSourceCoverage(source="actions", scanned=1, limit=1, truncated=False),
            GraphSourceCoverage(source="actions", scanned=1, limit=1, truncated=False),
        ),
    ],
)
async def test_source_coverage_contract_fails_closed(
    coverage: tuple[GraphSourceCoverage, ...],
) -> None:
    service = GraphApplicationService(
        SnapshotRepository(replace(source_snapshot(), coverage=coverage)),
        authorizer=RecordingAuthorizer(),
        cursor_signing_key=CURSOR_SIGNING_KEY,
    )
    with pytest.raises(GraphSourceContractError):
        await service.get_view("run-1", principal=PRINCIPAL, view=GraphViewKind.TASK)
