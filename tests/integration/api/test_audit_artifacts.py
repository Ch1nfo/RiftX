"""AUD-105 HTTP contract for partitioned, descriptor-safe Audit Artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from tests.integration.api.test_audits import (
    REQUEST_TWO,
    _audit_api,
    _request_payload,
)
from tests.integration.api.test_audits import (
    _service as audit_service,
)

from riftx.api.dependencies import (
    get_artifact_service,
    get_audit_object_authorizer,
    get_audit_service,
    get_run_service,
)
from riftx.application.errors import EntityNotFoundError, ResourceNotAccessibleError
from riftx.application.ports import AuditAuthorizationBinding
from riftx.application.services.artifacts import ArtifactApplicationService
from riftx.domain import (
    Artifact,
    ArtifactAccessClass,
    ArtifactContentTrust,
    ArtifactIngestMethod,
    ArtifactIngestProvenance,
    LocalPrincipal,
    Objective,
    OperatorCapability,
    Run,
    RunKind,
)
from riftx.persistence import (
    Database,
    SQLAlchemyArtifactRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.mappers import artifact_to_record
from riftx.persistence.orm import ArtifactRecord
from riftx.runner import LocalArtifactContentStore, OpenedArtifactContent, RunnerPaths

_MAX_ARTIFACT_BYTES = 1024 * 1024
_PATH_CANARY = "RIFTX_AUDIT_ARTIFACT_PATH_MUST_NOT_LEAK"
_PROVENANCE_CANARY = "RIFTX_AUDIT_ARTIFACT_PROVENANCE_MUST_NOT_LEAK"
_DENIAL_CANARY = "RIFTX_AUDIT_ARTIFACT_DENIAL_MUST_NOT_LEAK"
_CORRUPT_ROW_CANARY = "RIFTX_ARTIFACT_CORRUPT_ROW_MUST_NOT_LEAK"
_CORRUPT_ROW_PATH = f"/private/sensitive/{_CORRUPT_ROW_CANARY}/source.json"


class _RunReads:
    def __init__(self, repository: SQLAlchemyRunRepository) -> None:
        self._repository = repository

    async def resolve_kind(self, run_id: str) -> RunKind:
        kind = await self._repository.get_kind(run_id)
        if kind is None:
            raise EntityNotFoundError("Run", run_id)
        return kind

    async def get_run(self, run_id: str) -> object:
        run = await self._repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run


class _CountingStore(LocalArtifactContentStore):
    def __init__(self, paths: RunnerPaths) -> None:
        super().__init__(paths, max_artifact_bytes=_MAX_ARTIFACT_BYTES)
        self.open_calls = 0

    def open_verified(
        self,
        *,
        storage_key: str,
        expected_sha256: str,
        expected_size: int,
    ) -> OpenedArtifactContent:
        self.open_calls += 1
        return super().open_verified(
            storage_key=storage_key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )


@dataclass(frozen=True, slots=True)
class _ArtifactHarness:
    database: Database
    repository: SQLAlchemyArtifactRepository
    runs: SQLAlchemyRunRepository
    paths: RunnerPaths
    store: _CountingStore


def _install_artifact_runtime(
    database: Database,
    app: FastAPI,
    root: Path,
) -> _ArtifactHarness:
    paths = RunnerPaths(root)
    store = _CountingStore(paths)
    artifacts = SQLAlchemyArtifactRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    service = ArtifactApplicationService(
        run_repository=runs,
        execution_repository=SQLAlchemyExecutionRepository(database.session_factory),
        artifact_repository=artifacts,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=paths,
        max_artifact_bytes=_MAX_ARTIFACT_BYTES,
        content_store=store,
    )
    app.dependency_overrides[get_artifact_service] = lambda: service
    app.dependency_overrides[get_run_service] = lambda: _RunReads(runs)
    return _ArtifactHarness(
        database=database,
        repository=artifacts,
        runs=runs,
        paths=paths,
        store=store,
    )


async def _create_audit(
    client: httpx.AsyncClient,
    *,
    request_id: str | None = None,
    repository_seed: str = "artifact-owner",
) -> tuple[str, str]:
    payload = (
        _request_payload(repository_seed=repository_seed)
        if request_id is None
        else _request_payload(request_id, repository_seed=repository_seed)
    )
    response = await client.post("/api/v1/audits", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["audit"]["id"], response.json()["audit"]["run_id"]


async def _seed_artifact(
    harness: _ArtifactHarness,
    *,
    artifact_id: str,
    audit_id: str,
    run_id: str,
    access_class: ArtifactAccessClass,
    content_trust: ArtifactContentTrust,
    content: bytes,
    mime_type: str = "text/plain",
    bypass_owner_validation: bool = False,
) -> Artifact:
    name = f"{artifact_id}.txt"
    destination = harness.paths.artifact(run_id, artifact_id, name)
    stored = harness.store.snapshot_bytes(content, storage_key=destination.storage_key)
    artifact = Artifact(
        id=artifact_id,
        run_id=run_id,
        audit_id=audit_id,
        access_class=access_class,
        content_trust=content_trust,
        name=name,
        path=f"/legacy/{_PATH_CANARY}/{artifact_id}",
        storage_key=destination.storage_key,
        ingest_provenance=ArtifactIngestProvenance(
            method=ArtifactIngestMethod.AUTHENTICATED_CHUNK_STREAM,
            producer_node_id=_PROVENANCE_CANARY,
        ),
        mime_type=mime_type,
        sha256=stored.sha256,
        size=stored.size,
        description=f"safe {access_class.value} fixture",
    )
    if not bypass_owner_validation:
        return await harness.repository.create(artifact)
    async with harness.database.session_factory() as session, session.begin():
        session.add(artifact_to_record(artifact))
    return artifact


async def _corrupt_artifact_row(
    database: Database,
    artifact_id: str,
    corruption: str,
) -> str:
    values: dict[str, object]
    if corruption == "provenance":
        values = {
            "ingest_provenance_json": {
                "schema_version": "riftx.artifact-ingest-provenance/v1",
                "method": f"invalid-{_CORRUPT_ROW_CANARY}",
                "producer_node_id": _CORRUPT_ROW_PATH,
            }
        }
    elif corruption == "storage_key":
        values = {"storage_key": _CORRUPT_ROW_PATH}
    elif corruption == "mime_type":
        values = {
            "mime_type": (
                f"application/octet-stream\r\nX-{_CORRUPT_ROW_CANARY}: {_CORRUPT_ROW_PATH}"
            )
        }
    else:
        raise AssertionError(f"unsupported corruption fixture {corruption!r}")

    async with database.engine.begin() as connection:
        assert connection.dialect.name == "sqlite"
        await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        try:
            result = await connection.execute(
                update(ArtifactRecord).where(ArtifactRecord.id == artifact_id).values(**values)
            )
            assert result.rowcount == 1
        finally:
            await connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    return json.dumps(values, sort_keys=True)


def _assert_safe_metadata(
    response: httpx.Response,
    *,
    expected: Artifact,
) -> None:
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "id",
        "run_id",
        "audit_id",
        "execution_id",
        "name",
        "mime_type",
        "sha256",
        "size",
        "description",
        "access_class",
        "content_trust",
        "created_at",
        "content_url",
    }
    assert body["id"] == expected.id
    assert body["run_id"] == expected.run_id
    assert body["audit_id"] == expected.audit_id
    assert body["access_class"] == expected.access_class.value
    assert body["content_trust"] == expected.content_trust.value
    assert body["content_url"] == (
        f"/api/v1/audits/{expected.audit_id}/artifacts/{expected.id}/content"
    )
    assert _PATH_CANARY not in response.text
    assert _PROVENANCE_CANARY not in response.text


@pytest.mark.parametrize(
    ("access_class", "content_trust"),
    (
        (ArtifactAccessClass.PUBLIC_EXPORT, ArtifactContentTrust.GENERATED),
        (ArtifactAccessClass.AUDIT_INTERNAL, ArtifactContentTrust.UNTRUSTED_SOURCE),
        (
            ArtifactAccessClass.RESTRICTED_SENSITIVE,
            ArtifactContentTrust.UNTRUSTED_TOOL_OUTPUT,
        ),
    ),
)
async def test_explicit_audit_routes_read_each_access_class_with_safe_projection(
    tmp_path: Path,
    access_class: ArtifactAccessClass,
    content_trust: ArtifactContentTrust,
) -> None:
    async with _audit_api(
        tmp_path,
        name=f"audit-artifact-explicit-{access_class.value}",
    ) as (_settings, database, _audits, raw_app, client):
        assert isinstance(raw_app, FastAPI)
        harness = _install_artifact_runtime(
            database,
            raw_app,
            tmp_path / f"store-{access_class.value}",
        )
        audit_id, run_id = await _create_audit(client)
        content = f"content:{access_class.value}".encode()
        artifact = await _seed_artifact(
            harness,
            artifact_id=f"artifact-{access_class.value}",
            audit_id=audit_id,
            run_id=run_id,
            access_class=access_class,
            content_trust=content_trust,
            content=content,
        )

        listed = await client.get(f"/api/v1/audits/{audit_id}/artifacts")
        detail = await client.get(f"/api/v1/audits/{audit_id}/artifacts/{artifact.id}")
        downloaded = await client.get(f"/api/v1/audits/{audit_id}/artifacts/{artifact.id}/content")

        assert listed.status_code == 200, listed.text
        assert listed.json()["limit"] == 100
        assert listed.json()["offset"] == 0
        assert len(listed.json()["items"]) == 1
        _assert_safe_metadata(
            httpx.Response(200, json=listed.json()["items"][0]),
            expected=artifact,
        )
        _assert_safe_metadata(detail, expected=artifact)
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == content
        assert downloaded.headers["content-length"] == str(len(content))
        assert downloaded.headers["x-artifact-sha256"] == artifact.sha256
        assert downloaded.headers["etag"] == f'"sha256:{artifact.sha256}"'
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert downloaded.headers["cache-control"] == "no-store"
        assert harness.store.open_calls == 1


async def test_generic_routes_filter_before_pagination_and_hide_non_public_artifacts(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-artifact-generic") as (
        _settings,
        database,
        _audits,
        raw_app,
        client,
    ):
        assert isinstance(raw_app, FastAPI)
        harness = _install_artifact_runtime(database, raw_app, tmp_path / "generic-store")
        audit_id, run_id = await _create_audit(client)
        fixtures = (
            await _seed_artifact(
                harness,
                artifact_id="artifact-00-internal",
                audit_id=audit_id,
                run_id=run_id,
                access_class=ArtifactAccessClass.AUDIT_INTERNAL,
                content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
                content=b"internal",
            ),
            await _seed_artifact(
                harness,
                artifact_id="artifact-10-public",
                audit_id=audit_id,
                run_id=run_id,
                access_class=ArtifactAccessClass.PUBLIC_EXPORT,
                content_trust=ArtifactContentTrust.GENERATED,
                content=b"public",
            ),
            await _seed_artifact(
                harness,
                artifact_id="artifact-20-restricted",
                audit_id=audit_id,
                run_id=run_id,
                access_class=ArtifactAccessClass.RESTRICTED_SENSITIVE,
                content_trust=ArtifactContentTrust.UNTRUSTED_TOOL_OUTPUT,
                content=b"restricted",
            ),
        )
        by_class = {artifact.access_class: artifact for artifact in fixtures}

        audit_listed = await client.get(f"/api/v1/audits/{audit_id}/artifacts")
        assert audit_listed.status_code == 200, audit_listed.text
        assert {item["id"] for item in audit_listed.json()["items"]} == {
            artifact.id for artifact in fixtures
        }
        for item in audit_listed.json()["items"]:
            expected = next(artifact for artifact in fixtures if artifact.id == item["id"])
            _assert_safe_metadata(httpx.Response(200, json=item), expected=expected)

        listed = await client.get(
            f"/api/v1/runs/{run_id}/artifacts",
            params={"limit": 1, "offset": 0},
        )
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == [
            by_class[ArtifactAccessClass.PUBLIC_EXPORT].id
        ]
        _assert_safe_metadata(
            httpx.Response(200, json=listed.json()["items"][0]),
            expected=by_class[ArtifactAccessClass.PUBLIC_EXPORT],
        )

        for artifact in fixtures:
            detail = await client.get(f"/api/v1/artifacts/{artifact.id}")
            content = await client.get(f"/api/v1/artifacts/{artifact.id}/content")
            if artifact.access_class is ArtifactAccessClass.PUBLIC_EXPORT:
                _assert_safe_metadata(detail, expected=artifact)
                assert content.status_code == 200
                assert content.content == b"public"
            else:
                assert detail.status_code == content.status_code == 404
                assert detail.content == content.content
                assert detail.json()["error"] == {
                    "code": "resource_not_accessible",
                    "message": "The requested resource was not found",
                    "details": {},
                }
                assert artifact.id not in detail.text
                assert artifact.id not in content.text

        assert harness.store.open_calls == 1


async def test_generic_artifact_openapi_declares_persistence_503_and_binary_content(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-artifact-openapi") as (
        _settings,
        database,
        _audits,
        raw_app,
        client,
    ):
        assert isinstance(raw_app, FastAPI)
        harness = _install_artifact_runtime(database, raw_app, tmp_path / "openapi-store")
        audit_id, run_id = await _create_audit(client)
        artifact = await _seed_artifact(
            harness,
            artifact_id="artifact-openapi-binary",
            audit_id=audit_id,
            run_id=run_id,
            access_class=ArtifactAccessClass.PUBLIC_EXPORT,
            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
            content=b"\x00\xffbinary",
            mime_type="application/octet-stream",
        )

        downloaded = await client.get(f"/api/v1/artifacts/{artifact.id}/content")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == b"\x00\xffbinary"
        assert downloaded.headers["content-type"] == "application/octet-stream"

        schema = raw_app.openapi()
        detail_responses = schema["paths"]["/api/v1/artifacts/{artifact_id}"]["get"]["responses"]
        content_responses = schema["paths"]["/api/v1/artifacts/{artifact_id}/content"]["get"][
            "responses"
        ]

        assert detail_responses["503"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
        assert content_responses["503"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
        assert content_responses["200"]["content"] == {
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
        }


class _CanaryDenyingAuthorizer:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def authorized_engagement_scope(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> object:
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
            "canary_denial_code",
            _DENIAL_CANARY,
            details={"canary": _DENIAL_CANARY},
        )


class _CountingAuthorizer:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.binding_calls = 0

    def authorized_engagement_scope(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> object:
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
        self.binding_calls += 1
        self._delegate.require_audit_binding(  # type: ignore[attr-defined]
            principal,
            binding,
            capability=capability,
        )


@pytest.mark.parametrize("corruption", ("provenance", "storage_key", "mime_type"))
async def test_authorized_corrupt_artifact_rows_return_one_sanitized_503_without_open(
    tmp_path: Path,
    corruption: str,
) -> None:
    async with _audit_api(
        tmp_path,
        name=f"audit-artifact-corrupt-{corruption}",
    ) as (_settings, database, _audits, raw_app, client):
        assert isinstance(raw_app, FastAPI)
        harness = _install_artifact_runtime(
            database,
            raw_app,
            tmp_path / f"corrupt-{corruption}",
        )
        audit_id, run_id = await _create_audit(client)
        artifact = await _seed_artifact(
            harness,
            artifact_id=f"artifact-corrupt-{corruption}",
            audit_id=audit_id,
            run_id=run_id,
            access_class=ArtifactAccessClass.PUBLIC_EXPORT,
            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
            content=b"must-not-open",
        )
        raw_corruption = await _corrupt_artifact_row(
            database,
            artifact.id,
            corruption,
        )
        counting_authorizer = _CountingAuthorizer(raw_app.state.local_object_authorizer)
        raw_app.dependency_overrides[get_audit_object_authorizer] = lambda: counting_authorizer

        responses = (
            await client.get(f"/api/v1/audits/{audit_id}/artifacts"),
            await client.get(f"/api/v1/audits/{audit_id}/artifacts/{artifact.id}"),
            await client.get(f"/api/v1/audits/{audit_id}/artifacts/{artifact.id}/content"),
            await client.get(f"/api/v1/runs/{run_id}/artifacts"),
            await client.get(f"/api/v1/artifacts/{artifact.id}"),
            await client.get(f"/api/v1/artifacts/{artifact.id}/content"),
        )

        # Explicit Audit routes and generic reads of a Code Audit Run each
        # complete object authorization before the corrupt full row is loaded.
        assert counting_authorizer.binding_calls == 6
        assert all(response.status_code == 503 for response in responses)
        assert len({response.content for response in responses}) == 1
        assert responses[0].json()["error"] == {
            "code": "artifact_persistence_unavailable",
            "message": "Artifact metadata is temporarily unavailable",
            "details": {},
        }
        for response in responses:
            assert artifact.id not in response.text
            assert _CORRUPT_ROW_CANARY not in response.text
            assert _CORRUPT_ROW_PATH not in response.text
            assert _PATH_CANARY not in response.text
            assert _PROVENANCE_CANARY not in response.text
            assert raw_corruption not in response.text
        assert harness.store.open_calls == 0


@pytest.mark.parametrize("endpoint", ("detail", "content"))
async def test_missing_denied_wrong_audit_and_wrong_run_share_one_404_before_open(
    tmp_path: Path,
    endpoint: str,
) -> None:
    async with _audit_api(tmp_path, name=f"audit-artifact-opaque-{endpoint}") as (
        _settings,
        database,
        _audits,
        raw_app,
        client,
    ):
        assert isinstance(raw_app, FastAPI)
        harness = _install_artifact_runtime(database, raw_app, tmp_path / endpoint)
        first_audit_id, first_run_id = await _create_audit(client)
        second_audit_id, second_run_id = await _create_audit(
            client,
            request_id=REQUEST_TWO,
            repository_seed="artifact-second-owner",
        )
        owned = await _seed_artifact(
            harness,
            artifact_id="artifact-owned-first",
            audit_id=first_audit_id,
            run_id=first_run_id,
            access_class=ArtifactAccessClass.RESTRICTED_SENSITIVE,
            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
            content=b"owner-one",
        )
        wrong_run = await _seed_artifact(
            harness,
            artifact_id="artifact-wrong-run",
            audit_id=first_audit_id,
            run_id=second_run_id,
            access_class=ArtifactAccessClass.AUDIT_INTERNAL,
            content_trust=ArtifactContentTrust.UNTRUSTED_TOOL_OUTPUT,
            content=b"wrong-run",
            bypass_owner_validation=True,
        )
        first_run = await harness.runs.get(first_run_id)
        assert first_run is not None
        general_run_id = "general-run-wrong-kind"
        await harness.runs.create(
            Run(
                id=general_run_id,
                engagement_id=first_run.engagement_id,
                node_id=first_run.node_id,
                kind=RunKind.GENERAL,
                objective=Objective(description="Wrong-kind Artifact owner fixture"),
                workspace_path=str(tmp_path / "wrong-kind-workspace"),
            )
        )
        wrong_kind = await _seed_artifact(
            harness,
            artifact_id="artifact-wrong-kind",
            audit_id=first_audit_id,
            run_id=general_run_id,
            access_class=ArtifactAccessClass.RESTRICTED_SENSITIVE,
            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
            content=b"wrong-kind",
            bypass_owner_validation=True,
        )
        suffix = "" if endpoint == "detail" else "/content"

        counting_authorizer = _CountingAuthorizer(raw_app.state.local_object_authorizer)
        raw_app.dependency_overrides[get_audit_object_authorizer] = lambda: counting_authorizer

        missing = await client.get(
            f"/api/v1/audits/{first_audit_id}/artifacts/artifact-missing{suffix}"
        )
        wrong_audit = await client.get(
            f"/api/v1/audits/{second_audit_id}/artifacts/{owned.id}{suffix}"
        )
        wrong_run_response = await client.get(
            f"/api/v1/audits/{first_audit_id}/artifacts/{wrong_run.id}{suffix}"
        )
        wrong_kind_response = await client.get(
            f"/api/v1/audits/{first_audit_id}/artifacts/{wrong_kind.id}{suffix}"
        )
        assert counting_authorizer.binding_calls == 0

        raw_app.dependency_overrides[get_audit_object_authorizer] = lambda: (
            _CanaryDenyingAuthorizer(raw_app.state.local_object_authorizer)
        )
        denied = await client.get(f"/api/v1/audits/{first_audit_id}/artifacts/{owned.id}{suffix}")

        responses = (missing, denied, wrong_audit, wrong_run_response, wrong_kind_response)
        assert all(response.status_code == 404 for response in responses)
        assert len({response.content for response in responses}) == 1
        assert responses[0].json()["error"] == {
            "code": "resource_not_accessible",
            "message": "The requested resource was not found",
            "details": {},
        }
        assert all(_DENIAL_CANARY not in response.text for response in responses)
        assert harness.store.open_calls == 0


async def test_audit_artifact_list_missing_and_denied_are_byte_identical(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-artifact-list-opaque") as (
        _settings,
        database,
        _audits,
        raw_app,
        client,
    ):
        assert isinstance(raw_app, FastAPI)
        harness = _install_artifact_runtime(database, raw_app, tmp_path / "list-opaque")
        audit_id, _run_id = await _create_audit(client)
        missing = await client.get("/api/v1/audits/audit-missing/artifacts")

        raw_app.dependency_overrides[get_audit_object_authorizer] = lambda: (
            _CanaryDenyingAuthorizer(raw_app.state.local_object_authorizer)
        )
        denied = await client.get(f"/api/v1/audits/{audit_id}/artifacts")

        assert missing.status_code == denied.status_code == 404
        assert missing.content == denied.content
        assert missing.json()["error"] == {
            "code": "resource_not_accessible",
            "message": "The requested resource was not found",
            "details": {},
        }
        assert _DENIAL_CANARY not in denied.text
        assert harness.store.open_calls == 0


async def test_feature_flag_off_keeps_historical_restricted_artifact_reads_available(
    tmp_path: Path,
) -> None:
    async with _audit_api(tmp_path, name="audit-artifact-flag") as (
        settings,
        database,
        _enabled_audits,
        raw_app,
        client,
    ):
        assert isinstance(raw_app, FastAPI)
        harness = _install_artifact_runtime(database, raw_app, tmp_path / "flag-store")
        audit_id, run_id = await _create_audit(client)
        artifact = await _seed_artifact(
            harness,
            artifact_id="artifact-flag-restricted",
            audit_id=audit_id,
            run_id=run_id,
            access_class=ArtifactAccessClass.RESTRICTED_SENSITIVE,
            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
            content=b"historical-restricted",
        )
        raw_app.dependency_overrides[get_audit_service] = lambda: audit_service(
            database,
            settings,
            enabled=False,
        )

        listed = await client.get(f"/api/v1/audits/{audit_id}/artifacts")
        detail = await client.get(f"/api/v1/audits/{audit_id}/artifacts/{artifact.id}")
        content = await client.get(f"/api/v1/audits/{audit_id}/artifacts/{artifact.id}/content")

        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == [artifact.id]
        _assert_safe_metadata(detail, expected=artifact)
        assert content.status_code == 200
        assert content.content == b"historical-restricted"


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/audits/audit-auth/artifacts",
        "/api/v1/audits/audit-auth/artifacts/artifact-auth",
        "/api/v1/audits/audit-auth/artifacts/artifact-auth/content",
    ),
)
async def test_audit_artifact_routes_require_authentication_and_read_capability(
    tmp_path: Path,
    path: str,
) -> None:
    async with _audit_api(
        tmp_path,
        name=f"audit-artifact-unauthenticated-{path.count('/')}",
        authenticated=False,
    ) as (_settings, _database, _audits, _app, client):
        unauthenticated = await client.get(path)
    assert unauthenticated.status_code == 401

    async with _audit_api(
        tmp_path,
        name=f"audit-artifact-forbidden-{path.count('/')}",
        capabilities=frozenset(),
    ) as (_settings, _database, _audits, _app, client):
        forbidden = await client.get(path)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "local_operator_capability_denied"
