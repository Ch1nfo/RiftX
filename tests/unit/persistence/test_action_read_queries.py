from __future__ import annotations

import ast
import inspect
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, create_engine
from sqlalchemy.dialects import sqlite

from riftx.application.actions import ActionPageKey
from riftx.persistence import action_repositories
from riftx.persistence.action_read_queries import (
    DETAIL_SELECTED_COLUMN_KEYS,
    LIST_SELECTED_COLUMN_KEYS,
    build_action_artifact_query,
    build_action_detail_approval_query,
    build_action_detail_event_query,
    build_action_detail_execution_query,
    build_action_detail_root_query,
    build_action_finding_query,
    build_action_list_approval_query,
    build_action_list_event_query,
    build_action_list_execution_query,
    build_action_list_root_query,
)
from riftx.persistence.orm import (
    ArtifactRecord,
    Base,
    ExecutionRecord,
    FindingRecord,
    RunEventRecord,
    RuntimeApprovalRequestRecord,
    ToolCallIntentRecord,
)

_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
_QUERY_NAMES = {"roots", "approvals", "executions", "artifacts", "findings", "events"}
_LIST_FORBIDDEN = {
    "arguments_json",
    "feedback",
    "decision_feedback",
    "execution_node_id",
    "command_json",
    "command_text",
    "command_preview",
    "cwd",
    "env_diff_json",
    "argv_json",
    "stdout_path",
    "stderr_path",
    "path",
    "size",
    "evidence_json",
    "payload_json",
}
_DETAIL_FORBIDDEN = {
    "command_json",
    "command_text",
    "command_preview",
    "cwd",
    "env_diff_json",
    "argv_json",
    "stdout_path",
    "stderr_path",
    "path",
    "size",
    "evidence_json",
    "payload_json",
}


def _normalise(mapping: object) -> dict[str, set[str]]:
    assert isinstance(mapping, Mapping)
    assert set(mapping) == _QUERY_NAMES
    return {name: {str(value) for value in values} for name, values in mapping.items()}


def _selected_keys(statement: object) -> set[str]:
    return {str(column.key) for column in statement.selected_columns}  # type: ignore[attr-defined]


def test_list_physical_projection_excludes_detail_and_sensitive_columns() -> None:
    selected = _normalise(LIST_SELECTED_COLUMN_KEYS)

    for query_name, keys in selected.items():
        leaking = keys & _LIST_FORBIDDEN
        assert not leaking, f"{query_name} selects forbidden list columns: {sorted(leaking)}"

    assert "id" in selected["roots"]
    assert "created_at" in selected["roots"]
    assert "approval_count" in selected["approvals"]
    assert "artifact_execution_id" in selected["artifacts"]
    assert selected["findings"] == {
        "action_id",
        "finding_count",
        "finding_max_created_at",
        "finding_max_updated_at",
        "finding_partial",
    }
    assert selected["events"] == {
        "action_id",
        "event_count",
        "event_max_created_at",
        "event_partial",
    }


def test_detail_projection_still_excludes_execution_output_and_artifact_locations() -> None:
    selected = _normalise(DETAIL_SELECTED_COLUMN_KEYS)

    for query_name, keys in selected.items():
        leaking = keys & _DETAIL_FORBIDDEN
        assert not leaking, f"{query_name} selects forbidden detail columns: {sorted(leaking)}"

    assert "arguments_json" in selected["roots"]
    assert "feedback" in selected["approvals"]
    assert "execution_node_id" in selected["executions"]


def test_bounded_query_final_projections_are_opaque_and_allowlisted() -> None:
    list_execution = build_action_list_execution_query(("action-1",), ())
    detail_execution = build_action_detail_execution_query(("action-1",), ())
    artifact = build_action_artifact_query(("action-1",))
    list_finding = build_action_finding_query(("action-1",), detail=False)
    detail_finding = build_action_finding_query(("action-1",), detail=True)
    list_event = build_action_list_event_query(("action-1",))
    detail_event = build_action_detail_event_query(("action-1",))

    assert _selected_keys(list_execution) == set(LIST_SELECTED_COLUMN_KEYS["executions"])
    assert _selected_keys(detail_execution) == set(DETAIL_SELECTED_COLUMN_KEYS["executions"])
    assert _selected_keys(artifact) == set(LIST_SELECTED_COLUMN_KEYS["artifacts"])
    assert _selected_keys(list_finding) == set(LIST_SELECTED_COLUMN_KEYS["findings"])
    assert _selected_keys(detail_finding) == set(DETAIL_SELECTED_COLUMN_KEYS["findings"])
    assert _selected_keys(list_event) == set(LIST_SELECTED_COLUMN_KEYS["events"])
    assert _selected_keys(detail_event) == set(DETAIL_SELECTED_COLUMN_KEYS["events"])

    finding_sql = str(
        detail_finding.compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    list_execution_sql = str(
        list_execution.compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    list_event_sql = str(
        list_event.compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "json_each" in finding_sql
    assert ".node_id" not in list_execution_sql
    assert ".event_type" not in list_event_sql
    assert ".sequence" not in list_event_sql


def test_approval_queries_project_duplicate_count_and_cap_to_one_row_per_root() -> None:
    list_statement = build_action_list_approval_query(("action-1",))
    detail_statement = build_action_detail_approval_query(("action-1",))

    assert "approval_count" in _selected_keys(list_statement)
    assert "runtime_feedback" not in _selected_keys(list_statement)
    assert "runtime_feedback" in _selected_keys(detail_statement)
    sql = str(
        detail_statement.compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "row_number() over" in sql
    assert "approval_rank = 1" in sql


def test_list_root_query_is_descending_keyset_with_limit_plus_one() -> None:
    after = ActionPageKey(datetime(2026, 8, 1, 11, tzinfo=UTC), "action-after")
    snapshot = ActionPageKey(datetime(2026, 8, 1, 12, tzinfo=UTC), "action-snapshot")

    statement = build_action_list_root_query(
        "run-1",
        limit=7,
        after=after,
        snapshot=snapshot,
    )
    sql = str(
        statement.compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "order by" in sql
    assert "created_at desc" in sql
    assert "id desc" in sql
    assert "limit 8" in sql
    assert "action-after" in sql
    assert "action-snapshot" in sql
    assert "arguments_json" not in sql
    assert "command_preview" not in sql


def test_detail_root_query_is_scoped_to_run_and_action_without_output_columns() -> None:
    statement = build_action_detail_root_query("run-1", "action-1")
    sql = str(
        statement.compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "run-1" in sql
    assert "action-1" in sql
    assert "stdout_path" not in sql
    assert "stderr_path" not in sql
    assert "command_text" not in sql
    assert "argv_json" not in sql


def _intent_values(
    action_id: str,
    *,
    claim: tuple[str, str] | None,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "run_id": "run-1",
        "session_id": "session-run-1",
        "cycle_id": "cycle-run-1",
        "step_id": "step-run-1",
        "tool_id": "python",
        "skill_id": None,
        "arguments_json": {"opaque": action_id},
        "command_preview": "must not be projected",
        "reason": "bounded query test",
        "target_summary": "test",
        "approval_level": "never",
        "status": "executing" if claim is not None else "proposed",
        "claimed_execution_key": claim[0] if claim is not None else None,
        "claimed_attempt_group": claim[1] if claim is not None else None,
        "engine_call_id": f"engine-{action_id}",
        "execution_spec_json": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _execution_values(action_id: str, index: int) -> dict[str, Any]:
    created_at = _NOW + timedelta(seconds=index)
    return {
        "id": f"execution-{action_id}-{index:03d}",
        "execution_key": f"key-{action_id}-{index:03d}",
        "run_id": "run-1",
        "session_id": "session-run-1",
        "tool_call_id": action_id,
        "attempt_group": "attempts",
        "node_id": "node-1",
        "executor_type": "process",
        "argv_json": ["true"],
        "cwd": "/opaque",
        "env_diff_json": {},
        "platform_system": "test",
        "platform_release": "test",
        "platform_architecture": "test",
        "status": "completed",
        "exit_code": 0,
        "stdout_path": "/opaque/stdout",
        "stderr_path": "/opaque/stderr",
        "created_at": created_at,
        "started_at": created_at,
        "finished_at": created_at,
        "updated_at": created_at,
    }


def _seed_high_cardinality(connection: Connection, action_ids: Sequence[str]) -> None:
    intents: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    for action_index, action_id in enumerate(action_ids):
        action_executions = [_execution_values(action_id, index) for index in range(105)]
        executions.extend(action_executions)
        intents.append(
            _intent_values(
                action_id,
                claim=(str(action_executions[0]["execution_key"]), "attempts"),
            )
        )
        approvals.append(
            {
                "id": f"approval-{action_id}",
                "run_id": "run-1",
                "session_id": "session-run-1",
                "cycle_id": "cycle-run-1",
                "tool_call_intent_id": action_id,
                "status": "approved",
                "created_at": _NOW,
            }
        )
        for index, execution in enumerate(action_executions):
            artifacts.append(
                {
                    "id": f"artifact-{action_id}-{index:03d}",
                    "run_id": "run-1",
                    "execution_id": execution["id"],
                    "name": "artifact",
                    "path": "/opaque/artifact",
                    "mime_type": "text/plain",
                    "sha256": f"{action_index:02x}{index:062x}",
                    "size": index + 1,
                    "description": "opaque",
                    "created_at": _NOW + timedelta(days=1, seconds=index),
                }
            )
            findings.append(
                {
                    "id": f"finding-{action_id}-{index:03d}",
                    "run_id": "run-1",
                    "title": "finding",
                    "severity": "info",
                    "status": "draft",
                    "affected_assets_json": [],
                    "description": "opaque",
                    "evidence_json": [
                        {
                            "execution_id": execution["id"],
                            "artifact_id": f"artifact-{action_id}-{index:03d}",
                        }
                    ],
                    "reproduction_steps_json": [],
                    "impact": "",
                    "recommendation": "",
                    "created_at": _NOW + timedelta(days=2, seconds=index),
                    "updated_at": _NOW + timedelta(days=3, seconds=index),
                }
            )
        for index in range(205):
            execution = action_executions[index % len(action_executions)]
            events.append(
                {
                    "id": f"event-{action_id}-{index:03d}",
                    "run_id": "run-1",
                    "sequence": action_index * 1000 + index + 1,
                    "event_type": "action.bounded",
                    "payload_json": {"execution_id": execution["id"], "opaque": "secret"},
                    "created_at": _NOW + timedelta(days=4, seconds=index),
                }
            )

    connection.execute(ToolCallIntentRecord.__table__.insert(), intents)
    connection.execute(RuntimeApprovalRequestRecord.__table__.insert(), approvals)
    connection.execute(ExecutionRecord.__table__.insert(), executions)
    connection.execute(ArtifactRecord.__table__.insert(), artifacts)
    connection.execute(FindingRecord.__table__.insert(), findings)
    connection.execute(RunEventRecord.__table__.insert(), events)


def _rows(connection: Connection, statement: object) -> list[Mapping[str, Any]]:
    return list(connection.execute(statement).mappings())  # type: ignore[arg-type]


def _group_ids(
    rows: Sequence[Mapping[str, Any]],
    *,
    id_key: str,
) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        value = row[id_key]
        if isinstance(value, str):
            grouped[str(row["action_id"])].add(value)
    return grouped


def test_sqlite_high_cardinality_queries_materialize_exact_per_root_budgets() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    action_ids = ("action-visible", "action-sentinel")
    with engine.begin() as connection:
        _seed_high_cardinality(connection, action_ids)

        list_approvals = _rows(connection, build_action_list_approval_query(action_ids))
        detail_approvals = _rows(connection, build_action_detail_approval_query(action_ids))
        list_executions = _rows(
            connection,
            build_action_list_execution_query(action_ids, ()),
        )
        detail_executions = _rows(
            connection,
            build_action_detail_execution_query(action_ids, ()),
        )
        artifacts = _rows(connection, build_action_artifact_query(action_ids))
        list_findings = _rows(
            connection,
            build_action_finding_query(action_ids, detail=False),
        )
        detail_findings = _rows(
            connection,
            build_action_finding_query(action_ids, detail=True),
        )
        list_events = _rows(connection, build_action_list_event_query(action_ids))
        detail_events = _rows(connection, build_action_detail_event_query(action_ids))

    assert len(list_approvals) == len(detail_approvals) == len(action_ids)
    assert len(list_executions) == len(detail_executions) == len(action_ids) * 100
    assert len(artifacts) == len(action_ids) * 100
    assert len(list_findings) == len(action_ids)
    assert len(detail_findings) == len(action_ids) * 100
    assert len(list_events) == len(action_ids)
    assert len(detail_events) == len(action_ids) * 200

    for rows, count_key, expected_count in (
        (list_executions, "execution_count", 105),
        (detail_executions, "execution_count", 105),
        (artifacts, "artifact_count", 105),
        (list_findings, "finding_count", 105),
        (detail_findings, "finding_count", 105),
        (list_events, "event_count", 205),
        (detail_events, "event_count", 205),
    ):
        assert {row[count_key] for row in rows} == {expected_count}

    execution_ids = _group_ids(detail_executions, id_key="execution_id")
    artifact_ids = _group_ids(artifacts, id_key="artifact_id")
    finding_ids = _group_ids(detail_findings, id_key="finding_id")
    event_ids = _group_ids(detail_events, id_key="event_id")
    for action_id in action_ids:
        # The uniquely claimed oldest attempt is pinned by replacing rank 100,
        # leaving the 99 newest attempts plus the durable current attempt.
        assert f"execution-{action_id}-000" in execution_ids[action_id]
        assert f"execution-{action_id}-005" not in execution_ids[action_id]
        assert len(execution_ids[action_id]) == 100
        assert artifact_ids[action_id] == {
            f"artifact-{action_id}-{index:03d}" for index in range(100)
        }
        assert finding_ids[action_id] == {
            f"finding-{action_id}-{index:03d}" for index in range(100)
        }
        assert event_ids[action_id] == {f"event-{action_id}-{index:03d}" for index in range(200)}

    assert {row["event_max_created_at"] for row in detail_events} == {
        _NOW + timedelta(days=4, seconds=204)
    }


def test_global_off_page_claim_keeps_list_and_detail_reference_resolution_partial() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    execution = _execution_values("action-visible", 0)
    with engine.begin() as connection:
        connection.execute(
            ToolCallIntentRecord.__table__.insert(),
            [
                _intent_values(
                    "action-visible",
                    claim=(str(execution["execution_key"]), "attempts"),
                ),
                _intent_values(
                    "action-off-page",
                    claim=(str(execution["execution_key"]), "attempts"),
                ),
                _intent_values("action-sentinel", claim=None),
            ],
        )
        connection.execute(ExecutionRecord.__table__.insert(), [execution])
        connection.execute(
            FindingRecord.__table__.insert(),
            [
                {
                    "id": "finding-competing-claim",
                    "run_id": "run-1",
                    "title": "finding",
                    "severity": "info",
                    "status": "draft",
                    "affected_assets_json": [],
                    "description": "",
                    "evidence_json": [{"execution_id": execution["id"]}],
                    "reproduction_steps_json": [],
                    "impact": "",
                    "recommendation": "",
                    "created_at": _NOW,
                    "updated_at": _NOW,
                }
            ],
        )
        connection.execute(
            RunEventRecord.__table__.insert(),
            [
                {
                    "id": "event-competing-claim",
                    "run_id": "run-1",
                    "sequence": 1,
                    "event_type": "action.competing_claim",
                    "payload_json": {"execution_id": execution["id"]},
                    "created_at": _NOW,
                }
            ],
        )
        list_finding = _rows(
            connection,
            build_action_finding_query(("action-visible",), detail=False),
        )[0]
        detail_finding = _rows(
            connection,
            build_action_finding_query(("action-visible",), detail=True),
        )[0]
        list_event = _rows(
            connection,
            build_action_list_event_query(("action-visible",)),
        )[0]
        detail_event = _rows(
            connection,
            build_action_detail_event_query(("action-visible",)),
        )[0]

    assert list_finding["finding_count"] == detail_finding["finding_count"] == 0
    assert list_finding["finding_partial"] is detail_finding["finding_partial"] is True
    assert detail_finding["finding_id"] is None
    assert list_event["event_count"] == detail_event["event_count"] == 0
    assert list_event["event_partial"] is detail_event["event_partial"] is True
    assert detail_event["event_id"] is None


def test_repository_has_no_runner_output_or_filesystem_read_dependency() -> None:
    source = inspect.getsource(action_repositories)
    tree = ast.parse(source)
    imported_modules = {
        module
        for node in ast.walk(tree)
        for module in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    }

    assert not any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for module in imported_modules
        for forbidden in ("os", "pathlib", "subprocess", "riftx.execution", "riftx.runner")
    )
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"
        for node in ast.walk(tree)
    )
    assert list(
        inspect.signature(action_repositories.SQLAlchemyActionReadRepository).parameters
    ) == ["session_factory"]
    assert all(
        f'riftx_action_phase="{phase}"' in source
        for phase in ("root", "approval", "execution", "artifact", "finding", "event")
    )
