from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from riftx.api.auth import get_authenticated_local_principal
from riftx.api.dependencies import get_audit_object_authorizer
from riftx.api.errors import install_error_handlers
from riftx.api.routes.audit_preflight import router as audit_preflight_router
from riftx.application.errors import AuthorizationError
from riftx.application.services.audit_preflight import (
    AuditPreflightApplicationService,
)
from riftx.domain import LocalPrincipal, OperatorCapability
from riftx.persistence.audit_preflight import SQLAlchemyAuditPreflightRepository
from riftx.persistence.database import Database

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
IMAGE_DIGEST = hashlib.sha256(b"image").hexdigest()
POLICY_DIGEST = hashlib.sha256(b"policy").hexdigest()

PRINCIPAL = LocalPrincipal(
    id="operator-1",
    capabilities=frozenset(OperatorCapability),
)
OTHER_PRINCIPAL = LocalPrincipal(
    id="operator-2",
    capabilities=frozenset(OperatorCapability),
)


class ProfileAAuthorizer:
    def __init__(self) -> None:
        self.capabilities: list[tuple[str, OperatorCapability]] = []

    def preflight_authorization_scope_digest(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> str:
        if capability not in principal.capabilities:
            raise AuthorizationError(
                "local_operator_capability_denied",
                "The local operator lacks the required server capability",
            )
        self.capabilities.append((principal.id, capability))
        canonical = "\0".join(
            (
                principal.id,
                principal.profile.value,
                principal.namespace_id,
                ",".join(sorted(item.value for item in principal.capabilities)),
            )
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


def _request_payload(
    repository_path: Path,
    *,
    client_request_id: str = "123e4567-e89b-42d3-a456-426614174000",
    include_paths: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "riftx.audit-preflight-request/v1",
        "client_request_id": client_request_id,
        "repository_path": str(repository_path),
        "source_execution_target": {
            "node_id": "local",
            "source_ingest_backend": "linux_container",
        },
        "target": {
            "kind": "working_tree",
            "revision": "HEAD",
            "include_untracked": False,
        },
        "include_paths": include_paths or ["src"],
        "exclude_paths": ["vendor"],
        "security_context": {
            "input_id": None,
            "repository_paths": [],
            "discover_defaults": False,
        },
        "mode": "standard",
    }


def _service(
    database: Database,
    source_root: Path,
    *,
    enabled: bool = True,
) -> AuditPreflightApplicationService:
    identifiers = iter(f"preflight-job-{index}" for index in range(1, 100))
    return AuditPreflightApplicationService(
        repository=SQLAlchemyAuditPreflightRepository(database.session_factory),
        feature_enabled=enabled,
        source_roots=(source_root,),
        backend_id="linux_container",
        image_digest=IMAGE_DIGEST,
        policy_digest=POLICY_DIGEST,
        source_ingest_available=True,
        job_ttl_seconds=900,
        id_factory=lambda: next(identifiers),
        clock=lambda: NOW,
    )


@asynccontextmanager
async def _api(
    tmp_path: Path,
    *,
    enabled: bool = True,
) -> AsyncIterator[
    tuple[
        httpx.AsyncClient,
        Database,
        Path,
        SimpleNamespace,
        dict[str, LocalPrincipal],
        ProfileAAuthorizer,
    ]
]:
    source_root = tmp_path / "source"
    repository_path = source_root / "repository"
    repository_path.mkdir(parents=True)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'preflight-api.db'}")
    await database.create_schema()
    control_plane = SimpleNamespace(
        audit_preflight_service=_service(database, source_root, enabled=enabled)
    )
    principal_state = {"value": PRINCIPAL}
    authorizer = ProfileAAuthorizer()
    app = FastAPI()
    app.state.control_plane = control_plane
    install_error_handlers(app)
    app.include_router(audit_preflight_router, prefix="/api/v1")
    app.dependency_overrides[get_authenticated_local_principal] = lambda: principal_state["value"]
    app.dependency_overrides[get_audit_object_authorizer] = lambda: authorizer
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield (
                client,
                database,
                repository_path,
                control_plane,
                principal_state,
                authorizer,
            )
    finally:
        await database.dispose()


async def _count(database: Database, table: str) -> int:
    async with database.session_factory() as session:
        return int(await session.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0)


@pytest.mark.asyncio
async def test_create_replay_get_and_idempotency_conflict_use_safe_projection(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (
        client,
        database,
        repository_path,
        _control_plane,
        _principal_state,
        authorizer,
    ):
        payload = _request_payload(repository_path)
        created = await client.post("/api/v1/audits/preflight", json=payload)
        repository_path.rmdir()
        replayed = await client.post("/api/v1/audits/preflight", json=payload)
        job_id = created.json()["job_id"]
        detailed = await client.get(f"/api/v1/audits/preflight/{job_id}")
        changed = await client.post(
            "/api/v1/audits/preflight",
            json={**payload, "include_paths": ["different"]},
        )

        assert created.status_code == 202
        assert created.json()["created"] is True
        assert created.json()["replayed"] is False
        assert replayed.status_code == 200
        assert replayed.json()["job_id"] == job_id
        assert replayed.json()["created"] is False
        assert replayed.json()["replayed"] is True
        assert detailed.status_code == 200
        assert detailed.json()["status"] == "pending"
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == ("audit_preflight_idempotency_conflict")
        forbidden = {
            "repository_path",
            "restricted_request_json",
            "operator_principal_id",
            "authorization_scope_digest",
            "effect_owner_digest",
            "lease_id",
            "lease_envelope_digest",
            "capsule_id",
        }
        assert not forbidden.intersection(created.json())
        assert str(repository_path) not in created.text
        assert str(repository_path) not in detailed.text
        assert await _count(database, "audit_preflight_jobs") == 1
        assert await _count(database, "runs") == 0
        assert await _count(database, "audit_scans") == 0
        assert await _count(database, "runner_commands") == 0
        assert [capability for _, capability in authorizer.capabilities] == [
            OperatorCapability.HOST_EXECUTE,
            OperatorCapability.HOST_EXECUTE,
            OperatorCapability.READ,
            OperatorCapability.HOST_EXECUTE,
        ]


@pytest.mark.asyncio
async def test_missing_cross_principal_and_cross_scope_share_one_safe_404(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (
        client,
        _database,
        repository_path,
        _control_plane,
        principal_state,
        _authorizer,
    ):
        created = await client.post(
            "/api/v1/audits/preflight",
            json=_request_payload(repository_path),
        )
        job_id = created.json()["job_id"]
        principal_state["value"] = OTHER_PRINCIPAL

        foreign = await client.get(f"/api/v1/audits/preflight/{job_id}")
        missing = await client.get("/api/v1/audits/preflight/missing-job")
        foreign_cancel = await client.post(f"/api/v1/audits/preflight/{job_id}/cancel")
        missing_cancel = await client.post("/api/v1/audits/preflight/missing-job/cancel")

        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
        assert foreign.json()["error"]["code"] == "resource_not_accessible"
        assert foreign_cancel.status_code == missing_cancel.status_code == 404
        assert foreign_cancel.json() == missing_cancel.json() == foreign.json()


@pytest.mark.asyncio
async def test_existing_get_and_cancel_remain_available_after_feature_disable(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (
        client,
        database,
        repository_path,
        control_plane,
        _principal_state,
        authorizer,
    ):
        created = await client.post(
            "/api/v1/audits/preflight",
            json=_request_payload(repository_path),
        )
        job_id = created.json()["job_id"]
        control_plane.audit_preflight_service = _service(
            database,
            repository_path.parent,
            enabled=False,
        )

        detailed = await client.get(f"/api/v1/audits/preflight/{job_id}")
        cancelled = await client.post(f"/api/v1/audits/preflight/{job_id}/cancel")
        cancel_replay = await client.post(f"/api/v1/audits/preflight/{job_id}/cancel")
        new_create = await client.post(
            "/api/v1/audits/preflight",
            json=_request_payload(
                repository_path,
                client_request_id="223e4567-e89b-42d3-a456-426614174000",
            ),
        )

        assert detailed.status_code == 200
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["state_version"] == 2
        assert cancel_replay.status_code == 200
        assert cancel_replay.json() == cancelled.json()
        assert new_create.status_code == 503
        assert new_create.json()["error"]["code"] == "audit_feature_disabled"
        assert await _count(database, "audit_preflight_jobs") == 1
        assert (PRINCIPAL.id, OperatorCapability.READ) in authorizer.capabilities
        assert (PRINCIPAL.id, OperatorCapability.HOST_CONTROL) in authorizer.capabilities


@pytest.mark.asyncio
async def test_disabled_feature_precedes_sensitive_body_validation(tmp_path: Path) -> None:
    async with _api(tmp_path, enabled=False) as (
        client,
        database,
        repository_path,
        _control_plane,
        _principal_state,
        _authorizer,
    ):
        response = await client.post(
            "/api/v1/audits/preflight",
            json={
                "repository_path": str(repository_path / "CANARY-MUST-NOT-BE-PARSED"),
                "unknown": "CANARY-MUST-NOT-BE-REFLECTED",
            },
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "audit_feature_disabled"
        assert "CANARY" not in response.text
        assert await _count(database, "audit_preflight_jobs") == 0


@pytest.mark.asyncio
async def test_validation_redacts_path_and_unknown_value_canaries(tmp_path: Path) -> None:
    async with _api(tmp_path) as (
        client,
        database,
        repository_path,
        _control_plane,
        _principal_state,
        _authorizer,
    ):
        payload = _request_payload(repository_path)
        payload["repository_path"] = "/sensitive/RIFTX_PREFLIGHT_PATH_CANARY/../repository"
        payload["server_owned"] = "RIFTX_PREFLIGHT_VALUE_CANARY"
        response = await client.post("/api/v1/audits/preflight", json=payload)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
        assert "RIFTX_PREFLIGHT_PATH_CANARY" not in response.text
        assert "RIFTX_PREFLIGHT_VALUE_CANARY" not in response.text
        assert await _count(database, "audit_preflight_jobs") == 0
