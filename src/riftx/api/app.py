"""FastAPI application factory for the RiftX control plane."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from riftx.security import DeploymentProfileError, LocalObjectAuthorizer

from .errors import APIError, install_error_handlers
from .policy import apply_route_policy_inventory, install_local_operator_dependencies
from .routes import (
    actions_router,
    approvals_router,
    artifacts_router,
    audit_preflight_router,
    audit_preflight_runner_router,
    audits_router,
    browser_router,
    connectors_router,
    context_router,
    events_router,
    executions_router,
    findings_router,
    graphs_router,
    memories_router,
    models_router,
    nodes_router,
    observability_router,
    observer_router,
    pentests_router,
    reports_router,
    runner_control_router,
    runs_router,
    security_router,
    system_router,
    terminals_router,
    tools_router,
    traffic_router,
)
from .runtime import APISettings, ControlPlane, build_control_plane


class SPAStaticFiles(StaticFiles):
    """Serve Vite assets and fall back to index.html for client routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response


def create_app(
    *,
    control_plane: ControlPlane | None = None,
    settings: APISettings | None = None,
) -> FastAPI:
    if control_plane is not None and settings is not None:
        raise DeploymentProfileError(
            "control_plane_settings_ambiguous",
            "Pass either a ControlPlane or APISettings to create_app, not both",
            "create_app 只能传入 ControlPlane 或 APISettings，不能同时传入",
        )
    configured_settings = settings or (
        control_plane.settings if control_plane is not None else APISettings.from_environment()
    )
    configured_settings.validate_api_security_boundary()
    local_operator_security = configured_settings.create_local_operator_security()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = control_plane is None
        runtime = control_plane or await build_control_plane(configured_settings)
        app.state.control_plane = runtime
        try:
            yield
        finally:
            if owned:
                await runtime.close()

    app = FastAPI(
        title="RiftX Control Plane",
        version="2.0.0a0",
        lifespan=lifespan,
    )
    app.state.local_operator_security = local_operator_security
    app.state.local_object_authorizer = LocalObjectAuthorizer(local_operator_security)
    app.state.graph_cursor_signing_key = secrets.token_bytes(32)
    app.state.traffic_cursor_signing_key = secrets.token_bytes(32)
    install_error_handlers(app)
    if configured_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(configured_settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(pentests_router, prefix="/api/v1")
    app.include_router(audit_preflight_router, prefix="/api/v1")
    app.include_router(audits_router, prefix="/api/v1")
    app.include_router(actions_router, prefix="/api/v1")
    app.include_router(nodes_router, prefix="/api/v1")
    app.include_router(observability_router, prefix="/api/v1")
    app.include_router(observer_router, prefix="/api/v1")
    app.include_router(runner_control_router, prefix="/api/v1")
    app.include_router(audit_preflight_runner_router, prefix="/api/v1")
    app.include_router(tools_router, prefix="/api/v1")
    app.include_router(events_router, prefix="/api/v1")
    app.include_router(executions_router, prefix="/api/v1")
    app.include_router(findings_router, prefix="/api/v1")
    app.include_router(graphs_router, prefix="/api/v1")
    app.include_router(traffic_router, prefix="/api/v1")
    app.include_router(memories_router, prefix="/api/v1")
    app.include_router(models_router, prefix="/api/v1")
    app.include_router(reports_router, prefix="/api/v1")
    app.include_router(approvals_router, prefix="/api/v1")
    app.include_router(artifacts_router, prefix="/api/v1")
    app.include_router(context_router, prefix="/api/v1")
    app.include_router(terminals_router, prefix="/api/v1")
    app.include_router(browser_router, prefix="/api/v1")
    if configured_settings.connectors_enabled:
        app.include_router(connectors_router, prefix="/api/v1")
    app.include_router(security_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "trust_profile": local_operator_security.principal.profile.value,
        }

    @app.api_route(
        "/api/{unmatched_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def api_not_found(unmatched_path: str) -> None:
        raise APIError(
            404,
            "route_not_found",
            f"API route '/api/{unmatched_path}' was not found",
        )

    install_local_operator_dependencies(app)
    apply_route_policy_inventory(
        app,
        disabled_route_names=(
            ()
            if configured_settings.connectors_enabled
            else tuple(
                route.name
                for route in connectors_router.routes
                if isinstance(route.name, str)
            )
        ),
    )

    web_dist = configured_settings.web_dist_path
    if (web_dist / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=web_dist, html=True), name="web")

    return app
