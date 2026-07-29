"""FastAPI application factory for the RiftX control plane."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .errors import install_error_handlers
from .routes import events_router, findings_router, runs_router, tools_router
from .runtime import APISettings, ControlPlane, build_control_plane


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

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
