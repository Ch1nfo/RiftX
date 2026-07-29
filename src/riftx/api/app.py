"""FastAPI application factory for the RiftX control plane."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from .errors import APIError, install_error_handlers
from .routes import (
    approvals_router,
    events_router,
    findings_router,
    runs_router,
    terminals_router,
    tools_router,
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
    configured_settings = settings or (
        control_plane.settings if control_plane is not None else APISettings.from_environment()
    )

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
    install_error_handlers(app)
    if configured_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(configured_settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(tools_router, prefix="/api/v1")
    app.include_router(events_router, prefix="/api/v1")
    app.include_router(findings_router, prefix="/api/v1")
    app.include_router(approvals_router, prefix="/api/v1")
    app.include_router(terminals_router, prefix="/api/v1")

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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

    web_dist = configured_settings.web_dist_path
    if (web_dist / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=web_dist, html=True), name="web")

    return app


app = create_app()
