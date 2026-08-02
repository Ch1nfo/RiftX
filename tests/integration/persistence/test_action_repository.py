from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event

import riftx.persistence.action_repositories as action_repository_module
from riftx.application.actions import ActionCorrelationQuality, ActionPartialReason
from riftx.application.errors import ResourceNotAccessibleError
from riftx.application.services.actions import ActionApplicationService
from riftx.domain import LocalPrincipal, OperatorCapability
from riftx.persistence import Database
from riftx.persistence.action_read_queries import (
    build_action_detail_event_query,
    build_action_finding_query,
    build_action_list_event_query,
)
from riftx.persistence.action_repositories import SQLAlchemyActionReadRepository
from riftx.persistence.orm import (
    AgentCycleRecord,
    AgentRuntimeStepRecord,
    AgentSessionRecord,
    ApprovalRecord,
    ArtifactRecord,
    EngagementRecord,
    ExecutionRecord,
    FindingRecord,
    RunEventRecord,
    RunRecord,
    RuntimeApprovalRequestRecord,
    ToolCallIntentRecord,
    ToolCallRecord,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
PRINCIPAL = LocalPrincipal(
    id="local-principal:v1:action-persistence-test",
    capabilities=frozenset({OperatorCapability.READ}),
)


class _Authorizer:
    def require_child_run(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        capability: OperatorCapability,
    ) -> None:
        assert principal == PRINCIPAL
        assert capability is OperatorCapability.READ
        if resource_run_id != parent_run_id:
            raise ResourceNotAccessibleError(
                "resource_not_accessible",
                "The requested resource was not found",
            )


def _database(tmp_path: Path, name: str = "actions.db") -> Database:
    return Database(f"sqlite+aiosqlite:///{tmp_path / name}")


async def _seed_foundation(database: Database, *run_ids: str) -> None:
    async with database.session_factory() as session:
        session.add(
            EngagementRecord(
                id="engagement-actions",
                name="Action repository",
                description="",
                authorization_reference=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            RunRecord(
                kind="general",
                id=run_id,
                engagement_id="engagement-actions",
                node_id=f"node-{run_id}",
                objective="Action projection",
                success_criteria_json=[],
                entry_points_json=[],
                scope_json={},
                status="running",
                approval_mode="manual",
                model_profile="test",
                workspace_path=f"/workspace/{run_id}",
                temporal_workflow_id=None,
                created_at=NOW,
                started_at=NOW,
                finished_at=None,
            )
            for run_id in run_ids
        )
        await session.flush()
        session.add_all(
            AgentSessionRecord(
                id=f"session-{run_id}",
                run_id=run_id,
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
            for run_id in run_ids
        )
        await session.flush()
        session.add_all(
            AgentCycleRecord(
                id=f"cycle-{run_id}",
                run_id=run_id,
                session_id=f"session-{run_id}",
                sequence=1,
                status="running",
                yield_reason=None,
                waiting_object_id=None,
                checkpoint_id=None,
                model_call_count=0,
                tool_call_count=0,
                started_at=NOW,
                finished_at=None,
            )
            for run_id in run_ids
        )
        await session.flush()
        session.add_all(
            AgentRuntimeStepRecord(
                id=f"step-{run_id}",
                cycle_id=f"cycle-{run_id}",
                sequence=1,
                step_type="tool_proposal",
                status="running",
                input_refs_json=[],
                output_refs_json=[],
                started_at=NOW,
                finished_at=None,
            )
            for run_id in run_ids
        )
        await session.commit()


def _intent(
    action_id: str,
    *,
    run_id: str = "run-1",
    created_at: datetime = NOW,
    claim: tuple[str, str] | None = None,
) -> ToolCallIntentRecord:
    return ToolCallIntentRecord(
        id=action_id,
        run_id=run_id,
        session_id=f"session-{run_id}",
        cycle_id=f"cycle-{run_id}",
        step_id=f"step-{run_id}",
        tool_id="python",
        skill_id=None,
        arguments_json={"secret": "detail-only", "ordinal": action_id},
        command_preview="must not be selected",
        reason=f"reason for {action_id}",
        target_summary="test target",
        approval_level="never",
        status="executing" if claim is not None else "proposed",
        claimed_execution_key=claim[0] if claim is not None else None,
        claimed_attempt_group=claim[1] if claim is not None else None,
        engine_call_id=f"engine-{action_id}",
        execution_spec_json={"unsafe": "not projected"},
        created_at=created_at,
        updated_at=created_at,
    )


def _execution(
    action_id: str,
    index: int,
    *,
    run_id: str = "run-1",
    session_id: str | None = None,
    created_at: datetime | None = None,
    attempt_group: str = "attempts",
    status: str = "running",
) -> ExecutionRecord:
    execution_id = f"execution-{action_id}-{index:03d}"
    timestamp = created_at if created_at is not None else NOW + timedelta(seconds=index)
    return ExecutionRecord(
        id=execution_id,
        execution_key=f"key-{action_id}-{index:03d}-{run_id}",
        launch_fingerprint="launch:v1:test",
        run_id=run_id,
        session_id=session_id or f"session-{run_id}",
        tool_call_id=action_id,
        attempt_group=attempt_group,
        node_id=f"node-{run_id}",
        owner_runner_instance_id=None,
        owner_runner_epoch=None,
        executor_type="process",
        argv_json=["sh", "-c", "printf secret"],
        command_text="printf secret",
        tool_id="python",
        tool_version="test",
        executable_path="/usr/bin/python",
        cwd="/sensitive/workspace",
        env_diff_json={"TOKEN": "must-not-leak"},
        platform_system="test",
        platform_release="test",
        platform_architecture="test",
        status=status,
        pid=None,
        process_group_id=None,
        containment_id=None,
        exit_code=0 if status in {"completed", "exited"} else None,
        stdout_path="/sensitive/stdout",
        stderr_path="/sensitive/stderr",
        created_at=timestamp,
        process_created_at=None,
        started_at=timestamp,
        finished_at=(
            timestamp + timedelta(seconds=1) if status in {"completed", "exited"} else None
        ),
        physical_stop_confirmed_at=(
            timestamp + timedelta(seconds=1) if status in {"completed", "exited"} else None
        ),
        updated_at=timestamp,
    )


async def _insert(database: Database, records: Iterable[object]) -> None:
    async with database.session_factory() as session:
        session.add_all(list(records))
        await session.commit()


def _service(database: Database) -> ActionApplicationService:
    return ActionApplicationService(
        SQLAlchemyActionReadRepository(database.session_factory),
        authorizer=_Authorizer(),
    )


async def _count_selects(database: Database, operation: object) -> tuple[object, list[str]]:
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

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await operation  # type: ignore[misc]
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    return result, statements


@pytest.mark.parametrize("action_count", [1, 50, 100])
async def test_nonempty_list_and_detail_service_flows_use_exactly_seven_selects(
    tmp_path: Path,
    action_count: int,
) -> None:
    database = _database(tmp_path, f"query-count-{action_count}.db")
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    await _insert(
        database,
        (
            _intent(
                f"action-{index:02d}",
                created_at=NOW + timedelta(minutes=index),
            )
            for index in range(action_count)
        ),
    )
    try:
        listed, list_selects = await _count_selects(
            database,
            _service(database).list("run-1", principal=PRINCIPAL, limit=100),
        )
        detailed, detail_selects = await _count_selects(
            database,
            _service(database).get("run-1", "action-00", principal=PRINCIPAL),
        )

        assert len(listed.items) == action_count  # type: ignore[union-attr]
        assert detailed.action_id == "action-00"  # type: ignore[union-attr]
        assert len(list_selects) == 7, "\n\n".join(list_selects)
        assert len(detail_selects) == 7, "\n\n".join(detail_selects)

        list_sql = "\n".join(list_selects).lower()
        for forbidden in (
            ".arguments_json",
            ".feedback",
            ".command_json",
            ".command_text",
            ".command_preview",
            ".cwd",
            ".env_diff_json",
            ".argv_json",
            ".stdout_path",
            ".stderr_path",
            ".path",
            ".size",
        ):
            assert forbidden not in list_sql
        assert "executions.node_id as execution_node_id" in list_sql

        detail_sql = "\n".join(detail_selects).lower()
        for forbidden in (
            ".command_json",
            ".command_text",
            ".command_preview",
            ".cwd",
            ".env_diff_json",
            ".argv_json",
            ".stdout_path",
            ".stderr_path",
            ".path",
            ".size",
        ):
            assert forbidden not in detail_sql
    finally:
        await database.dispose()


async def test_high_cardinality_hydration_bounds_materialized_rows_without_losing_exact_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path, "bounded-hydration.db")
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    action_id = "action-bounded"
    executions = [_execution(action_id, index, status="completed") for index in range(105)]
    claimed = executions[0]
    artifact_base = NOW + timedelta(days=1)
    finding_base = NOW + timedelta(days=2)
    finding_update_base = NOW + timedelta(days=3)
    event_base = NOW + timedelta(days=4)
    await _insert(
        database,
        [
            _intent(
                action_id,
                claim=(claimed.execution_key, claimed.attempt_group or "attempts"),
            ),
            *executions,
        ],
    )
    await _insert(
        database,
        [
            *(
                ArtifactRecord(
                    id=f"artifact-budget-{index:03d}",
                    run_id="run-1",
                    execution_id=execution.id,
                    name=f"artifact {index}",
                    path=f"/sensitive/budget/{index}",
                    mime_type="text/plain",
                    sha256=f"{index:064x}",
                    size=index + 1,
                    description="must not be projected",
                    created_at=artifact_base + timedelta(seconds=index),
                )
                for index, execution in enumerate(executions)
            ),
        ],
    )
    await _insert(
        database,
        [
            *(
                FindingRecord(
                    id=f"finding-budget-{index:03d}",
                    run_id="run-1",
                    title=f"Finding {index}",
                    severity="info",
                    status="draft",
                    affected_assets_json=[],
                    description="must not be projected",
                    evidence_json=[
                        {
                            "execution_id": execution.id,
                            "artifact_id": f"artifact-budget-{index:03d}",
                            "description": "must not be projected",
                            "location": f"/sensitive/finding/{index}",
                        }
                    ],
                    reproduction_steps_json=[],
                    impact="",
                    recommendation="",
                    created_at=finding_base + timedelta(seconds=index),
                    updated_at=finding_update_base + timedelta(seconds=index),
                )
                for index, execution in enumerate(executions)
            ),
            *(
                RunEventRecord(
                    id=f"event-budget-{index:03d}",
                    run_id="run-1",
                    sequence=index + 1,
                    event_type="action.row_budget",
                    payload_json={
                        "execution_id": executions[index % len(executions)].id,
                        "opaque": "must not be projected",
                    },
                    created_at=event_base + timedelta(seconds=index),
                )
                for index in range(205)
            ),
        ],
    )

    original_rows = action_repository_module._rows
    materialized_rows: dict[str, int] = {}

    async def record_rows(session: Any, statement: Any) -> tuple[Any, ...]:
        rows = await original_rows(session, statement)
        phase = statement.get_execution_options().get("riftx_action_phase")
        assert isinstance(phase, str) and phase
        assert phase not in materialized_rows
        materialized_rows[phase] = len(rows)
        return rows

    monkeypatch.setattr(action_repository_module, "_rows", record_rows)
    try:
        listed, list_selects = await _count_selects(
            database,
            _service(database).list("run-1", principal=PRINCIPAL),
        )
        list_rows = dict(materialized_rows)
        materialized_rows.clear()
        detailed, detail_selects = await _count_selects(
            database,
            _service(database).get("run-1", action_id, principal=PRINCIPAL),
        )
        detail_rows = dict(materialized_rows)

        assert len(list_selects) == len(detail_selects) == 7
        assert len(list_rows) == len(detail_rows) == 6
        for phase_rows in (list_rows, detail_rows):
            assert set(phase_rows) == {
                "root",
                "approval",
                "execution",
                "artifact",
                "finding",
                "event",
            }
            roots = phase_rows["root"]
            assert roots == 1
            assert phase_rows["approval"] <= roots
            assert phase_rows["execution"] <= roots * 100
            assert phase_rows["artifact"] <= roots * 100
            assert phase_rows["finding"] <= roots * 100
            assert phase_rows["event"] <= roots * 200
        assert list_rows["finding"] == list_rows["event"] == 1

        item = listed.items[0]  # type: ignore[union-attr]
        assert item.execution_count == detailed.execution_count == 105  # type: ignore[union-attr]
        assert len(item.attempts) == len(detailed.executions) == 100  # type: ignore[union-attr]
        assert [attempt.node_id for attempt in item.attempts] == [  # type: ignore[union-attr]
            execution.node_id
            for execution in detailed.executions  # type: ignore[union-attr]
        ]
        assert [attempt.exit_code for attempt in item.attempts] == [  # type: ignore[union-attr]
            execution.exit_code
            for execution in detailed.executions  # type: ignore[union-attr]
        ]
        assert item.attempt_coverage.scanned == detailed.attempt_coverage.scanned == 100  # type: ignore[union-attr]
        assert item.attempt_coverage.limit == detailed.attempt_coverage.limit == 100  # type: ignore[union-attr]
        assert item.attempt_coverage.truncated is detailed.attempt_coverage.truncated is True  # type: ignore[union-attr]
        assert item.current_execution_id == detailed.current_execution_id == claimed.id  # type: ignore[union-attr]
        assert item.artifact_count == detailed.result.artifact_count == 105  # type: ignore[union-attr]
        assert len(item.artifact_ids) == len(detailed.result.artifact_ids) == 100  # type: ignore[union-attr]
        assert item.artifacts_truncated is detailed.result.truncated is True  # type: ignore[union-attr]
        assert item.finding_count == detailed.evidence.finding_count == 105  # type: ignore[union-attr]
        assert len(detailed.evidence.finding_ids) == 100  # type: ignore[union-attr]
        assert item.finding_coverage.scanned == detailed.evidence.finding_coverage.scanned == 100  # type: ignore[union-attr]
        assert (
            item.finding_coverage.truncated is detailed.evidence.finding_coverage.truncated is True
        )  # type: ignore[union-attr]
        assert item.event_count == detailed.evidence.event_count == 205  # type: ignore[union-attr]
        assert len(detailed.evidence.events) == 200  # type: ignore[union-attr]
        assert item.event_coverage.scanned == detailed.evidence.event_coverage.scanned == 200  # type: ignore[union-attr]
        assert item.event_coverage.truncated is detailed.evidence.event_coverage.truncated is True  # type: ignore[union-attr]
        assert "artifact-budget-001" in detailed.result.artifact_ids  # type: ignore[union-attr]
        assert "finding-budget-001" in detailed.evidence.finding_ids  # type: ignore[union-attr]
        assert any(
            event.event_id == "event-budget-001"
            for event in detailed.evidence.events  # type: ignore[union-attr]
        )
        exact_high_water = event_base + timedelta(seconds=204)
        assert item.updated_at == detailed.updated_at == exact_high_water  # type: ignore[union-attr]
        assert item.version == detailed.version  # type: ignore[union-attr]
        assert "must not be projected" not in repr(item)
        assert "must not be projected" not in repr(detailed)
    finally:
        await database.dispose()


async def test_json_reference_projection_is_value_minimal_and_fail_safe(tmp_path: Path) -> None:
    database = _database(tmp_path, "json-reference-boundary.db")
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    action_id = "action-json-boundary"
    canary = "RIFTX_ACTION_OPAQUE_JSON_CANARY_DO_NOT_HYDRATE"
    execution = _execution(action_id, 0)
    finding_id = "finding-json-boundary"
    event_payloads = (
        (
            "event-invalid-tool-call-intent-ref",
            {"tool_call_intent_id": 17, "action_id": action_id, "opaque": canary},
        ),
        (
            "event-invalid-action-ref",
            {"action_id": {"opaque": canary}, "execution_id": execution.id},
        ),
        (
            "event-invalid-approval-ref",
            {"tool_call_intent_id": action_id, "approval_id": 23, "opaque": canary},
        ),
        (
            "event-invalid-execution-ref",
            {
                "tool_call_intent_id": action_id,
                "execution_id": {"opaque": canary},
            },
        ),
        (
            "event-invalid-artifact-ref",
            {"tool_call_intent_id": action_id, "artifact_id": 29, "opaque": canary},
        ),
        (
            "event-null-refs",
            {
                "tool_call_intent_id": action_id,
                "action_id": None,
                "approval_id": None,
                "execution_id": None,
                "artifact_id": None,
                "opaque": canary,
            },
        ),
    )
    await _insert(
        database,
        [
            _intent(
                action_id,
                claim=(execution.execution_key, execution.attempt_group or "attempts"),
            ),
            execution,
            FindingRecord(
                id=finding_id,
                run_id="run-1",
                title="Mixed-shape evidence",
                severity="high",
                status="draft",
                affected_assets_json=[],
                description="must not be projected",
                evidence_json=[
                    None,
                    canary,
                    {"execution_id": 31},
                    {"artifact_id": {"opaque": canary}},
                    {
                        "execution_id": execution.id,
                        "artifact_id": None,
                        "description": canary,
                        "location": f"/sensitive/{canary}",
                    },
                ],
                reproduction_steps_json=[],
                impact="",
                recommendation="",
                created_at=NOW + timedelta(minutes=1),
                updated_at=NOW + timedelta(minutes=1),
            ),
            *(
                RunEventRecord(
                    id=event_id,
                    run_id="run-1",
                    sequence=sequence,
                    event_type="action.json_reference_probe",
                    payload_json=payload,
                    created_at=NOW + timedelta(minutes=1, seconds=sequence),
                )
                for sequence, (event_id, payload) in enumerate(event_payloads, start=1)
            ),
        ],
    )

    try:
        async with database.session_factory() as session:
            finding_list_rows = (
                (await session.execute(build_action_finding_query((action_id,)))).mappings().all()
            )
            finding_detail_rows = (
                (await session.execute(build_action_finding_query((action_id,), detail=True)))
                .mappings()
                .all()
            )
            list_event_rows = (
                (await session.execute(build_action_list_event_query((action_id,))))
                .mappings()
                .all()
            )
            detail_event_rows = (
                (await session.execute(build_action_detail_event_query((action_id,))))
                .mappings()
                .all()
            )

        finding_summary_keys = {
            "action_id",
            "finding_count",
            "finding_max_created_at",
            "finding_max_updated_at",
            "finding_partial",
        }
        finding_detail_keys = finding_summary_keys | {
            "finding_id",
            "finding_run_id",
            "finding_created_at",
            "finding_updated_at",
        }
        event_summary_keys = {
            "action_id",
            "event_count",
            "event_max_created_at",
            "event_partial",
        }
        event_detail_keys = event_summary_keys | {
            "event_id",
            "event_run_id",
            "event_created_at",
            "event_sequence",
            "event_type",
        }
        assert finding_list_rows
        assert finding_detail_rows
        assert list_event_rows
        assert detail_event_rows
        assert all(set(row) == finding_summary_keys for row in finding_list_rows)
        assert all(set(row) == finding_detail_keys for row in finding_detail_rows)
        assert all(set(row) == event_summary_keys for row in list_event_rows)
        assert all(set(row) == event_detail_keys for row in detail_event_rows)
        projected = [
            *(dict(row) for row in finding_list_rows),
            *(dict(row) for row in finding_detail_rows),
            *(dict(row) for row in list_event_rows),
            *(dict(row) for row in detail_event_rows),
        ]
        assert canary not in repr(projected)
        assert all(
            "evidence_json" not in row and "finding_evidence" not in row
            for row in finding_detail_rows
        )
        assert all(
            "payload_json" not in row and "event_payload" not in row for row in detail_event_rows
        )

        assert finding_list_rows[0]["finding_count"] == 1
        assert finding_list_rows[0]["finding_partial"] is True
        assert {row["finding_id"] for row in finding_detail_rows} == {finding_id}
        assert list_event_rows[0]["event_count"] == len(event_payloads)
        assert list_event_rows[0]["event_partial"] is True
        assert {row["event_id"] for row in detail_event_rows} == {
            event_id for event_id, _payload in event_payloads
        }
        invalid_event_ids = {
            event_id for event_id, _payload in event_payloads if event_id != "event-null-refs"
        }

        repository = SQLAlchemyActionReadRepository(database.session_factory)
        aggregate = await repository.get("run-1", action_id)
        page = await repository.list_page(
            "run-1",
            limit=10,
            after=None,
            snapshot=None,
        )
        assert aggregate is not None
        # A malformed sibling is partial evidence, not a veto on the exact known sibling.
        assert aggregate.finding_ids == (finding_id,)
        attached_event_ids = {item.event_id for item in aggregate.events}
        assert attached_event_ids == {event_id for event_id, _payload in event_payloads}
        assert invalid_event_ids < attached_event_ids
        assert ActionPartialReason.FINDING_EVIDENCE_UNRESOLVED in aggregate.partial_reasons
        assert ActionPartialReason.EVENT_CORRELATION_PARTIAL in aggregate.partial_reasons
        assert page.items[0].finding_count == 1
        assert page.items[0].event_count == len(event_payloads)
        assert ActionPartialReason.FINDING_EVIDENCE_UNRESOLVED in page.items[0].partial_reasons
        assert ActionPartialReason.EVENT_CORRELATION_PARTIAL in page.items[0].partial_reasons
        assert canary not in repr(aggregate)
        assert canary not in repr(page)
    finally:
        await database.dispose()


async def test_cross_action_well_typed_references_are_unresolved_for_list_and_detail(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "cross-action-references.db")
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    first = _execution("action-first", 0)
    second = _execution("action-second", 0)
    await _insert(
        database,
        [
            _intent(
                "action-first",
                claim=(first.execution_key, first.attempt_group or "attempts"),
            ),
            _intent(
                "action-second",
                claim=(second.execution_key, second.attempt_group or "attempts"),
            ),
            first,
            second,
            FindingRecord(
                id="finding-cross-action",
                run_id="run-1",
                title="Ambiguous ownership",
                severity="high",
                status="draft",
                affected_assets_json=[],
                description="must not be projected",
                evidence_json=[
                    {"execution_id": first.id},
                    {"execution_id": second.id},
                ],
                reproduction_steps_json=[],
                impact="",
                recommendation="",
                created_at=NOW + timedelta(minutes=1),
                updated_at=NOW + timedelta(minutes=1),
            ),
            RunEventRecord(
                id="event-cross-action",
                run_id="run-1",
                sequence=1,
                event_type="action.competing_references",
                payload_json={
                    "action_id": "action-first",
                    "execution_id": second.id,
                },
                created_at=NOW + timedelta(minutes=1, seconds=1),
            ),
        ],
    )

    try:
        listed, list_statements = await _count_selects(
            database,
            _service(database).list("run-1", principal=PRINCIPAL, limit=10),
        )
        first_detail, first_detail_statements = await _count_selects(
            database,
            _service(database).get("run-1", "action-first", principal=PRINCIPAL),
        )
        second_detail, second_detail_statements = await _count_selects(
            database,
            _service(database).get("run-1", "action-second", principal=PRINCIPAL),
        )
        items = {item.action_id: item for item in listed.items}  # type: ignore[union-attr]

        assert len(list_statements) == 7, "\n\n".join(list_statements)
        assert len(first_detail_statements) == 7, "\n\n".join(first_detail_statements)
        assert len(second_detail_statements) == 7, "\n\n".join(second_detail_statements)
        assert set(items) == {"action-first", "action-second"}
        expected_reasons = {
            ActionPartialReason.FINDING_EVIDENCE_UNRESOLVED,
            ActionPartialReason.EVENT_CORRELATION_PARTIAL,
        }
        for item in items.values():
            assert item.finding_count == 0
            assert item.event_count == 0
            assert set(item.partial_reasons) == expected_reasons

        details = (first_detail, second_detail)
        for detail in details:
            assert detail.evidence.finding_ids == ()  # type: ignore[union-attr]
            assert detail.evidence.finding_count == 0  # type: ignore[union-attr]
            assert detail.evidence.events == ()  # type: ignore[union-attr]
            assert detail.evidence.event_count == 0  # type: ignore[union-attr]
            assert set(detail.partial_reasons) == expected_reasons  # type: ignore[union-attr]
    finally:
        await database.dispose()


async def test_off_page_execution_claim_keeps_list_and_detail_reference_resolution_partial(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "off-page-claim.db")
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    execution = _execution("action-visible", 0)
    claim = (execution.execution_key, execution.attempt_group or "attempts")
    await _insert(
        database,
        [
            _intent(
                "action-visible",
                created_at=NOW + timedelta(minutes=2),
                claim=claim,
            ),
            _intent("action-sentinel", created_at=NOW + timedelta(minutes=1)),
            _intent("action-off-page", created_at=NOW, claim=claim),
            execution,
            RunEventRecord(
                id="event-off-page-claim",
                run_id="run-1",
                sequence=1,
                event_type="action.off_page_claim",
                payload_json={"execution_id": execution.id},
                created_at=NOW + timedelta(minutes=3),
            ),
        ],
    )

    try:
        service = _service(database)
        listed = await service.list("run-1", principal=PRINCIPAL, limit=1)
        detailed = await service.get("run-1", "action-visible", principal=PRINCIPAL)

        assert [item.action_id for item in listed.items] == ["action-visible"]
        item = listed.items[0]
        assert item.event_count == detailed.evidence.event_count == 0
        assert item.version == detailed.version
        assert ActionPartialReason.EVENT_CORRELATION_PARTIAL in item.partial_reasons
        assert ActionPartialReason.EVENT_CORRELATION_PARTIAL in detailed.partial_reasons
    finally:
        await database.dispose()


async def test_limit_plus_one_sentinel_is_fully_hydrated(tmp_path: Path) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    old_execution = _execution("action-old", 0)
    await _insert(
        database,
        [
            _intent("action-old", created_at=NOW),
            _intent("action-new", created_at=NOW + timedelta(minutes=1)),
            old_execution,
        ],
    )
    await _insert(
        database,
        [
            ArtifactRecord(
                id="artifact-sentinel",
                run_id="run-1",
                execution_id=old_execution.id,
                name="proof",
                path="/must/not/be/read",
                mime_type="text/plain",
                sha256="a" * 64,
                size=999_999,
                description="must not be selected",
                created_at=NOW + timedelta(seconds=2),
            ),
            RunEventRecord(
                id="event-sentinel",
                run_id="run-1",
                sequence=1,
                event_type="action.test",
                payload_json={"tool_call_intent_id": "action-old", "secret": "ignored"},
                created_at=NOW + timedelta(seconds=3),
            ),
        ],
    )
    try:
        page = await SQLAlchemyActionReadRepository(database.session_factory).list_page(
            "run-1",
            limit=1,
            after=None,
            snapshot=None,
        )

        assert page.has_more is True
        assert [item.intent.action_id for item in page.items] == ["action-new", "action-old"]
        sentinel = page.items[1]
        assert [item.execution_id for item in sentinel.executions] == [old_execution.id]
        assert sentinel.result.artifact_ids == ("artifact-sentinel",)
        assert sentinel.event_count == 1
    finally:
        await database.dispose()


async def test_repository_keyset_is_stable_for_equal_timestamps_and_newer_inserts(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    await _insert(
        database,
        [_intent(action_id, created_at=NOW) for action_id in ("action-a", "action-b", "action-c")],
    )
    try:
        service = _service(database)
        first = await service.list("run-1", principal=PRINCIPAL, limit=1)
        assert [item.action_id for item in first.items] == ["action-c"]
        assert first.next_cursor is not None

        await _insert(
            database,
            [_intent("action-newer", created_at=NOW + timedelta(minutes=1))],
        )
        second = await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=1,
            cursor=first.next_cursor,
        )
        assert [item.action_id for item in second.items] == ["action-b"]
        assert second.next_cursor is not None

        third = await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=1,
            cursor=second.next_cursor,
        )
        assert [item.action_id for item in third.items] == ["action-a"]
        assert all(
            item.action_id != "action-newer"
            for page in (first, second, third)
            for item in page.items
        )
    finally:
        await database.dispose()


async def test_approval_bridge_uses_shared_approval_id_not_public_tool_call_id(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    approval_time = NOW + timedelta(minutes=5)
    await _insert(
        database,
        [
            _intent("action-approval"),
            ToolCallRecord(
                id="legacy-public-tool-call-poison",
                sdk_call_id="sdk-poison",
                run_id="run-1",
                agent_step_id="step-run-1",
                tool_id="python",
                skill_id=None,
                arguments_json={},
                approval_status="approved",
                execution_id=None,
                created_at=NOW,
            ),
        ],
    )
    await _insert(
        database,
        [
            RuntimeApprovalRequestRecord(
                id="approval-shared",
                run_id="run-1",
                session_id="session-run-1",
                cycle_id="cycle-run-1",
                tool_call_intent_id="action-approval",
                context_compilation_id=None,
                working_memory_version=None,
                provider_state_id=None,
                status="approved",
                decision="approve_once",
                feedback="runtime feedback",
                decided_by=PRINCIPAL.id,
                created_at=NOW,
                decided_at=approval_time,
            ),
            ApprovalRecord(
                id="approval-shared",
                run_id="run-1",
                tool_call_id="legacy-public-tool-call-poison",
                status="approved",
                tool_name="python",
                command_json=["must", "not", "be", "selected"],
                cwd="/must/not/be/selected",
                target_summary="target",
                env_diff_json={"TOKEN": "must-not-leak"},
                reason="public approval",
                decision="approve_once",
                decision_feedback="public feedback",
                decided_by=PRINCIPAL.id,
                created_at=NOW,
                decided_at=approval_time,
            ),
            RunEventRecord(
                id="event-approval-only",
                run_id="run-1",
                sequence=1,
                event_type="tool.approval_resolved",
                payload_json={"approval_id": "approval-shared"},
                created_at=approval_time + timedelta(seconds=1),
            ),
        ],
    )
    try:
        repository = SQLAlchemyActionReadRepository(database.session_factory)
        aggregate = await repository.get("run-1", "action-approval")
        page = await repository.list_page("run-1", limit=10, after=None, snapshot=None)

        assert aggregate is not None and aggregate.approval is not None
        assert aggregate.approval.approval_id == "approval-shared"
        assert aggregate.approval.runtime_status == "approved"
        assert aggregate.approval.public_status == "approved"
        assert aggregate.approval.feedback == "runtime feedback"
        assert [item.event_id for item in aggregate.events] == ["event-approval-only"]
        assert aggregate.updated_at == approval_time + timedelta(seconds=1)
        assert page.items[0].approval is not None
        assert page.items[0].approval.approval_id == "approval-shared"
        assert page.items[0].event_count == 1
    finally:
        await database.dispose()


async def test_cross_scope_approval_is_redacted_and_does_not_advance_high_water(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1", "run-poison")
    await _insert(
        database,
        [
            _intent("action-cross-scope"),
            ToolCallRecord(
                id="tool-call-cross-scope",
                sdk_call_id="sdk-cross-scope",
                run_id="run-poison",
                agent_step_id="step-run-poison",
                tool_id="python",
                skill_id=None,
                arguments_json={},
                approval_status="approved",
                execution_id=None,
                created_at=NOW,
            ),
        ],
    )
    poison_time = NOW + timedelta(days=30)
    await _insert(
        database,
        [
            RuntimeApprovalRequestRecord(
                id="approval-cross-scope",
                run_id="run-poison",
                session_id="session-run-poison",
                cycle_id="cycle-run-poison",
                tool_call_intent_id="action-cross-scope",
                context_compilation_id=None,
                working_memory_version=None,
                provider_state_id=None,
                status="approved",
                decision="approve_once",
                feedback="RIFTX_TEST_SECRET_DO_NOT_LEAK_APPROVAL",
                decided_by="foreign-actor",
                created_at=poison_time,
                decided_at=poison_time,
            ),
            ApprovalRecord(
                id="approval-cross-scope",
                run_id="run-poison",
                tool_call_id="tool-call-cross-scope",
                status="approved",
                tool_name="python",
                command_json=[],
                cwd="/foreign",
                target_summary="foreign",
                env_diff_json={},
                reason="foreign",
                decision="approve_once",
                decision_feedback="foreign feedback",
                decided_by="foreign-actor",
                created_at=poison_time,
                decided_at=poison_time,
            ),
        ],
    )
    try:
        repository = SQLAlchemyActionReadRepository(database.session_factory)
        aggregate = await repository.get("run-1", "action-cross-scope")
        page = await repository.list_page("run-1", limit=10, after=None, snapshot=None)

        assert aggregate is not None and aggregate.approval is not None
        assert aggregate.approval.approval_id == "approval-cross-scope"
        assert aggregate.approval.runtime_status is None
        assert aggregate.approval.public_status is None
        assert aggregate.approval.runtime_decided_by is None
        assert aggregate.approval.public_decided_by is None
        assert aggregate.approval.runtime_decided_at is None
        assert aggregate.approval.public_decided_at is None
        assert aggregate.approval.feedback is None
        assert ActionPartialReason.APPROVAL_SCOPE_MISMATCH in aggregate.partial_reasons
        assert aggregate.updated_at == NOW
        assert page.items[0].approval is not None
        assert page.items[0].approval.runtime_status is None
        assert page.items[0].approval.public_status is None
        assert page.items[0].updated_at == NOW
    finally:
        await database.dispose()


async def test_claim_null_preserves_multiple_exact_legacy_attempts_without_current(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    await _insert(
        database,
        [_intent("action-legacy"), _execution("action-legacy", 0), _execution("action-legacy", 1)],
    )
    try:
        aggregate = await SQLAlchemyActionReadRepository(database.session_factory).get(
            "run-1", "action-legacy"
        )

        assert aggregate is not None
        assert {item.execution_id for item in aggregate.executions} == {
            "execution-action-legacy-000",
            "execution-action-legacy-001",
        }
        assert all(
            item.correlation_quality is ActionCorrelationQuality.LEGACY
            for item in aggregate.executions
        )
        assert aggregate.current_execution_id is None
        assert (
            ActionPartialReason.EXECUTION_CURRENT_CORRELATION_PARTIAL in aggregate.partial_reasons
        )
    finally:
        await database.dispose()


async def test_legacy_execution_with_null_created_at_is_projected_fail_safe(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    legacy = _execution("action-null-clock", 0)
    legacy.created_at = None
    await _insert(
        database,
        [
            _intent(
                "action-null-clock",
                claim=(legacy.execution_key, legacy.attempt_group or "attempts"),
            ),
            legacy,
        ],
    )
    try:
        aggregate = await SQLAlchemyActionReadRepository(database.session_factory).get(
            "run-1", "action-null-clock"
        )

        assert aggregate is not None
        assert aggregate.current_execution_id == legacy.id
        assert len(aggregate.executions) == 1
        assert aggregate.executions[0].created_at is None
    finally:
        await database.dispose()


async def test_namespace_poison_is_excluded_without_removing_exact_attempts(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1", "run-poison")
    exact = _execution("action-exact", 0)
    poison = _execution("action-exact", 9, run_id="run-poison")
    await _insert(
        database,
        [
            _intent(
                "action-exact",
                claim=(exact.execution_key, exact.attempt_group or "attempts"),
            ),
            exact,
            poison,
        ],
    )
    try:
        aggregate = await SQLAlchemyActionReadRepository(database.session_factory).get(
            "run-1", "action-exact"
        )

        assert aggregate is not None
        assert [item.execution_id for item in aggregate.executions] == [exact.id]
        assert aggregate.current_execution_id == exact.id
        assert ActionPartialReason.EXECUTION_SCOPE_MISMATCH in aggregate.partial_reasons
        assert aggregate.updated_at == exact.updated_at
    finally:
        await database.dispose()


async def test_unique_active_attempt_without_durable_claim_is_not_current(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    active = _execution("action-unclaimed", 0)
    await _insert(database, [_intent("action-unclaimed"), active])
    try:
        repository = SQLAlchemyActionReadRepository(database.session_factory)
        aggregate = await repository.get("run-1", "action-unclaimed")
        detail = await _service(database).get("run-1", "action-unclaimed", principal=PRINCIPAL)
        listed = await _service(database).list("run-1", principal=PRINCIPAL)

        assert aggregate is not None
        assert aggregate.current_execution_id is None
        assert detail.current_execution_id is None
        assert listed.items[0].current_execution_id is None
        assert detail.latest_execution_id == active.id
    finally:
        await database.dispose()


async def test_exact_claimed_terminal_attempt_remains_current(tmp_path: Path) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    terminal = _execution("action-terminal", 0, status="completed")
    await _insert(
        database,
        [
            _intent(
                "action-terminal",
                claim=(terminal.execution_key, terminal.attempt_group or "attempts"),
            ),
            terminal,
        ],
    )
    try:
        repository = SQLAlchemyActionReadRepository(database.session_factory)
        aggregate = await repository.get("run-1", "action-terminal")
        detail = await _service(database).get("run-1", "action-terminal", principal=PRINCIPAL)
        listed = await _service(database).list("run-1", principal=PRINCIPAL)

        assert aggregate is not None
        assert aggregate.current_execution_id == terminal.id
        assert detail.current_execution_id == terminal.id
        assert listed.items[0].current_execution_id == terminal.id
    finally:
        await database.dispose()


async def test_claimed_older_attempt_is_current_while_newer_attempt_is_latest(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    older = _execution("action-history", 0, status="completed")
    newer = _execution("action-history", 1, status="completed")
    await _insert(
        database,
        [
            _intent(
                "action-history",
                claim=(older.execution_key, older.attempt_group or "attempts"),
            ),
            older,
            newer,
        ],
    )
    try:
        repository = SQLAlchemyActionReadRepository(database.session_factory)
        aggregate = await repository.get("run-1", "action-history")
        detail = await _service(database).get("run-1", "action-history", principal=PRINCIPAL)
        listed = await _service(database).list("run-1", principal=PRINCIPAL)

        assert aggregate is not None
        assert aggregate.current_execution_id == older.id
        assert detail.current_execution_id == older.id
        assert detail.latest_execution_id == newer.id
        assert listed.items[0].current_execution_id == older.id
        assert listed.items[0].latest_execution_id == newer.id
    finally:
        await database.dispose()


async def test_claim_outside_attempt_window_is_pinned_once_and_current_survives_truncation(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    attempts = [_execution("action-pinned", index, status="completed") for index in range(105)]
    claimed = attempts[0]
    await _insert(
        database,
        [
            _intent(
                "action-pinned",
                claim=(claimed.execution_key, claimed.attempt_group or "attempts"),
            ),
            *attempts,
        ],
    )
    try:
        repository = SQLAlchemyActionReadRepository(database.session_factory)
        aggregate = await repository.get("run-1", "action-pinned")
        detail = await _service(database).get("run-1", "action-pinned", principal=PRINCIPAL)
        listed = await _service(database).list("run-1", principal=PRINCIPAL)

        assert aggregate is not None
        returned_ids = [item.execution_id for item in aggregate.executions]
        assert aggregate.execution_count == 105
        assert len(returned_ids) == len(set(returned_ids))
        assert claimed.id in returned_ids
        assert returned_ids.count(claimed.id) == 1
        omitted_ids = {item.id for item in attempts} - set(returned_ids)
        assert omitted_ids
        assert claimed.id not in omitted_ids
        assert aggregate.current_execution_id == claimed.id
        assert len(returned_ids) <= aggregate.execution_coverage.scanned
        assert aggregate.execution_coverage.scanned <= aggregate.execution_coverage.limit
        assert aggregate.execution_coverage.truncated is True

        for view in (detail, listed.items[0]):
            assert view.current_execution_id == claimed.id
            assert view.latest_execution_id is None
            assert view.attempt_coverage.truncated is True
            assert ActionPartialReason.EXECUTION_ATTEMPTS_TRUNCATED in view.partial_reasons
        assert listed.items[0].version == detail.version
    finally:
        await database.dispose()


async def test_application_fails_closed_when_current_pointer_is_not_in_attempt_tuple(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    claimed = _execution("action-invalid-current", 0)
    await _insert(
        database,
        [
            _intent(
                "action-invalid-current",
                claim=(claimed.execution_key, claimed.attempt_group or "attempts"),
            ),
            claimed,
        ],
    )
    try:
        repository = SQLAlchemyActionReadRepository(database.session_factory)
        aggregate = await repository.get("run-1", "action-invalid-current")
        assert aggregate is not None and aggregate.current_execution_id == claimed.id
        invalid = replace(aggregate, current_execution_id="execution-not-returned")

        class InvalidCurrentRepository:
            async def resolve_action_run(self, run_id: str, action_id: str) -> str | None:
                return run_id if action_id == "action-invalid-current" else None

            async def get(self, run_id: str, action_id: str) -> object:
                return invalid

        service = ActionApplicationService(  # type: ignore[arg-type]
            InvalidCurrentRepository(),
            authorizer=_Authorizer(),
        )
        with pytest.raises(RuntimeError, match="invalid Action aggregate"):
            await service.get("run-1", "action-invalid-current", principal=PRINCIPAL)
    finally:
        await database.dispose()


async def test_evidence_beyond_attempt_coverage_is_retained_and_outputs_are_metadata_only(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    executions = [_execution("action-wide", index) for index in range(101)]
    await _insert(
        database,
        [
            _intent(
                "action-wide",
                claim=(executions[0].execution_key, executions[0].attempt_group or "attempts"),
            ),
            *executions,
        ],
    )
    try:
        repository = SQLAlchemyActionReadRepository(database.session_factory)
        before_evidence = await repository.get("run-1", "action-wide")
        assert before_evidence is not None
        visible_ids = {item.execution_id for item in before_evidence.executions}
        assert executions[0].id in visible_ids
        assert before_evidence.current_execution_id == executions[0].id
        omitted_ids = {item.id for item in executions} - visible_ids
        assert len(omitted_ids) == 1
        omitted_id = omitted_ids.pop()
        assert omitted_id != executions[0].id
        await _insert(
            database,
            [
                ArtifactRecord(
                    id="artifact-hidden-attempt",
                    run_id="run-1",
                    execution_id=omitted_id,
                    name="hidden proof",
                    path="/secret/hidden-proof",
                    mime_type="text/plain",
                    sha256="b" * 64,
                    size=42_000,
                    description="hidden child",
                    created_at=NOW + timedelta(hours=1),
                ),
                FindingRecord(
                    id="finding-hidden-attempt",
                    run_id="run-1",
                    title="Evidence on truncated execution",
                    severity="high",
                    status="draft",
                    affected_assets_json=[],
                    description="must not be projected",
                    evidence_json=[
                        {
                            "execution_id": omitted_id,
                            "artifact_id": "artifact-hidden-attempt",
                            "description": "proof",
                            "location": None,
                        }
                    ],
                    reproduction_steps_json=[],
                    impact="",
                    recommendation="",
                    created_at=NOW + timedelta(hours=1),
                    updated_at=NOW + timedelta(hours=1),
                ),
                RunEventRecord(
                    id="event-hidden-attempt",
                    run_id="run-1",
                    sequence=1,
                    event_type="execution.hidden",
                    payload_json={"execution_id": omitted_id, "secret": "ignored"},
                    created_at=NOW + timedelta(hours=2),
                ),
            ],
        )
        aggregate = await repository.get("run-1", "action-wide")

        assert aggregate is not None
        assert aggregate.execution_count == 101
        assert aggregate.execution_coverage.truncated is True
        assert len(aggregate.executions) == aggregate.execution_coverage.limit
        assert omitted_id not in {item.execution_id for item in aggregate.executions}
        assert aggregate.result.artifact_ids == ("artifact-hidden-attempt",)
        assert aggregate.result.artifact_count == 1
        assert aggregate.finding_ids == ("finding-hidden-attempt",)
        assert aggregate.finding_count == 1
        assert [item.event_id for item in aggregate.events] == ["event-hidden-attempt"]
        assert aggregate.event_count == 1
        assert aggregate.updated_at == NOW + timedelta(hours=2)
        assert aggregate.result.output_available is False
        assert aggregate.result.output_size == 0
        assert all(item.error_summary is None for item in aggregate.executions)
    finally:
        await database.dispose()


async def test_action_projection_survives_database_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "restart-actions.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _seed_foundation(database, "run-1")
    execution = _execution("action-restart", 0)
    await _insert(
        database,
        [
            _intent(
                "action-restart",
                claim=(execution.execution_key, execution.attempt_group or "attempts"),
            ),
            execution,
        ],
    )
    before = await SQLAlchemyActionReadRepository(database.session_factory).get(
        "run-1", "action-restart"
    )
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    try:
        after = await SQLAlchemyActionReadRepository(reopened.session_factory).get(
            "run-1", "action-restart"
        )
        assert before is not None
        assert after == before
    finally:
        await reopened.dispose()
