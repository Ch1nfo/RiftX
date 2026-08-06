from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from riftx.api import APISettings, create_app
from riftx.api.dependencies import authorize_run_read, get_observer_projector
from riftx.api.policy import RouteAuthorization, RouteEffect
from riftx.application.graphs import (
    GraphScope,
    GraphSnapshot,
    GraphViewKind,
    GraphViewPage,
)
from riftx.application.services.reports import ReportSource, StructuredReport
from riftx.domain import (
    Engagement,
    LocalPrincipal,
    Objective,
    OperatorCapability,
    Run,
    RunKind,
    TrustProfile,
)
from riftx.observer import (
    ObserverProjection,
    ProjectedGraph,
    ProjectionCoverage,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyGraphReadRepository,
    SQLAlchemyRunRepository,
)

LOCAL_TOKEN = "test-only-observer-projection-token-0001"


def graph_page(view: GraphViewKind) -> GraphViewPage:
    signature = "a" * 64
    return GraphViewPage(
        scope=GraphScope(run_id="run-1", engagement_id="engagement-1"),
        view=view,
        snapshot=GraphSnapshot(id=signature, topology_signature=signature),
        snapshot_id=signature,
        projection_sources=(f"{view.value}_source",),
        nodes=(),
        edges=(),
        type_metadata=(),
        partial_reasons=(),
        truncated=False,
        has_more=False,
        next_cursor=None,
    )


class Projector:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def project(self, run_id: str, **kwargs: object) -> ObserverProjection:
        self.calls.append({"run_id": run_id, **kwargs})
        report_source = ReportSource(
            run_id=run_id,
            objective="Authorized projection",
            scope={},
            run_status="running",
            run_summary="Projection in progress",
        )
        return ObserverProjection(
            run_id=run_id,
            task_graph=graph_page(GraphViewKind.TASK),
            reasoning_graph=ProjectedGraph(kind="reasoning"),
            evidence_graph=graph_page(GraphViewKind.EVIDENCE),
            attack_graph=ProjectedGraph(
                kind="attack",
                partial=True,
                partial_reasons=("attack_graph_authoritative_source_unavailable",),
            ),
            code_graph=ProjectedGraph(
                kind="code",
                partial=True,
                partial_reasons=("code_graph_authoritative_source_unavailable",),
            ),
            operation_graph=graph_page(GraphViewKind.OPERATION),
            coverage=ProjectionCoverage(
                graph_page_limit=int(kwargs["graph_limit"]),
                timeline_limit=int(kwargs["timeline_limit"]),
                partial=True,
                partial_reasons=("code_graph_authoritative_source_unavailable",),
            ),
            report_draft=StructuredReport(
                title="RiftX projection",
                executive_summary="Projection in progress",
                source=report_source,
            ),
            partial_reasons=("code_graph_authoritative_source_unavailable",),
        )


class ReportDrafts:
    async def build_source(self, run: str) -> ReportSource:
        return ReportSource(
            run_id=run,
            objective="Authorized projection",
            scope={},
            run_status="created",
            run_summary="Projection is available",
        )


async def allow_general_run_read() -> object:
    return SimpleNamespace(kind=RunKind.GENERAL)


@asynccontextmanager
async def projection_api(
    tmp_path: Path,
    projector: Projector,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "local-principal.json",
        local_operator_capabilities=frozenset({OperatorCapability.READ}),
        web_dist_path=tmp_path / "missing-web-dist",
        admin_token=LOCAL_TOKEN,
        cors_origins=(),
    )
    app = create_app(control_plane=SimpleNamespace(settings=settings))  # type: ignore[arg-type]
    app.dependency_overrides[get_observer_projector] = lambda: projector
    app.dependency_overrides[authorize_run_read] = allow_general_run_read
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
        ) as client:
            yield app, client


async def test_projection_endpoint_forwards_only_bounded_read_parameters(
    tmp_path: Path,
) -> None:
    projector = Projector()
    async with projection_api(tmp_path, projector) as (app, client):
        response = await client.get(
            "/api/v1/runs/run-1/projection",
            params={"graph_limit": 25, "timeline_limit": 40},
        )

        assert response.status_code == 200, response.text
        call = projector.calls[0]
        assert call["run_id"] == "run-1"
        assert isinstance(call["principal"], LocalPrincipal)
        assert call["graph_limit"] == 25
        assert call["timeline_limit"] == 40
        assert response.json()["code_graph"]["partial_reasons"] == [
            "code_graph_authoritative_source_unavailable"
        ]
        operation = app.openapi()["paths"]["/api/v1/runs/{run_id}/projection"]["get"]
        assert {
            item["name"] for item in operation["parameters"] if item["in"] == "query"
        } == {"graph_limit", "timeline_limit"}


async def test_projection_route_is_fail_closed_read_only(tmp_path: Path) -> None:
    projector = Projector()
    async with projection_api(tmp_path, projector) as (app, _client):
        records = [
            item
            for item in app.state.route_policy_inventory
            if item.name == "get_observer_projection"
        ]

        assert len(records) == 1
        assert records[0].policy.authorization is RouteAuthorization.LOCAL_OPERATOR
        assert records[0].policy.effect is RouteEffect.READ_ONLY


async def test_real_projection_wiring_reuses_graph_and_report_services(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'projection.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Projection wiring")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind=RunKind.GENERAL,
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Project existing state"),
            workspace_path=str(tmp_path),
        )
    )
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "real-local-principal.json",
        local_operator_capabilities=frozenset({OperatorCapability.READ}),
        web_dist_path=tmp_path / "missing-real-web-dist",
        admin_token=LOCAL_TOKEN,
        cors_origins=(),
    )
    graph_repository = SQLAlchemyGraphReadRepository(database.session_factory)
    app = create_app(
        control_plane=SimpleNamespace(
            settings=settings,
            database=database,
            graph_repository=graph_repository,
            report_service=ReportDrafts(),
        )  # type: ignore[arg-type]
    )
    app.dependency_overrides[authorize_run_read] = allow_general_run_read
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
            ) as client:
                response = await client.get("/api/v1/runs/run-1/projection")

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["run_id"] == "run-1"
        assert payload["task_graph"]["view"] == "task"
        assert payload["evidence_graph"]["view"] == "evidence"
        assert payload["operation_graph"]["view"] == "operation"
        assert app.state.observer_projector._graph_views is app.state.graph_service
        assert app.state.graph_service._repository is graph_repository
    finally:
        await database.dispose()
