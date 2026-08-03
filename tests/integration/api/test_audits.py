"""HTTP and real-persistence contract for the AUD-104 Code Audit skeleton."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
from sqlalchemy import text
from tests.unit.domain.test_audit_domain import _contract as domain_contract

from riftx.api import APISettings, create_app
from riftx.api.dependencies import (
    get_audit_object_authorizer,
    get_audit_service,
    get_run_service,
)
from riftx.application.errors import EntityNotFoundError, ResourceNotAccessibleError
from riftx.application.ports import (
    AuditAuthorizationBinding,
    AuditEngagementScope,
)
from riftx.application.services import AuditApplicationService
from riftx.config import AuditConfig
from riftx.domain import LocalPrincipal, OperatorCapability, TrustProfile
from riftx.persistence import (
    Database,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyAuditCreationUnitOfWork,
    SQLAlchemyRunRepository,
)

LOCAL_TOKEN = "test-only-audit-api-local-operator-token-0001"
NOW = datetime(2026, 8, 3, 8, tzinfo=UTC)
REQUEST_ONE = "11111111-1111-4111-8111-111111111111"
REQUEST_TWO = "22222222-2222-4222-8222-222222222222"
SOURCE_CANARY = "/sensitive/RIFTX_AUDIT_SOURCE_PATH_MUST_NOT_LEAK"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _request_payload(
    client_request_id: str = REQUEST_ONE,
    *,
    repository_seed: str = "repository-one",
    engagement_id: str | None = None,
    source_path: str = SOURCE_CANARY,
) -> dict[str, object]:
    contract = domain_contract().model_dump(
        mode="json",
        exclude={"audit_id", "project_id"},
    )
    contract["source_target"]["repository_path"] = source_path
    return {
        "client_request_id": client_request_id,
        "project_name": "RiftX",
        "repository_identity_digest": _digest(repository_seed),
        "engagement_id": engagement_id,
        "default_branch": "main",
        "contract": contract,
    }


def _settings(
    tmp_path: Path,
    *,
    name: str,
    enabled: bool,
    capabilities: frozenset[OperatorCapability] = frozenset(OperatorCapability),
) -> APISettings:
    root = tmp_path / name
    return APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=root / "secrets" / "local-principal.json",
        local_operator_capabilities=capabilities,
        admin_token=LOCAL_TOKEN,
        cors_origins=(),
        web_dist_path=root / "missing-web-dist",
        audit=AuditConfig(
            enabled=enabled,
            snapshot_root=root / "audit" / "snapshots",
            temp_root=root / "audit" / "tmp",
            fix_root=root / "audit" / "fixes",
        ),
    )


def _service(
    database: Database,
    settings: APISettings,
    *,
    enabled: bool,
) -> AuditApplicationService:
    return AuditApplicationService(
        creation_uow=SQLAlchemyAuditCreationUnitOfWork(database.session_factory),
        aggregate_repository=SQLAlchemyAuditAggregateReadRepository(database.session_factory),
        feature_enabled=enabled,
        workspace_root=settings.audit.temp_root,
        clock=lambda: NOW,
    )


@asynccontextmanager
async def _audit_api(
    tmp_path: Path,
    *,
    name: str,
    enabled: bool = True,
    authenticated: bool = True,
    capabilities: frozenset[OperatorCapability] = frozenset(OperatorCapability),
) -> AsyncIterator[
    tuple[APISettings, Database, AuditApplicationService, object, httpx.AsyncClient]
]:
    settings = _settings(
        tmp_path,
        name=name,
        enabled=enabled,
        capabilities=capabilities,
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
    await database.create_schema()
    service = _service(database, settings, enabled=enabled)
    app = create_app(control_plane=SimpleNamespace(settings=settings))  # type: ignore[arg-type]
    app.dependency_overrides[get_audit_service] = lambda: service
    headers = {"Authorization": f"Bearer {LOCAL_TOKEN}"} if authenticated else {}
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=headers,
            ) as client:
                yield settings, database, service, app, client
    finally:
        await database.dispose()


async def _counts(database: Database) -> dict[str, int]:
    tables = (
        "engagements",
        "audit_projects",
        "runs",
        "run_events",
        "audit_contracts",
        "audit_scans",
        "audit_client_requests",
        "source_snapshots",
        "audit_start_intents",
    )
    async with database.engine.connect() as connection:
        return {
            table: int(await connection.scalar(text(f'SELECT count(*) FROM "{table}"')) or 0)
            for table in tables
        }


async def test_create_replay_list_and_detail_are_draft_only_and_path_free(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-happy") as (
        _settings_value,
        database,
        _service_value,
        _app,
        client,
    ):
        payload = _request_payload()
        created = await client.post("/api/v1/audits", json=payload)
        replayed = await client.post("/api/v1/audits", json=payload)
        audit_id = created.json()["audit"]["id"]
        listed = await client.get("/api/v1/audits")
        detailed = await client.get(f"/api/v1/audits/{audit_id}")

        assert created.status_code == 201, created.text
        assert replayed.status_code == 200, replayed.text
        assert created.json()["created"] is True
        assert created.json()["replayed"] is False
        assert replayed.json()["created"] is False
        assert replayed.json()["replayed"] is True
        assert replayed.json()["audit"] == created.json()["audit"]
        assert listed.status_code == detailed.status_code == 200
        assert listed.json()["items"] == [detailed.json()]
        for response in (created, replayed, listed, detailed):
            assert SOURCE_CANARY not in response.text
            assert "authorization_reference" not in response.text
            assert "canonical_contract" not in response.text
            assert "temporal_workflow" not in response.text
            assert "workspace_path" not in response.text

        assert await _counts(database) == {
            "engagements": 1,
            "audit_projects": 1,
            "runs": 1,
            "run_events": 2,
            "audit_contracts": 1,
            "audit_scans": 1,
            "audit_client_requests": 1,
            "source_snapshots": 0,
            "audit_start_intents": 0,
        }


async def test_feature_flag_blocks_first_create_and_replay_but_not_existing_reads(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-flag") as (
        settings,
        database,
        _enabled_service,
        app,
        client,
    ):
        payload = _request_payload()
        created = await client.post("/api/v1/audits", json=payload)
        assert created.status_code == 201
        audit_id = created.json()["audit"]["id"]

        disabled = _service(database, settings, enabled=False)
        app.dependency_overrides[get_audit_service] = lambda: disabled
        replay = await client.post("/api/v1/audits", json=payload)
        first = await client.post(
            "/api/v1/audits",
            json=_request_payload(REQUEST_TWO, repository_seed="repository-two"),
        )
        listed = await client.get("/api/v1/audits")
        detailed = await client.get(f"/api/v1/audits/{audit_id}")

        for response in (replay, first):
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "feature_disabled"
        assert listed.status_code == detailed.status_code == 200
        assert len(listed.json()["items"]) == 1
        assert (await _counts(database))["audit_scans"] == 1


async def test_audit_body_validation_redacts_the_entire_sensitive_input(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-validation") as (
        _settings_value,
        database,
        _service_value,
        _app,
        client,
    ):
        payload = _request_payload(source_path="relative/RIFTX_BODY_CANARY_MUST_NOT_LEAK")
        contract = payload["contract"]
        assert isinstance(contract, dict)
        contract["authorization_reference"] = "RIFTX_AUTH_CANARY_MUST_NOT_LEAK"
        response = await client.post("/api/v1/audits", json=payload)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
        assert "RIFTX_BODY_CANARY_MUST_NOT_LEAK" not in response.text
        assert "RIFTX_AUTH_CANARY_MUST_NOT_LEAK" not in response.text
        assert all(
            item.get("input") == "[redacted]"
            for item in response.json()["error"]["details"]
        )
        assert (await _counts(database))["audit_scans"] == 0


class _DenyingAuthorizer:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def authorized_engagement_scope(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> AuditEngagementScope:
        return self._delegate.authorized_engagement_scope(  # type: ignore[attr-defined,no-any-return]
            principal,
            capability=capability,
        )

    def draft_authorization_reference(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> str:
        return self._delegate.draft_authorization_reference(  # type: ignore[attr-defined,no-any-return]
            principal,
            capability=capability,
        )

    def require_audit_binding(
        self,
        principal: LocalPrincipal,
        binding: AuditAuthorizationBinding,
        *,
        capability: OperatorCapability,
    ) -> None:
        del principal, binding, capability
        raise ResourceNotAccessibleError(
            "resource_not_accessible",
            "The requested resource was not found",
            details={"messages": {"zh-CN": "未找到请求的资源"}},
        )


async def test_missing_and_denied_detail_are_byte_identical_generic_404(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-generic-404") as (
        _settings_value,
        _database,
        _service_value,
        app,
        client,
    ):
        created = await client.post("/api/v1/audits", json=_request_payload())
        assert created.status_code == 201
        audit_id = created.json()["audit"]["id"]
        missing = await client.get("/api/v1/audits/audit-that-does-not-exist")

        app.dependency_overrides[get_audit_object_authorizer] = lambda: _DenyingAuthorizer(
            app.state.local_object_authorizer
        )
        denied = await client.get(f"/api/v1/audits/{audit_id}")

        assert missing.status_code == denied.status_code == 404
        assert missing.content == denied.content
        assert missing.json() == {
            "error": {
                "code": "resource_not_accessible",
                "message": "The requested resource was not found",
                "details": {"messages": {"zh-CN": "未找到请求的资源"}},
            }
        }


async def test_generic_code_audit_run_reads_use_audit_authorization_and_projection(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-generic-run-read") as (
        _settings_value,
        database,
        _service_value,
        app,
        client,
    ):
        created = await client.post("/api/v1/audits", json=_request_payload())
        assert created.status_code == 201
        run_id = created.json()["audit"]["run_id"]
        runs = SQLAlchemyRunRepository(database.session_factory)

        class _RunReads:
            async def resolve_kind(self, requested_run_id: str):
                kind = await runs.get_kind(requested_run_id)
                if kind is None:
                    raise EntityNotFoundError("Run", requested_run_id)
                return kind

            async def get_run(self, requested_run_id: str):
                run = await runs.get(requested_run_id)
                if run is None:
                    raise EntityNotFoundError("Run", requested_run_id)
                return run

        app.dependency_overrides[get_run_service] = lambda: _RunReads()
        detail = await client.get(f"/api/v1/runs/{run_id}")
        listed = await client.get("/api/v1/runs", params={"kind": "code_audit"})

        assert detail.status_code == listed.status_code == 200
        assert listed.json()["items"] == [detail.json()]
        assert detail.json()["kind"] == "code_audit"
        assert "workspace_path" not in detail.json()
        assert "temporal_workflow_id" not in detail.json()

        app.dependency_overrides[get_audit_object_authorizer] = lambda: _DenyingAuthorizer(
            app.state.local_object_authorizer
        )
        denied = await client.get(f"/api/v1/runs/{run_id}")
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "resource_not_accessible"


async def test_missing_engagement_and_cross_owner_repository_share_creation_conflict(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-create-conflict") as (
        _settings_value,
        _database,
        _service_value,
        _app,
        client,
    ):
        created = await client.post("/api/v1/audits", json=_request_payload())
        assert created.status_code == 201
        existing_engagement = created.json()["audit"]["project"]["engagement_id"]

        missing = await client.post(
            "/api/v1/audits",
            json=_request_payload(
                REQUEST_TWO,
                repository_seed="new-repository",
                engagement_id="missing-engagement",
            ),
        )
        cross_owner = await client.post(
            "/api/v1/audits",
            json=_request_payload(
                str(UUID(int=3, version=4)),
                engagement_id="different-engagement",
            ),
        )

        assert existing_engagement not in missing.text
        assert missing.status_code == cross_owner.status_code == 409
        assert missing.content == cross_owner.content
        assert missing.json()["error"] == {
            "code": "audit_creation_conflict",
            "message": "The Code Audit draft conflicts with an existing authorization domain",
            "details": {},
        }


async def test_audit_routes_require_the_declared_read_and_write_capabilities(
    tmp_path: Path,
) -> None:
    async with _audit_api(
        tmp_path,
        name="audit-read-only",
        capabilities=frozenset({OperatorCapability.READ}),
    ) as (_settings_value, database, _service_value, _app, client):
        create = await client.post("/api/v1/audits", json=_request_payload())
        read = await client.get("/api/v1/audits")

        assert create.status_code == 403
        assert read.status_code == 200
        assert (await _counts(database))["audit_scans"] == 0

    async with _audit_api(
        tmp_path,
        name="audit-unauthenticated",
        authenticated=False,
    ) as (_settings_value, database, _service_value, _app, client):
        create = await client.post("/api/v1/audits", json=_request_payload())
        read = await client.get("/api/v1/audits")

        assert create.status_code == read.status_code == 401
        assert (await _counts(database))["audit_scans"] == 0


async def test_openapi_exposes_the_audit_and_read_only_artifact_surfaces_safely(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-openapi") as (
        _settings_value,
        _database,
        _service_value,
        app,
        _client,
    ):
        openapi = app.openapi()
        paths = {
            path: item
            for path, item in openapi["paths"].items()
            if path.startswith("/api/v1/audits")
        }

        assert set(paths) == {
            "/api/v1/audits",
            "/api/v1/audits/{audit_id}",
            "/api/v1/audits/{audit_id}/artifacts",
            "/api/v1/audits/{audit_id}/artifacts/{artifact_id}",
            "/api/v1/audits/{audit_id}/artifacts/{artifact_id}/content",
        }
        assert set(paths["/api/v1/audits"]) == {"get", "post"}
        assert set(paths["/api/v1/audits/{audit_id}"]) == {"get"}
        assert set(paths["/api/v1/audits/{audit_id}/artifacts"]) == {"get"}
        assert set(paths["/api/v1/audits/{audit_id}/artifacts/{artifact_id}"]) == {
            "get"
        }
        assert set(
            paths[
                "/api/v1/audits/{audit_id}/artifacts/{artifact_id}/content"
            ]
        ) == {"get"}
        for artifact_path in (
            "/api/v1/audits/{audit_id}/artifacts",
            "/api/v1/audits/{audit_id}/artifacts/{artifact_id}",
            "/api/v1/audits/{audit_id}/artifacts/{artifact_id}/content",
        ):
            responses = paths[artifact_path]["get"]["responses"]
            assert {"200", "401", "403", "404", "409", "422", "503"} <= set(
                responses
            )
        assert "preflight" not in str(paths)
        assert "/start" not in str(paths)
        serialized = str(paths)
        for forbidden in (
            "authorization_reference",
            "canonical_contract_json",
            "request_digest",
            "workspace_path",
            "temporal_workflow_id",
            "storage_key",
            "ingest_provenance",
        ):
            assert forbidden not in serialized
        artifact_properties = openapi["components"]["schemas"]["ArtifactResponse"][
            "properties"
        ]
        assert {"path", "storage_key", "ingest_provenance"}.isdisjoint(
            artifact_properties
        )
