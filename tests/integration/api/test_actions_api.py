"""Focused HTTP contract tests for the read-only Run Action projection."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event

from riftx.api import APISettings, build_control_plane, create_app
from riftx.application.services import ActionApplicationService
from riftx.domain import OperatorCapability, TrustProfile
from riftx.persistence import Database, SQLAlchemyActionReadRepository
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
    TargetHttpRequestRecord,
    ToolCallIntentRecord,
    ToolCallRecord,
)
from riftx.security import LocalObjectAuthorizer

NOW = datetime(2026, 8, 2, 9, tzinfo=UTC)
LOCAL_TOKEN = "test-only-action-api-local-operator-token-0001"


@dataclass(frozen=True, slots=True)
class _APIHarness:
    app: FastAPI
    database: Database
    headers: dict[str, str]
    principal_id: str


def _production_settings(tmp_path: Path) -> APISettings:
    tools_path = tmp_path / "production-tools.yaml"
    tools_path.write_text("version: 1\nexecution_policy: registered_only\ntools: {}\n")
    models_path = tmp_path / "production-models.yaml"
    models_path.write_text(
        """\
default_profile: primary
models:
  primary:
    provider: openai_compatible
    model: test-model
    api: chat_completions
    base_url: http://127.0.0.1:8000/v1
    requires_api_key: false
"""
    )
    return APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "production-secrets" / "local-principal.json",
        local_operator_capabilities=frozenset({OperatorCapability.READ}),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'production-actions.db'}",
        tools_config_path=tools_path,
        models_config_path=models_path,
        model_secrets_path=tmp_path / "production-secrets" / "models.json",
        workspace_root=tmp_path / "production-workspaces",
        runner_state_path=tmp_path / "production-runner",
        web_dist_path=tmp_path / "missing-production-web-dist",
        admin_token=LOCAL_TOKEN,
        cors_origins=(),
    )


@asynccontextmanager
async def _action_api(
    tmp_path: Path,
    *,
    capabilities: frozenset[OperatorCapability] = frozenset({OperatorCapability.READ}),
    name: str = "actions-api",
) -> AsyncIterator[tuple[_APIHarness, httpx.AsyncClient]]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
    await database.create_schema()
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / name / "local-principal.json",
        local_operator_capabilities=capabilities,
        database_url=database.url,
        web_dist_path=tmp_path / name / "missing-web-dist",
        admin_token=LOCAL_TOKEN,
        cors_origins=(),
    )
    security = settings.create_local_operator_security()
    action_service = ActionApplicationService(
        SQLAlchemyActionReadRepository(database.session_factory),
        authorizer=LocalObjectAuthorizer(security),
    )
    control_plane = SimpleNamespace(settings=settings, action_service=action_service)
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]
    headers = {"Authorization": f"Bearer {LOCAL_TOKEN}"}
    harness = _APIHarness(
        app=app,
        database=database,
        headers=headers,
        principal_id=security.principal.id,
    )
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=headers,
            ) as client:
                yield harness, client
    finally:
        await database.dispose()


async def _seed_foundation(database: Database, *run_ids: str) -> None:
    async with database.session_factory() as session, session.begin():
        session.add(
            EngagementRecord(
                id="engagement-action-api",
                name="Action API",
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
                engagement_id="engagement-action-api",
                node_id=f"node-{run_id}",
                objective="Action API contract",
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


def _intent(
    run_id: str,
    action_id: str,
    *,
    created_at: datetime = NOW,
    status: str = "proposed",
    claim: tuple[str, str] | None = None,
    arguments: dict[str, object] | None = None,
    command_preview: str = "",
    execution_spec: dict[str, object] | None = None,
) -> ToolCallIntentRecord:
    return ToolCallIntentRecord(
        id=action_id,
        run_id=run_id,
        session_id=f"session-{run_id}",
        cycle_id=f"cycle-{run_id}",
        step_id=f"step-{run_id}",
        tool_id="python",
        skill_id=None,
        arguments_json=arguments or {"target": "example.test"},
        command_preview=command_preview,
        reason="Inspect the authorized target",
        target_summary="example.test",
        approval_level="sensitive",
        status=status,
        claimed_execution_key=claim[0] if claim is not None else None,
        claimed_attempt_group=claim[1] if claim is not None else None,
        engine_call_id=f"engine-{action_id}",
        execution_spec_json=execution_spec,
        created_at=created_at,
        updated_at=created_at,
    )


async def _insert(database: Database, records: Iterable[object]) -> None:
    async with database.session_factory() as session, session.begin():
        for record in records:
            session.add(record)
            await session.flush()


async def _count_selects(
    database: Database,
    operation: Awaitable[httpx.Response],
) -> tuple[httpx.Response, list[str]]:
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
        response = await operation
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    return response, statements


def _invalid_cursor_body() -> dict[str, object]:
    return {
        "error": {
            "code": "invalid_action_cursor",
            "message": "The Action cursor is invalid",
            "details": {},
        }
    }


def _rich_action_records(
    run_id: str,
    *,
    principal_id: str,
) -> tuple[tuple[object, ...], dict[str, str]]:
    action_id = "action-rich"
    execution_id = "execution-action-rich"
    execution_key = "execution-key-action-rich"
    approval_id = "approval-action-rich"
    artifact_id = "artifact-action-rich"
    finding_id = "finding-action-rich"
    canaries = {
        name: f"RIFTX_TEST_SECRET_DO_NOT_LEAK_ACTION_API_{name.upper()}"
        for name in ("intent", "approval", "execution", "artifact", "finding", "event")
    }
    created_at = NOW + timedelta(minutes=1)
    public_decided_at = created_at + timedelta(seconds=1)
    runtime_decided_at = created_at + timedelta(seconds=2)
    return (
        (
            _intent(
                run_id,
                action_id,
                created_at=created_at,
                status="executing",
                claim=(execution_key, "initial"),
                arguments={
                    "target": "example.test",
                    "api_key": canaries["intent"],
                },
                command_preview=canaries["intent"],
                execution_spec={"command_text": canaries["intent"]},
            ),
            ToolCallRecord(
                id="tool-call-action-rich",
                sdk_call_id="sdk-call-action-rich",
                run_id=run_id,
                agent_step_id=f"step-{run_id}",
                tool_id="python",
                skill_id=None,
                arguments_json={"opaque": canaries["approval"]},
                approval_status="approved",
                execution_id=execution_id,
                created_at=created_at,
            ),
            RuntimeApprovalRequestRecord(
                id=approval_id,
                run_id=run_id,
                session_id=f"session-{run_id}",
                cycle_id=f"cycle-{run_id}",
                tool_call_intent_id=action_id,
                context_compilation_id=None,
                working_memory_version=None,
                provider_state_id=None,
                status="approved",
                decision="approve_once",
                feedback=canaries["approval"],
                decided_by=principal_id,
                created_at=created_at,
                decided_at=runtime_decided_at,
            ),
            ApprovalRecord(
                id=approval_id,
                run_id=run_id,
                tool_call_id="tool-call-action-rich",
                status="approved",
                tool_name="python",
                command_json=[canaries["approval"]],
                cwd=f"/sensitive/{canaries['approval']}",
                target_summary="example.test",
                env_diff_json={"SECRET": canaries["approval"]},
                reason=canaries["approval"],
                decision="approve_once",
                decision_feedback=canaries["approval"],
                decided_by=principal_id,
                created_at=created_at,
                decided_at=public_decided_at,
            ),
            ExecutionRecord(
                id=execution_id,
                execution_key=execution_key,
                launch_fingerprint="launch:v1:action-api",
                run_id=run_id,
                session_id=f"session-{run_id}",
                tool_call_id=action_id,
                attempt_group="initial",
                node_id=f"node-{run_id}",
                owner_runner_instance_id=None,
                owner_runner_epoch=None,
                executor_type="process",
                argv_json=[canaries["execution"]],
                command_text=canaries["execution"],
                tool_id="python",
                tool_version=canaries["execution"],
                executable_path=f"/sensitive/{canaries['execution']}",
                cwd=f"/sensitive/{canaries['execution']}",
                env_diff_json={"SECRET": canaries["execution"]},
                platform_system="test",
                platform_release="test",
                platform_architecture="test",
                status="running",
                pid=None,
                process_group_id=None,
                containment_id=None,
                exit_code=None,
                stdout_path=f"/sensitive/{canaries['execution']}.stdout",
                stderr_path=f"/sensitive/{canaries['execution']}.stderr",
                created_at=created_at,
                process_created_at=None,
                started_at=created_at,
                finished_at=None,
                physical_stop_confirmed_at=None,
                updated_at=created_at,
            ),
            ArtifactRecord(
                id=artifact_id,
                run_id=run_id,
                execution_id=execution_id,
                name=canaries["artifact"],
                path=f"/sensitive/{canaries['artifact']}",
                mime_type="text/plain",
                sha256="a" * 64,
                size=987654321,
                description=canaries["artifact"],
                created_at=created_at + timedelta(seconds=3),
            ),
            FindingRecord(
                id=finding_id,
                run_id=run_id,
                title=canaries["finding"],
                severity="high",
                status="draft",
                affected_assets_json=[canaries["finding"]],
                description=canaries["finding"],
                evidence_json=[
                    {
                        "execution_id": execution_id,
                        "artifact_id": artifact_id,
                        "description": canaries["finding"],
                        "location": f"/sensitive/{canaries['finding']}",
                    }
                ],
                reproduction_steps_json=[canaries["finding"]],
                impact=canaries["finding"],
                recommendation=canaries["finding"],
                created_at=created_at + timedelta(seconds=4),
                updated_at=created_at + timedelta(seconds=4),
            ),
            RunEventRecord(
                id="event-action-rich",
                run_id=run_id,
                sequence=1,
                event_type="action.test",
                payload_json={
                    "action_id": action_id,
                    "execution_id": execution_id,
                    "opaque": canaries["event"],
                    "environment": {"SECRET": canaries["event"]},
                },
                created_at=created_at + timedelta(seconds=5),
            ),
        ),
        canaries,
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {nested for item in value.values() for nested in _all_keys(item)}
    if isinstance(value, list):
        return {nested for item in value for nested in _all_keys(item)}
    return set()


@pytest.mark.asyncio
async def test_action_routes_require_read_auth_and_publish_fail_closed_openapi(
    tmp_path: Path,
) -> None:
    async with _action_api(tmp_path) as (harness, client):
        unauthorized = await client.get(
            "/api/v1/runs/run-1/actions",
            headers={"Authorization": ""},
        )
        assert unauthorized.status_code == 401

        inventory = {record.name: record for record in harness.app.state.route_policy_inventory}
        assert inventory["list_run_actions"].policy.authorization.value == "local_operator"
        assert inventory["list_run_actions"].policy.effect.value == "read_only"
        assert inventory["get_run_action"].policy.authorization.value == "local_operator"
        assert inventory["get_run_action"].policy.effect.value == "read_only"

        openapi = harness.app.openapi()
        list_operation = openapi["paths"]["/api/v1/runs/{run_id}/actions"]["get"]
        detail_operation = openapi["paths"]["/api/v1/runs/{run_id}/actions/{action_id}"]["get"]
        for operation in (list_operation, detail_operation):
            assert operation["x-riftx-authorization"] == "local_operator"
            assert operation["x-riftx-effect"] == "read_only"
        assert list_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/RunActionListView"
        }
        assert detail_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/RunActionView"
        }
        for operation in (list_operation, detail_operation):
            for status_code in ("401", "403", "404", "422"):
                assert operation["responses"][status_code]["content"]["application/json"][
                    "schema"
                ] == {"$ref": "#/components/schemas/ErrorResponse"}
        parameters = {item["name"]: item for item in list_operation["parameters"]}
        limit_schema = parameters["limit"]["schema"]
        assert limit_schema["type"] == "integer"
        assert limit_schema["maximum"] == 100
        assert limit_schema["minimum"] == 1
        assert limit_schema["default"] == 50
        assert parameters["cursor"]["required"] is False
        assert parameters["sort"]["schema"]["default"] == "created_at_desc"
        assert parameters["sort"]["schema"]["enum"] == ["created_at_desc"]

    async with _action_api(
        tmp_path,
        capabilities=frozenset({OperatorCapability.WRITE}),
        name="actions-api-no-read",
    ) as (_harness, client):
        forbidden = await client.get("/api/v1/runs/run-1/actions")
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "local_operator_capability_denied"


@pytest.mark.asyncio
async def test_production_control_plane_wires_action_repository_through_http(
    tmp_path: Path,
) -> None:
    runtime = await build_control_plane(_production_settings(tmp_path))
    try:
        await _seed_foundation(runtime.database, "run-production")
        await _insert(
            runtime.database,
            [_intent("run-production", "action-production")],
        )
        app = create_app(control_plane=runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
            ) as client:
                listed = await client.get("/api/v1/runs/run-production/actions")
                detailed = await client.get("/api/v1/runs/run-production/actions/action-production")

        assert listed.status_code == detailed.status_code == 200
        assert [item["action_id"] for item in listed.json()["items"]] == ["action-production"]
        assert detailed.json()["action_id"] == "action-production"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_action_detail_idor_is_opaque_and_stops_after_owner_resolution(
    tmp_path: Path,
) -> None:
    async with _action_api(tmp_path) as (harness, client):
        await _seed_foundation(harness.database, "run-a", "run-b")
        await _insert(harness.database, [_intent("run-b", "action-b")])

        owned = await client.get("/api/v1/runs/run-b/actions/action-b")
        assert owned.status_code == 200
        assert owned.json()["action_id"] == "action-b"

        missing, missing_statements = await _count_selects(
            harness.database,
            client.get("/api/v1/runs/run-a/actions/action-missing"),
        )
        foreign, foreign_statements = await _count_selects(
            harness.database,
            client.get("/api/v1/runs/run-a/actions/action-b"),
        )
        missing_parent, missing_parent_statements = await _count_selects(
            harness.database,
            client.get("/api/v1/runs/run-missing/actions/action-missing"),
        )
        missing_list, missing_list_statements = await _count_selects(
            harness.database,
            client.get("/api/v1/runs/run-missing/actions"),
        )

        assert (
            missing.status_code
            == foreign.status_code
            == missing_parent.status_code
            == missing_list.status_code
            == 404
        )
        assert missing.json() == foreign.json() == missing_parent.json() == missing_list.json()
        assert missing.json()["error"]["code"] == "resource_not_accessible"
        assert len(missing_statements) == 1
        assert len(foreign_statements) == 1
        assert len(missing_parent_statements) == 1
        assert len(missing_list_statements) == 1


@pytest.mark.asyncio
async def test_action_cursor_errors_are_generic_bound_and_never_echo_input(
    tmp_path: Path,
) -> None:
    async with _action_api(tmp_path) as (harness, client):
        await _seed_foundation(harness.database, "run-a", "run-b")
        await _insert(
            harness.database,
            [
                _intent("run-a", "action-a", created_at=NOW),
                _intent("run-a", "action-b", created_at=NOW),
            ],
        )
        first = await client.get("/api/v1/runs/run-a/actions", params={"limit": 1})
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        assert isinstance(cursor, str) and cursor
        tampered = f"{cursor[:-4]}AAAA"

        cases = (
            ("run-a", {"limit": 1, "cursor": "not-a-cursor"}, "not-a-cursor"),
            ("run-a", {"limit": 1, "cursor": tampered}, tampered),
            ("run-b", {"limit": 1, "cursor": cursor}, cursor),
            ("run-a", {"limit": 2, "cursor": cursor}, cursor),
            (
                "run-a",
                {"limit": 1, "cursor": cursor, "sort": "created_at_asc"},
                cursor,
            ),
            ("run-a", {"sort": "created_at_asc"}, "created_at_asc"),
        )
        for run_id, params, forbidden_echo in cases:
            response = await client.get(
                f"/api/v1/runs/{run_id}/actions",
                params=params,
            )
            assert response.status_code == 422, response.text
            assert response.json() == _invalid_cursor_body()
            assert forbidden_echo not in response.text


@pytest.mark.asyncio
async def test_action_api_redacts_raw_siblings_and_keeps_fixed_select_budgets(
    tmp_path: Path,
) -> None:
    async with _action_api(tmp_path) as (harness, client):
        await _seed_foundation(harness.database, "run-rich", "run-empty")
        records, canaries = _rich_action_records(
            "run-rich",
            principal_id=harness.principal_id,
        )
        await _insert(harness.database, records)

        listed, list_statements = await _count_selects(
            harness.database,
            client.get("/api/v1/runs/run-rich/actions"),
        )
        detailed, detail_statements = await _count_selects(
            harness.database,
            client.get("/api/v1/runs/run-rich/actions/action-rich"),
        )
        empty, empty_statements = await _count_selects(
            harness.database,
            client.get("/api/v1/runs/run-empty/actions"),
        )

        assert listed.status_code == detailed.status_code == empty.status_code == 200
        assert len(list_statements) == 7, "\n\n".join(list_statements)
        assert len(detail_statements) == 7, "\n\n".join(detail_statements)
        assert len(empty_statements) == 2, "\n\n".join(empty_statements)

        list_payload = listed.json()
        detail_payload = detailed.json()
        assert len(list_payload["items"]) == 1
        assert empty.json()["items"] == []
        for canary in canaries.values():
            assert canary not in listed.text
            assert canary not in detailed.text

        list_item = list_payload["items"][0]
        assert list_item["attempts"] == [
            {
                "execution_id": "execution-action-rich",
                "attempt_group": "initial",
                "node_id": "node-run-rich",
                "status": "running",
                "created_at": "2026-08-02T09:01:00Z",
                "started_at": "2026-08-02T09:01:00Z",
                "finished_at": None,
                "exit_code": None,
                "correlation_quality": "exact",
                "physical_stop_confirmed_at": None,
                "stop_confirmation": "not_applicable",
            }
        ]
        assert not {
            "arguments_summary",
            "approval",
            "executions",
            "result",
            "evidence",
        } & set(list_item)
        assert not {
            "command_preview",
            "execution_spec_json",
            "command_text",
            "argv_json",
            "cwd",
            "env_diff_json",
            "stdout_path",
            "stderr_path",
            "payload_json",
            "evidence_json",
            "path",
            "size",
            "description",
            "location",
        } & _all_keys(list_payload)

        assert detail_payload["arguments_summary"]["api_key"] == "[REDACTED]"
        assert detail_payload["approval"]["feedback_summary"] == "[REDACTED]"
        assert detail_payload["current_execution_id"] == "execution-action-rich"
        assert detail_payload["result"]["artifact_ids"] == ["artifact-action-rich"]
        assert detail_payload["result"]["output_available"] is False
        assert detail_payload["result"]["output_size"] == 0
        assert detail_payload["evidence"]["finding_ids"] == ["finding-action-rich"]
        assert [item["event_id"] for item in detail_payload["evidence"]["events"]] == [
            "event-action-rich"
        ]
        assert not {
            "command_preview",
            "execution_spec_json",
            "command_text",
            "argv_json",
            "cwd",
            "env_diff_json",
            "stdout_path",
            "stderr_path",
            "payload_json",
            "evidence_json",
            "path",
            "size",
            "description",
            "location",
        } & _all_keys(detail_payload)


@pytest.mark.asyncio
async def test_action_artifacts_exclude_target_http_marker_and_association(
    tmp_path: Path,
) -> None:
    marker_canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_ACTION_MARKER_ARTIFACT_ID"
    associated_canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_ACTION_ASSOCIATED_ARTIFACT_ID"
    async with _action_api(tmp_path, name="actions-artifact-visibility") as (harness, client):
        await _seed_foundation(harness.database, "run-artifact-visibility")
        records, _ = _rich_action_records(
            "run-artifact-visibility",
            principal_id=harness.principal_id,
        )
        execution_id = "execution-action-rich"
        await _insert(
            harness.database,
            (
                *records,
                ArtifactRecord(
                    id=marker_canary,
                    run_id="run-artifact-visibility",
                    execution_id=execution_id,
                    name="target-http-orphan-request.json",
                    path="/restricted/marker.json",
                    mime_type="application/json",
                    sha256="b" * 64,
                    size=9,
                    description="Immutable Target HTTP request",
                    created_at=NOW + timedelta(minutes=2),
                ),
                ArtifactRecord(
                    id=associated_canary,
                    run_id="run-artifact-visibility",
                    execution_id=execution_id,
                    name="legacy-arbitrary.bin",
                    path="/restricted/associated.bin",
                    mime_type="application/octet-stream",
                    sha256="c" * 64,
                    size=10,
                    description="Legacy arbitrary name",
                    created_at=NOW + timedelta(minutes=3),
                ),
                TargetHttpRequestRecord(
                    id="exchange-action-associated",
                    execution_key=f"execution:v1:{'d' * 64}",
                    run_id="run-artifact-visibility",
                    session_id="session-run-artifact-visibility",
                    tool_call_id="action-rich",
                    node_id="node-run-artifact-visibility",
                    method="GET",
                    url="https://target.example/",
                    request_json={},
                    result_json={},
                    request_artifact_id=associated_canary,
                    response_artifact_id=None,
                    created_at=NOW + timedelta(minutes=4),
                ),
            ),
        )

        listed = await client.get("/api/v1/runs/run-artifact-visibility/actions")
        detailed = await client.get("/api/v1/runs/run-artifact-visibility/actions/action-rich")

        assert listed.status_code == detailed.status_code == 200
        assert marker_canary not in listed.text
        assert marker_canary not in detailed.text
        assert associated_canary not in listed.text
        assert associated_canary not in detailed.text
        assert detailed.json()["result"]["artifact_ids"] == ["artifact-action-rich"]


@pytest.mark.asyncio
async def test_action_api_pagination_is_stable_with_ties_snapshot_and_sentinel_budget(
    tmp_path: Path,
) -> None:
    async with _action_api(tmp_path) as (harness, client):
        await _seed_foundation(harness.database, "run-pages")
        tied_at = NOW + timedelta(hours=1)
        await _insert(
            harness.database,
            [
                _intent("run-pages", action_id, created_at=tied_at)
                for action_id in ("action-a", "action-b", "action-c")
            ],
        )

        first, first_statements = await _count_selects(
            harness.database,
            client.get("/api/v1/runs/run-pages/actions", params={"limit": 1}),
        )
        assert first.status_code == 200
        assert len(first_statements) == 7, "\n\n".join(first_statements)
        first_payload = first.json()
        assert [item["action_id"] for item in first_payload["items"]] == ["action-c"]
        assert first_payload["has_more"] is True
        assert isinstance(first_payload["next_cursor"], str)

        await _insert(
            harness.database,
            [
                _intent(
                    "run-pages",
                    "action-newer",
                    created_at=tied_at + timedelta(minutes=1),
                )
            ],
        )
        second, second_statements = await _count_selects(
            harness.database,
            client.get(
                "/api/v1/runs/run-pages/actions",
                params={"limit": 1, "cursor": first_payload["next_cursor"]},
            ),
        )
        assert second.status_code == 200
        assert len(second_statements) == 7, "\n\n".join(second_statements)
        second_payload = second.json()
        assert [item["action_id"] for item in second_payload["items"]] == ["action-b"]
        assert second_payload["has_more"] is True

        third, third_statements = await _count_selects(
            harness.database,
            client.get(
                "/api/v1/runs/run-pages/actions",
                params={"limit": 1, "cursor": second_payload["next_cursor"]},
            ),
        )
        assert third.status_code == 200
        assert len(third_statements) == 7, "\n\n".join(third_statements)
        third_payload = third.json()
        assert [item["action_id"] for item in third_payload["items"]] == ["action-a"]
        assert third_payload["has_more"] is False
        assert third_payload["next_cursor"] is None
        assert all(
            item["action_id"] != "action-newer"
            for payload in (first_payload, second_payload, third_payload)
            for item in payload["items"]
        )
