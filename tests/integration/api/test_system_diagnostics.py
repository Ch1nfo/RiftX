"""System diagnostic API authorization and serialization tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx

from riftx.api import APISettings, create_app
from riftx.api.dependencies import get_system_diagnostics_service
from riftx.diagnostics import (
    DatabaseMigrationDiagnostics,
    OfficialPackDiagnostics,
    SystemDiagnosticsSnapshot,
)
from riftx.domain import OperatorCapability, TrustProfile

LOCAL_TOKEN = "system-diagnostics-local-token-1234567890"


class _Diagnostics:
    async def snapshot(self) -> SystemDiagnosticsSnapshot:
        return SystemDiagnosticsSnapshot(
            database=DatabaseMigrationDiagnostics(
                status="ready",
                expected_revision="head-1",
                current_revisions=("head-1",),
            ),
            official_packs=OfficialPackDiagnostics(
                status="ready",
                expected_pack_count=22,
                installed_pack_count=22,
                active_lock_count=66,
            ),
        )


@asynccontextmanager
async def _diagnostics_api(tmp_path: Path, *, authenticated: bool = True):
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "local-principal.json",
        local_operator_capabilities=frozenset({OperatorCapability.READ}),
        web_dist_path=tmp_path / "missing-web-dist",
        admin_token=LOCAL_TOKEN,
        cors_origins=(),
    )
    app = create_app(control_plane=SimpleNamespace(settings=settings))  # type: ignore[arg-type]
    app.dependency_overrides[get_system_diagnostics_service] = lambda: _Diagnostics()
    headers = {"Authorization": f"Bearer {LOCAL_TOKEN}"} if authenticated else {}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client:
            yield client


async def test_system_diagnostics_endpoint_is_local_read_only(tmp_path: Path) -> None:
    async with _diagnostics_api(tmp_path) as client:
        response = await client.get("/api/v1/system/diagnostics")

    assert response.status_code == 200
    assert response.json()["database"]["status"] == "ready"
    assert response.json()["official_packs"]["active_lock_count"] == 66


async def test_system_diagnostics_endpoint_requires_local_operator(tmp_path: Path) -> None:
    async with _diagnostics_api(tmp_path, authenticated=False) as client:
        response = await client.get("/api/v1/system/diagnostics")

    assert response.status_code == 401
