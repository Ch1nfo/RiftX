"""HTTP contract tests for the authenticated, read-only Run Graph endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from riftx.api import APISettings, create_app
from riftx.api.dependencies import get_graph_service
from riftx.api.policy import RouteAuthorization, RouteEffect
from riftx.application.errors import ResourceNotAccessibleError
from riftx.application.graphs import (
    GraphScope,
    GraphSnapshot,
    GraphViewKind,
    GraphViewPage,
    InvalidGraphCursorError,
    StaleGraphCursorError,
)
from riftx.domain import LocalPrincipal, OperatorCapability, TrustProfile
from riftx.persistence import Database, GraphReadLimits, SQLAlchemyGraphReadRepository
from riftx.persistence.orm import (
    ArtifactRecord,
    EngagementRecord,
    RunRecord,
    TargetHttpRequestRecord,
    WorkingMemoryRecord,
)

LOCAL_TOKEN = "test-only-graph-api-local-operator-token-0001"
NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


@dataclass(slots=True)
class _RecordingGraphService:
    error: Exception | None = None
    calls: list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        self.calls = []

    async def get_view(
        self,
        run_id: str,
        *,
        principal: LocalPrincipal,
        view: GraphViewKind,
        limit: int,
        cursor: str | None,
        node_type: str | None,
        edge_type: str | None,
        focus: str | None,
        search: str | None,
    ) -> GraphViewPage:
        assert self.calls is not None
        self.calls.append(
            {
                "run_id": run_id,
                "principal": principal,
                "view": view,
                "limit": limit,
                "cursor": cursor,
                "node_type": node_type,
                "edge_type": edge_type,
                "focus": focus,
                "search": search,
            }
        )
        if self.error is not None:
            raise self.error
        snapshot_id = "a" * 64
        return GraphViewPage(
            scope=GraphScope(run_id=run_id, engagement_id="engagement-graph"),
            view=view,
            snapshot=GraphSnapshot(id=snapshot_id, topology_signature=snapshot_id),
            snapshot_id=snapshot_id,
            projection_sources=("tool_call_intents",),
            nodes=(),
            edges=(),
            type_metadata=(),
            partial_reasons=(),
            truncated=False,
            has_more=False,
            next_cursor=None,
        )


def _settings(
    tmp_path: Path,
    *,
    name: str,
    capabilities: frozenset[OperatorCapability] = frozenset({OperatorCapability.READ}),
) -> APISettings:
    return APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / name / "local-principal.json",
        local_operator_capabilities=capabilities,
        web_dist_path=tmp_path / name / "missing-web-dist",
        admin_token=LOCAL_TOKEN,
        cors_origins=(),
    )


async def _seed_truncated_task_graph(database: Database) -> None:
    async with database.session_factory() as session, session.begin():
        session.add(
            EngagementRecord(
                id="engagement-graph",
                name="Graph API integration",
                description="",
                authorization_reference=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.flush()
        session.add(
            RunRecord(
                kind="general",
                id="run-graph",
                engagement_id="engagement-graph",
                node_id="node-graph",
                objective="Graph API integration",
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
                version=1,
                state_json={
                    "run_plan": {
                        "items": [
                            {
                                "id": f"plan-item-{index}",
                                "task": "must not be projected",
                                "status": "running",
                                "sequence": index,
                                "completion_summary": None,
                            }
                            for index in range(1, 4)
                        ]
                    }
                },
                created_at=NOW,
                updated_at=NOW,
            )
        )


@asynccontextmanager
async def _graph_api(
    tmp_path: Path,
    service: _RecordingGraphService,
    *,
    name: str,
    capabilities: frozenset[OperatorCapability] = frozenset({OperatorCapability.READ}),
    authenticated: bool = True,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    settings = _settings(tmp_path, name=name, capabilities=capabilities)
    control_plane = SimpleNamespace(settings=settings)
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]
    app.dependency_overrides[get_graph_service] = lambda: service
    headers = {"Authorization": f"Bearer {LOCAL_TOKEN}"} if authenticated else {}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client:
            yield app, client


async def test_graph_query_schema_forwards_only_allowlisted_server_authenticated_fields(
    tmp_path: Path,
) -> None:
    service = _RecordingGraphService()
    async with _graph_api(tmp_path, service, name="graph-query") as (app, client):
        response = await client.get(
            "/api/v1/runs/run-graph/graph",
            params={
                "view": "task",
                "node_type": "action",
                "edge_type": "unassigned",
                "focus": "action:run-graph:action-001",
                "search": "safe-tool",
                "limit": "17",
                "cursor": "opaque-cursor",
            },
        )

        assert response.status_code == 200
        assert service.calls is not None and len(service.calls) == 1
        call = service.calls[0]
        assert call | {"principal": None} == {
            "run_id": "run-graph",
            "principal": None,
            "view": GraphViewKind.TASK,
            "limit": 17,
            "cursor": "opaque-cursor",
            "node_type": "action",
            "edge_type": "unassigned",
            "focus": "action:run-graph:action-001",
            "search": "safe-tool",
        }
        assert isinstance(call["principal"], LocalPrincipal)
        assert call["principal"].id == app.state.local_operator_security.principal.id

        operation = app.openapi()["paths"]["/api/v1/runs/{run_id}/graph"]["get"]
        assert {
            parameter["name"] for parameter in operation["parameters"] if parameter["in"] == "query"
        } == {"view", "node_type", "edge_type", "focus", "search", "limit", "cursor"}

        untrusted = await client.get(
            "/api/v1/runs/run-graph/graph",
            params={"view": "task", "actor": "attacker", "role": "admin"},
        )
        assert untrusted.status_code == 422
        assert untrusted.json()["error"]["code"] == "validation_error"
        assert len(service.calls) == 1


async def test_graph_query_schema_rejects_invalid_filters_without_echoing_values(
    tmp_path: Path,
) -> None:
    service = _RecordingGraphService()
    cases = (
        ("node_type", "Action-graph-query-canary"),
        ("edge_type", "supports/graph-query-canary"),
        ("focus", "action:run-graph/graph-query-canary"),
        ("search", "graph-query-canary\u200bsecret"),
        ("search", ""),
    )
    async with _graph_api(tmp_path, service, name="graph-invalid-query") as (
        _app,
        client,
    ):
        for field, value in cases:
            response = await client.get(
                "/api/v1/runs/run-graph/graph",
                params={"view": "task", field: value},
            )

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "validation_error"
            assert "graph-query-canary" not in response.text

    assert service.calls == []


async def test_graph_route_is_in_fail_closed_read_only_policy_inventory(tmp_path: Path) -> None:
    service = _RecordingGraphService()
    async with _graph_api(tmp_path, service, name="graph-policy") as (app, _client):
        records = [
            record for record in app.state.route_policy_inventory if record.name == "get_run_graph"
        ]

        assert len(records) == 1
        assert records[0].path == "/api/v1/runs/{run_id}/graph"
        assert records[0].methods == ("GET",)
        assert records[0].policy.authorization is RouteAuthorization.LOCAL_OPERATOR
        assert records[0].policy.effect is RouteEffect.READ_ONLY


async def test_graph_route_requires_authentication_and_read_capability(tmp_path: Path) -> None:
    unauthenticated_service = _RecordingGraphService()
    async with _graph_api(
        tmp_path,
        unauthenticated_service,
        name="graph-401",
        authenticated=False,
    ) as (_app, client):
        response = await client.get("/api/v1/runs/run-graph/graph", params={"view": "task"})
        assert response.status_code == 401
        assert unauthenticated_service.calls == []

    unauthorized_service = _RecordingGraphService()
    async with _graph_api(
        tmp_path,
        unauthorized_service,
        name="graph-403",
        capabilities=frozenset(),
    ) as (_app, client):
        response = await client.get("/api/v1/runs/run-graph/graph", params={"view": "task"})
        assert response.status_code == 403
        assert unauthorized_service.calls == []


async def test_unknown_and_foreign_graph_parents_are_indistinguishable(tmp_path: Path) -> None:
    responses: list[dict[str, object]] = []
    for name, run_id in (
        ("graph-missing", "run-missing"),
        ("graph-foreign", "run-foreign"),
    ):
        service = _RecordingGraphService(
            error=ResourceNotAccessibleError(
                "resource_not_accessible",
                "The requested resource was not found",
            )
        )
        async with _graph_api(tmp_path, service, name=name) as (_app, client):
            response = await client.get(
                f"/api/v1/runs/{run_id}/graph",
                params={"view": "task"},
            )
            assert response.status_code == 404
            responses.append(response.json())

    assert (
        responses[0]
        == responses[1]
        == {
            "error": {
                "code": "resource_not_accessible",
                "message": "The requested resource was not found",
                "details": {},
            }
        }
    )


async def test_graph_cursor_errors_are_generic_and_stale_is_a_conflict(tmp_path: Path) -> None:
    cases = (
        (InvalidGraphCursorError(), 422, "invalid_graph_cursor"),
        (StaleGraphCursorError(), 409, "stale_graph_cursor"),
    )
    for index, (error, status, code) in enumerate(cases):
        service = _RecordingGraphService(error=error)
        async with _graph_api(
            tmp_path,
            service,
            name=f"graph-cursor-{index}",
        ) as (_app, client):
            response = await client.get(
                "/api/v1/runs/run-graph/graph",
                params={"view": "task", "cursor": "client-secret-cursor"},
            )

        assert response.status_code == status
        payload = response.json()
        assert payload["error"]["code"] == code
        assert "client-secret-cursor" not in response.text
        assert payload["error"]["details"] == {}


async def test_real_graph_service_wiring_authenticates_authorizes_and_reports_source_limits(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'graph-real-http.db'}")
    await database.create_schema()
    try:
        await _seed_truncated_task_graph(database)
        repository = SQLAlchemyGraphReadRepository(
            database.session_factory,
            limits=GraphReadLimits(plan_items=2),
        )
        settings = _settings(tmp_path, name="graph-real-http")
        control_plane = SimpleNamespace(
            settings=settings,
            graph_repository=repository,
        )
        app = create_app(control_plane=control_plane)  # type: ignore[arg-type]

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                unauthenticated = await client.get(
                    "/api/v1/runs/run-graph/graph",
                    params={"view": "task"},
                )
                response = await client.get(
                    "/api/v1/runs/run-graph/graph",
                    params={"view": "task"},
                    headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
                )

        assert unauthenticated.status_code == 401
        assert response.status_code == 200
        payload = response.json()
        assert payload["truncated"] is True
        assert payload["partial_reasons"] == ["plan_items_source_limit"]
        assert [node["domain_id"] for node in payload["nodes"]] == [
            "plan-item-1",
            "plan-item-2",
        ]
        assert app.dependency_overrides == {}
        assert app.state.graph_service._repository is repository
        assert app.state.graph_service._authorizer is app.state.graph_object_authorizer
        assert app.state.graph_object_authorizer.delegate is app.state.local_object_authorizer
    finally:
        await database.dispose()


async def test_evidence_graph_excludes_target_http_artifacts_without_hiding_ordinary_artifacts(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'graph-artifact-visibility.db'}")
    await database.create_schema()
    marker_canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_GRAPH_MARKER_ARTIFACT_ID"
    associated_canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_GRAPH_ASSOCIATED_ARTIFACT_ID"
    try:
        await _seed_truncated_task_graph(database)
        async with database.session_factory() as session, session.begin():
            session.add_all(
                [
                    ArtifactRecord(
                        id="artifact-graph-ordinary",
                        run_id="run-graph",
                        execution_id=None,
                        name="ordinary.txt",
                        path="/evidence/ordinary.txt",
                        mime_type="text/plain",
                        sha256="a" * 64,
                        size=8,
                        description="Ordinary evidence",
                        created_at=NOW,
                    ),
                    ArtifactRecord(
                        id=marker_canary,
                        run_id="run-graph",
                        execution_id=None,
                        name="target-http-orphan-response.bin",
                        path="/restricted/marker.bin",
                        mime_type="application/octet-stream",
                        sha256="b" * 64,
                        size=9,
                        description="Immutable Target HTTP response body",
                        created_at=NOW,
                    ),
                    ArtifactRecord(
                        id=associated_canary,
                        run_id="run-graph",
                        execution_id=None,
                        name="legacy-arbitrary.bin",
                        path="/restricted/associated.bin",
                        mime_type="application/octet-stream",
                        sha256="c" * 64,
                        size=10,
                        description="Legacy arbitrary name",
                        created_at=NOW,
                    ),
                ]
            )
            session.add(
                TargetHttpRequestRecord(
                    id="exchange-graph-associated",
                    execution_key=f"execution:v1:{'a' * 64}",
                    run_id="run-graph",
                    session_id="session-graph-associated",
                    tool_call_id="intent-graph-associated",
                    node_id="node-graph",
                    method="GET",
                    url="https://target.example/",
                    request_json={},
                    result_json={},
                    request_artifact_id=associated_canary,
                    response_artifact_id=None,
                    created_at=NOW,
                )
            )

        repository = SQLAlchemyGraphReadRepository(database.session_factory)
        settings = _settings(tmp_path, name="graph-artifact-visibility")
        app = create_app(
            control_plane=SimpleNamespace(settings=settings, graph_repository=repository)  # type: ignore[arg-type]
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
            ) as client:
                response = await client.get(
                    "/api/v1/runs/run-graph/graph",
                    params={"view": "evidence"},
                )

        assert response.status_code == 200, response.text
        assert marker_canary not in response.text
        assert associated_canary not in response.text
        artifact_ids = {
            node["domain_id"] for node in response.json()["nodes"] if node["type"] == "artifact"
        }
        assert artifact_ids == {"artifact-graph-ordinary"}
    finally:
        await database.dispose()
