"""Persistence contract tests for safe, Run-scoped Graph source snapshots."""

from __future__ import annotations

import re
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event

from riftx.application.graphs import (
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
    GraphScope,
    GraphSessionSource,
    GraphUserDecisionSource,
    GraphViewKind,
)
from riftx.persistence import Database, GraphReadLimits, SQLAlchemyGraphReadRepository
from riftx.persistence.orm import (
    AgentCycleRecord,
    AgentRuntimeStepRecord,
    AgentSessionRecord,
    ArtifactRecord,
    EngagementFactRecord,
    EngagementRecord,
    ExecutionRecord,
    FactRelationRecord,
    FindingRecord,
    NodeRecord,
    RunnerCredentialRecord,
    RunRecord,
    ToolCallIntentRecord,
    WorkingMemoryRecord,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
SECRET_CANARIES = (
    "artifact-path-secret",
    "artifact-description-secret",
    "execution-command-secret",
    "execution-argv-secret",
    "execution-env-secret",
    "execution-cwd-secret",
    "finding-description-secret",
    "finding-location-secret",
    "finding-evidence-secret",
    "fact-value-secret",
    "fact-natural-language-secret",
    "credential-token-cookie-secret",
)


def _database(tmp_path: Path, name: str) -> Database:
    return Database(f"sqlite+aiosqlite:///{tmp_path / name}")


async def _seed(
    database: Database,
    *,
    action_count: int,
    plan_item_count: int = 1,
    relation_count: int = 1,
) -> None:
    async with database.session_factory() as session, session.begin():
        session.add(
            EngagementRecord(
                id="engagement-graph",
                name="Graph persistence",
                description="",
                authorization_reference=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.flush()
        session.add(
            NodeRecord(
                id="node-graph",
                name="Graph node",
                platform="test",
                architecture="test",
                runner_version="test",
                status="online",
                capabilities_json=[],
                labels_json={"credential": "credential-token-cookie-secret"},
                current_runner_instance_id=None,
                current_runner_epoch=0,
                last_seen_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.flush()
        session.add(
            RunnerCredentialRecord(
                runner_instance_id="runner-graph",
                node_id="node-graph",
                runner_epoch=1,
                token_hash="credential-token-cookie-secret".ljust(64, "x"),
                token_prefix="secret-prefix",
                created_at=NOW,
                rotated_at=NOW,
                revoked_at=None,
            )
        )
        session.add(
            RunRecord(
                id="run-graph",
                engagement_id="engagement-graph",
                node_id="node-graph",
                objective="Graph projection",
                success_criteria_json=[],
                entry_points_json=[],
                scope_json={},
                status="running",
                approval_mode="manual",
                model_profile="test",
                workspace_path="/workspace/run-graph",
                temporal_workflow_id=None,
                created_at=NOW,
                started_at=NOW,
                finished_at=None,
            )
        )
        await session.flush()
        session.add(
            WorkingMemoryRecord(
                id="working-memory-graph",
                run_id="run-graph",
                version=3,
                state_json={
                    "run_plan": {
                        "items": [
                            {
                                "id": f"plan-item-{index}",
                                "task": "Sensitive task text is not projected",
                                "status": "running",
                                "sequence": index,
                                "completion_summary": None,
                            }
                            for index in range(1, plan_item_count + 1)
                        ]
                    },
                    "confirmed_facts": [
                        {
                            "id": "working-fact-secret",
                            "run_id": "run-graph",
                            "subject": "subject",
                            "predicate": "predicate",
                            "value": "fact-value-secret",
                            "natural_language": "fact-natural-language-secret",
                            "confidence": 1.0,
                            "status": "confirmed",
                            "source_refs": ["artifact-secret", "execution-000"],
                            "source_types": {
                                "artifact-secret": "deterministic_parser",
                                "execution-000": "deterministic_parser",
                            },
                            "first_observed_at": NOW.isoformat(),
                            "last_confirmed_at": NOW.isoformat(),
                            "supersedes_fact_id": None,
                        }
                    ],
                    "user_decisions": [
                        {
                            "id": "decision-secret",
                            "question": "credential-token-cookie-secret",
                            "decision": "credential-token-cookie-secret",
                            "reason": "credential-token-cookie-secret",
                            "source_ref": "artifact-secret",
                            "created_at": NOW.isoformat(),
                        }
                    ],
                    "hypotheses": [
                        {
                            "id": "hypothesis-graph",
                            "statement": "finding-description-secret",
                            "confidence": 1.0,
                            "status": "confirmed",
                            "supporting_fact_ids": ["working-fact-secret"],
                            "contradicting_fact_ids": [],
                            "next_validation_action": "execution-command-secret",
                        }
                    ],
                },
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.flush()
        session.add(
            AgentSessionRecord(
                id="session-graph",
                run_id="run-graph",
                parent_session_id=None,
                agent_type="primary",
                model_profile="test",
                status="running",
                latest_checkpoint_id=None,
                provider_state_id=None,
                turn_count=0,
                model_call_count=0,
                tool_call_count=action_count,
                created_at=NOW,
                closed_at=None,
            )
        )
        await session.flush()
        session.add(
            AgentCycleRecord(
                id="cycle-graph",
                run_id="run-graph",
                session_id="session-graph",
                sequence=1,
                status="running",
                yield_reason=None,
                waiting_object_id=None,
                checkpoint_id=None,
                model_call_count=0,
                tool_call_count=action_count,
                started_at=NOW,
                finished_at=None,
            )
        )
        await session.flush()
        session.add(
            AgentRuntimeStepRecord(
                id="step-graph",
                cycle_id="cycle-graph",
                sequence=1,
                step_type="tool_proposal",
                status="running",
                input_refs_json=[],
                output_refs_json=[],
                started_at=NOW,
                finished_at=None,
            )
        )
        await session.flush()

        intents: list[ToolCallIntentRecord] = []
        executions: list[ExecutionRecord] = []
        for index in range(action_count):
            action_id = f"action-{index:03d}"
            execution_id = f"execution-{index:03d}"
            intents.append(
                ToolCallIntentRecord(
                    id=action_id,
                    run_id="run-graph",
                    session_id="session-graph",
                    cycle_id="cycle-graph",
                    step_id="step-graph",
                    tool_id="safe-tool",
                    skill_id=None,
                    arguments_json={"secret": "credential-token-cookie-secret"},
                    command_preview="execution-command-secret",
                    reason="must not be returned",
                    target_summary="must not be returned",
                    approval_level="never",
                    status="completed",
                    claimed_execution_key=None,
                    claimed_attempt_group=None,
                    engine_call_id=f"engine-{index:03d}",
                    execution_spec_json={"secret": "credential-token-cookie-secret"},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            executions.append(
                ExecutionRecord(
                    id=execution_id,
                    execution_key=f"execution-key-{index:03d}",
                    launch_fingerprint=None,
                    run_id="run-graph",
                    session_id="session-graph",
                    tool_call_id=action_id,
                    attempt_group=None,
                    node_id="node-graph",
                    owner_runner_instance_id=None,
                    owner_runner_epoch=None,
                    executor_type="process",
                    argv_json=["execution-argv-secret"],
                    command_text="execution-command-secret",
                    tool_id="safe-tool",
                    tool_version=None,
                    executable_path=None,
                    cwd="execution-cwd-secret",
                    env_diff_json={"TOKEN": "execution-env-secret"},
                    platform_system="test",
                    platform_release="test",
                    platform_architecture="test",
                    status="completed",
                    pid=None,
                    process_group_id=None,
                    containment_id=None,
                    exit_code=0,
                    stdout_path="/output/execution-command-secret",
                    stderr_path="/output/execution-command-secret",
                    created_at=NOW,
                    process_created_at=None,
                    started_at=NOW,
                    finished_at=NOW,
                    physical_stop_confirmed_at=NOW,
                    updated_at=NOW,
                )
            )
        session.add_all([*intents, *executions])
        await session.flush()
        session.add_all(
            [
                ArtifactRecord(
                    id="artifact-secret",
                    run_id="run-graph",
                    execution_id=executions[0].id if executions else None,
                    name="metadata-only",
                    path="artifact-path-secret",
                    mime_type="text/plain",
                    sha256="a" * 64,
                    size=1,
                    description="artifact-description-secret",
                    created_at=NOW,
                ),
                FindingRecord(
                    id="finding-secret",
                    run_id="run-graph",
                    title="metadata-only",
                    severity="low",
                    status="open",
                    affected_assets_json=[],
                    description="finding-description-secret",
                    evidence_json=[
                        {
                            "artifact_id": "artifact-secret",
                            "execution_id": executions[0].id if executions else None,
                            "description": "finding-evidence-secret",
                            "location": "finding-location-secret",
                        }
                    ],
                    reproduction_steps_json=[],
                    impact="",
                    recommendation="",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                EngagementFactRecord(
                    id="engagement-fact-secret",
                    engagement_id="engagement-graph",
                    subject="metadata-only",
                    predicate="metadata-only",
                    value_json="fact-value-secret",
                    natural_language="fact-natural-language-secret",
                    evidence_refs_json=["artifact-secret"],
                    source_run_ids_json=["run-graph"],
                    source_session_ids_json=["session-graph"],
                    source_execution_ids_json=[
                        executions[0].id if executions else "unresolved-execution"
                    ],
                    artifact_ids_json=["artifact-secret"],
                    confidence=1.0,
                    valid_from=None,
                    valid_until=None,
                    supersedes_fact_id=None,
                    status="active",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                EngagementFactRecord(
                    id="engagement-fact-target",
                    engagement_id="engagement-graph",
                    subject="metadata-only-target",
                    predicate="metadata-only-target",
                    value_json="fact-value-secret",
                    natural_language="fact-natural-language-secret",
                    evidence_refs_json=["artifact-secret"],
                    source_run_ids_json=["run-graph"],
                    source_session_ids_json=["session-graph"],
                    source_execution_ids_json=[],
                    artifact_ids_json=["artifact-secret"],
                    confidence=1.0,
                    valid_from=None,
                    valid_until=None,
                    supersedes_fact_id=None,
                    status="active",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                FactRelationRecord(
                    id=(
                        "fact-relation-graph" if index == 0 else f"fact-relation-extra-{index:03d}"
                    ),
                    engagement_id="engagement-graph",
                    source_fact_id="engagement-fact-secret",
                    target_fact_id="engagement-fact-target",
                    relation_type=(
                        "enables",
                        "depends_on",
                        "leads_to",
                        "exploits",
                        "discovered_on",
                    )[index],
                    evidence_refs_json=["artifact-secret"],
                    source_run_id="run-graph",
                    source_session_id="session-graph",
                    source_execution_ids_json=[
                        executions[0].id if executions else "unresolved-execution"
                    ],
                    artifact_ids_json=["artifact-secret"],
                    confidence=1.0,
                    valid_until=None,
                    created_at=NOW,
                )
                for index in range(relation_count)
            ]
        )


async def _capture_selects(
    database: Database,
    operation: Awaitable[object],
    *,
    parameters: list[object] | None = None,
) -> tuple[object, tuple[str, ...]]:
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)
            if parameters is not None:
                parameters.append(_parameters)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await operation
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    return result, tuple(statements)


def _assert_artifact_select_projection_is_metadata_only(statements: tuple[str, ...]) -> None:
    rendered = "\n".join(statements).lower()
    selects = tuple(
        re.finditer(
            r"\bselect\b(.*?)\bfrom\b\s+([a-z0-9_\"`.]+)",
            rendered,
            flags=re.DOTALL,
        )
    )
    assert selects
    for selection in selects:
        projection = selection.group(1)
        from_target = selection.group(2).rsplit(".", 1)[-1].strip('"`')
        if from_target == "artifacts":
            assert (
                re.search(
                    r"(?:^|,)\s*(?:distinct\s+)?\*(?:\s|,|$)",
                    projection,
                    flags=re.DOTALL,
                )
                is None
            )
        assert re.search(r"\bartifacts\s*\.\s*\*", projection) is None
        for forbidden_column in (
            "artifacts.path",
            "artifacts.name",
            "artifacts.description",
            "artifacts.mime_type",
            "artifacts.sha256",
            "artifacts.size",
            "artifacts.content",
        ):
            assert forbidden_column not in projection
    assert "artifacts.name like" in rendered
    assert "artifacts.description =" in rendered


@pytest.mark.parametrize(
    "unsafe_projection",
    ["*", "DISTINCT *", "artifacts .*"],
)
def test_artifact_projection_gate_rejects_wildcards(unsafe_projection: str) -> None:
    statement = (
        f"SELECT {unsafe_projection} FROM artifacts "
        "WHERE artifacts.name LIKE ? AND artifacts.description = ?"
    )

    with pytest.raises(AssertionError):
        _assert_artifact_select_projection_is_metadata_only((statement,))


async def test_resolve_scope_is_server_derived_and_unknown_runs_are_indistinguishable(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "graph-scope.db")
    await database.create_schema()
    try:
        await _seed(database, action_count=1)
        repository = SQLAlchemyGraphReadRepository(database.session_factory)

        assert await repository.resolve_scope("run-graph") == GraphScope(
            run_id="run-graph",
            engagement_id="engagement-graph",
        )
        assert await repository.resolve_scope("run-foreign-or-missing") is None
    finally:
        await database.dispose()


def test_graph_read_limits_cannot_exceed_application_snapshot_budget() -> None:
    limits = GraphReadLimits()
    for source_name in GraphReadLimits.__dataclass_fields__:
        assert getattr(limits, source_name) <= 10_000
        with pytest.raises(ValueError):
            GraphReadLimits(**{source_name: 10_001})


@pytest.mark.parametrize("action_count", [1, 50])
async def test_snapshot_uses_constant_selects_and_only_allowlisted_source_fields(
    tmp_path: Path,
    action_count: int,
) -> None:
    database = _database(tmp_path, f"graph-task-source-{action_count}.db")
    await database.create_schema()
    try:
        await _seed(database, action_count=action_count)
        repository = SQLAlchemyGraphReadRepository(database.session_factory)
        scope = GraphScope(run_id="run-graph", engagement_id="engagement-graph")

        query_parameters: list[object] = []
        loaded, statements = await _capture_selects(
            database,
            repository.load(scope, GraphViewKind.TASK),
            parameters=query_parameters,
        )
        source = loaded

        assert len(statements) == 6
        assert source.scope == scope
        assert source.run.id == "run-graph"
        assert source.run.engagement_id == "engagement-graph"
        assert source.run.node_id == "node-graph"
        assert source.plan_items == (
            GraphPlanItemSource(
                id="plan-item-1",
                run_id="run-graph",
                sequence=1,
                status="running",
            ),
        )
        assert len(source.actions) == action_count
        assert source.actions[0].execution_ids == ("execution-000",)
        assert all(not hasattr(action, "plan_item_id") for action in source.actions)
        assert tuple(item.source for item in source.coverage) == (
            "plan_items",
            "actions",
            "findings",
            "artifacts",
            "executions",
        )
        assert source.facts == source.sessions == source.execution_hosts == ()

        rendered_source = repr(source)
        rendered_sql = "\n".join(statements).lower()
        for canary in SECRET_CANARIES:
            assert canary not in rendered_source
            assert canary not in rendered_sql
            assert canary not in repr(query_parameters)
        _assert_artifact_select_projection_is_metadata_only(statements)
        for forbidden_sql_fragment in (
            "arguments_json",
            "command_preview",
            "execution_spec_json",
            "artifacts.path",
            "artifacts.content",
            "target_http_requests.url",
            "target_http_requests.request_json",
            "target_http_requests.result_json",
            "executions.argv_json",
            "executions.command_text",
            "executions.cwd",
            "executions.env_diff_json",
            "executions.stdout_path",
            "executions.stderr_path",
            "findings.description",
            "engagement_facts.value_json",
            "engagement_facts.natural_language",
            "runner_credentials",
        ):
            assert forbidden_sql_fragment not in rendered_sql
    finally:
        await database.dispose()


async def test_evidence_and_operation_sources_are_explicit_same_run_metadata_only(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "graph-evidence-operation.db")
    await database.create_schema()
    try:
        await _seed(database, action_count=1)
        repository = SQLAlchemyGraphReadRepository(database.session_factory)
        scope = GraphScope(run_id="run-graph", engagement_id="engagement-graph")

        query_parameters: list[object] = []
        evidence, evidence_statements = await _capture_selects(
            database,
            repository.load(scope, GraphViewKind.EVIDENCE),
            parameters=query_parameters,
        )
        operation, operation_statements = await _capture_selects(
            database,
            repository.load(scope, GraphViewKind.OPERATION),
            parameters=query_parameters,
        )

        assert len(evidence_statements) == 15
        assert len(operation_statements) == 4
        assert tuple(item.source for item in evidence.coverage) == (
            "facts",
            "hypotheses",
            "user_decisions",
            "engagement_facts",
            "fact_relations",
            "findings",
            "artifacts",
            "executions",
            "sessions",
        )
        assert tuple(item.source for item in operation.coverage) == (
            "executions",
            "sessions",
            "execution_hosts",
        )
        assert evidence.plan_items == evidence.actions == evidence.execution_hosts == ()
        assert (
            operation.plan_items
            == operation.actions
            == operation.facts
            == operation.findings
            == operation.artifacts
            == ()
        )

        assert evidence.facts == (
            GraphFactSource(
                fact_id="working-fact-secret",
                run_id="run-graph",
                status="confirmed",
                evidence_refs=(
                    GraphEvidenceRefSource(
                        reference_id="artifact-secret",
                        source_type="deterministic_parser",
                    ),
                    GraphEvidenceRefSource(
                        reference_id="execution-000",
                        source_type="deterministic_parser",
                    ),
                ),
            ),
        )
        assert evidence.hypotheses == (
            GraphHypothesisSource(
                hypothesis_id="hypothesis-graph",
                run_id="run-graph",
                status="confirmed",
                supporting_fact_ids=("working-fact-secret",),
            ),
        )
        assert evidence.user_decisions == (
            GraphUserDecisionSource(
                decision_id="decision-secret",
                run_id="run-graph",
            ),
        )
        assert evidence.findings == (
            GraphFindingSource(
                finding_id="finding-secret",
                run_id="run-graph",
                status="open",
                severity="low",
                artifact_ids=("artifact-secret",),
                execution_ids=("execution-000",),
            ),
        )
        assert evidence.artifacts == (
            GraphArtifactSource(
                artifact_id="artifact-secret",
                run_id="run-graph",
                execution_id="execution-000",
            ),
        )
        assert evidence.executions == (
            GraphExecutionSource(
                execution_id="execution-000",
                run_id="run-graph",
                session_id="session-graph",
                node_id="node-graph",
                status="completed",
            ),
        )
        assert evidence.sessions == (
            GraphSessionSource(
                session_id="session-graph",
                run_id="run-graph",
                status="running",
            ),
        )
        assert operation.executions == evidence.executions
        assert operation.sessions == evidence.sessions
        assert operation.execution_hosts == (
            GraphExecutionHostSource(
                node_id="node-graph",
                run_id="run-graph",
                status="online",
            ),
        )
        assert evidence.engagement_facts == (
            GraphEngagementFactSource(
                id="engagement-fact-secret",
                engagement_id="engagement-graph",
                status="active",
                source_run_ids=("run-graph",),
                source_session_ids=("session-graph",),
                source_execution_ids=("execution-000",),
                artifact_ids=("artifact-secret",),
            ),
            GraphEngagementFactSource(
                id="engagement-fact-target",
                engagement_id="engagement-graph",
                status="active",
                source_run_ids=("run-graph",),
                source_session_ids=("session-graph",),
                artifact_ids=("artifact-secret",),
            ),
        )
        assert evidence.fact_relations == (
            GraphFactRelationSource(
                id="fact-relation-graph",
                engagement_id="engagement-graph",
                source_fact_id="engagement-fact-secret",
                target_fact_id="engagement-fact-target",
                relation_type="enables",
                source_run_id="run-graph",
                source_session_id="session-graph",
                source_execution_ids=("execution-000",),
                artifact_ids=("artifact-secret",),
            ),
        )

        rendered = repr((evidence, operation))
        rendered_sql = "\n".join((*evidence_statements, *operation_statements)).lower()
        for canary in SECRET_CANARIES:
            assert canary not in rendered
            assert canary not in rendered_sql
            assert canary not in repr(query_parameters)
        _assert_artifact_select_projection_is_metadata_only(evidence_statements)
        for forbidden_sql_fragment in (
            "artifacts.path",
            "artifacts.content",
            "target_http_requests.url",
            "target_http_requests.request_json",
            "target_http_requests.result_json",
            "executions.argv_json",
            "executions.command_text",
            "executions.cwd",
            "executions.env_diff_json",
            "executions.stdout_path",
            "executions.stderr_path",
            "findings.description",
            "engagement_facts.value_json",
            "engagement_facts.natural_language",
            "runner_credentials",
        ):
            assert forbidden_sql_fragment not in rendered_sql
    finally:
        await database.dispose()


@pytest.mark.parametrize("relation_count", [1, 5])
async def test_evidence_select_count_does_not_grow_with_fact_relations(
    tmp_path: Path,
    relation_count: int,
) -> None:
    database = _database(tmp_path, f"graph-relations-{relation_count}.db")
    await database.create_schema()
    try:
        await _seed(database, action_count=1, relation_count=relation_count)
        repository = SQLAlchemyGraphReadRepository(database.session_factory)
        scope = GraphScope(run_id="run-graph", engagement_id="engagement-graph")

        loaded, statements = await _capture_selects(
            database,
            repository.load(scope, GraphViewKind.EVIDENCE),
        )

        assert len(statements) == 15
        assert len(loaded.fact_relations) == relation_count
    finally:
        await database.dispose()


async def test_injected_task_limits_use_limit_plus_one_and_return_first_rows(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "graph-injected-limits.db")
    await database.create_schema()
    try:
        await _seed(database, action_count=3, plan_item_count=3)
        repository = SQLAlchemyGraphReadRepository(
            database.session_factory,
            limits=GraphReadLimits(
                plan_items=2,
                actions=2,
                executions=2,
            ),
        )
        scope = GraphScope(run_id="run-graph", engagement_id="engagement-graph")

        source = await repository.load(scope, GraphViewKind.TASK)

        assert tuple(item.id for item in source.plan_items) == (
            "plan-item-1",
            "plan-item-2",
        )
        assert tuple(item.action_id for item in source.actions) == (
            "action-000",
            "action-001",
        )
        assert tuple(item.execution_id for item in source.executions) == (
            "execution-000",
            "execution-001",
        )
        coverage = {item.source: item for item in source.coverage}
        for source_name in ("plan_items", "actions", "executions"):
            assert coverage[source_name].scanned == 3
            assert coverage[source_name].limit == 2
            assert coverage[source_name].truncated is True
    finally:
        await database.dispose()


async def test_cross_run_session_references_are_not_projected(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "graph-cross-run-sessions.db")
    await database.create_schema()
    try:
        await _seed(database, action_count=1)
        async with database.session_factory() as session, session.begin():
            session.add(
                RunRecord(
                    id="run-foreign",
                    engagement_id="engagement-graph",
                    node_id="node-graph",
                    objective="Foreign Run",
                    success_criteria_json=[],
                    entry_points_json=[],
                    scope_json={},
                    status="running",
                    approval_mode="manual",
                    model_profile="test",
                    workspace_path="/workspace/run-foreign",
                    temporal_workflow_id=None,
                    created_at=NOW,
                    started_at=NOW,
                    finished_at=None,
                )
            )
            await session.flush()
            session.add(
                AgentSessionRecord(
                    id="session-foreign",
                    run_id="run-foreign",
                    parent_session_id=None,
                    agent_type="primary",
                    model_profile="test",
                    status="running",
                    latest_checkpoint_id=None,
                    provider_state_id=None,
                    turn_count=0,
                    model_call_count=0,
                    tool_call_count=0,
                    created_at=NOW,
                    closed_at=None,
                )
            )
            await session.flush()
            execution = await session.get(ExecutionRecord, "execution-000")
            local_session = await session.get(AgentSessionRecord, "session-graph")
            assert execution is not None and local_session is not None
            execution.session_id = "session-foreign"
            local_session.parent_session_id = "session-foreign"

        repository = SQLAlchemyGraphReadRepository(database.session_factory)
        scope = GraphScope(run_id="run-graph", engagement_id="engagement-graph")

        operation, statements = await _capture_selects(
            database,
            repository.load(scope, GraphViewKind.OPERATION),
        )
        task = await repository.load(scope, GraphViewKind.TASK)

        assert len(statements) == 4
        assert operation.executions[0].session_id is None
        assert operation.sessions[0].parent_session_id is None
        assert task.actions[0].execution_ids == ()
    finally:
        await database.dispose()
