"""HTTP contract tests for the metadata-only Target HTTP read surface."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from riftx.api import APISettings, create_app
from riftx.api.dependencies import get_traffic_metadata_service
from riftx.api.policy import RouteAuthorization, RouteEffect
from riftx.application.errors import AuthorizationError, ResourceNotAccessibleError
from riftx.application.traffic import (
    InvalidTrafficCursorError,
    StaleTrafficCursorError,
    TrafficExchangeDetail,
    TrafficExchangePage,
    TrafficStatusClass,
)
from riftx.domain import LocalPrincipal, OperatorCapability, TrustProfile

FIXTURES = Path(__file__).parents[2] / "fixtures"
LIST_FIXTURE = json.loads((FIXTURES / "traffic_metadata_list.json").read_text())
DETAIL_FIXTURE = json.loads((FIXTURES / "traffic_metadata_detail.json").read_text())
LOCAL_TOKEN = "test-only-traffic-api-local-operator-token-0001"


@dataclass(slots=True)
class _RecordingTrafficService:
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def list(
        self,
        run_id: str,
        *,
        principal: LocalPrincipal,
        method: str | None,
        status_class: TrafficStatusClass | None,
        limit: int,
        cursor: str | None,
    ) -> TrafficExchangePage:
        self.calls.append(
            {
                "operation": "list",
                "run_id": run_id,
                "principal": principal,
                "method": method,
                "status_class": status_class,
                "limit": limit,
                "cursor": cursor,
            }
        )
        self._raise_if_needed(principal)
        return TrafficExchangePage.model_validate(LIST_FIXTURE)

    async def get(
        self,
        run_id: str,
        exchange_id: str,
        *,
        principal: LocalPrincipal,
    ) -> TrafficExchangeDetail:
        self.calls.append(
            {
                "operation": "get",
                "run_id": run_id,
                "exchange_id": exchange_id,
                "principal": principal,
            }
        )
        self._raise_if_needed(principal)
        return TrafficExchangeDetail.model_validate(DETAIL_FIXTURE)

    def _raise_if_needed(self, principal: LocalPrincipal) -> None:
        if self.error is not None:
            raise self.error
        if OperatorCapability.READ not in principal.capabilities:
            raise AuthorizationError(
                "traffic_metadata_forbidden",
                "Traffic metadata access is forbidden",
            )


@asynccontextmanager
async def _traffic_api(
    tmp_path: Path,
    service: _RecordingTrafficService,
    *,
    name: str,
    capabilities: frozenset[OperatorCapability] = frozenset({OperatorCapability.READ}),
    authenticated: bool = True,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / name / "local-principal.json",
        local_operator_capabilities=capabilities,
        web_dist_path=tmp_path / name / "missing-web-dist",
        admin_token=LOCAL_TOKEN,
        cors_origins=(),
    )
    app = create_app(control_plane=SimpleNamespace(settings=settings))  # type: ignore[arg-type]
    app.dependency_overrides[get_traffic_metadata_service] = lambda: service
    headers = {"Authorization": f"Bearer {LOCAL_TOKEN}"} if authenticated else {}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client:
            yield app, client


async def test_traffic_routes_serialize_the_frozen_fixtures_and_forward_allowlisted_query(
    tmp_path: Path,
) -> None:
    service = _RecordingTrafficService()
    async with _traffic_api(tmp_path, service, name="traffic-fixtures") as (app, client):
        listed = await client.get(
            "/api/v1/runs/run-traffic/target-http/exchanges",
            params={"method": "GET", "status_class": "success", "limit": 50},
        )
        detailed = await client.get(
            "/api/v1/runs/run-traffic/target-http/exchanges/exchange-traffic"
        )

        assert listed.status_code == detailed.status_code == 200
        assert listed.json() == LIST_FIXTURE
        assert detailed.json() == DETAIL_FIXTURE
        assert service.calls[0]["method"] == "GET"
        assert service.calls[0]["status_class"] is TrafficStatusClass.SUCCESS
        assert service.calls[0]["limit"] == 50
        assert service.calls[1]["exchange_id"] == "exchange-traffic"
        assert all(isinstance(call["principal"], LocalPrincipal) for call in service.calls)

        traffic_paths = {
            path: item
            for path, item in app.openapi()["paths"].items()
            if "/target-http/exchanges" in path
        }
        assert set(traffic_paths) == {
            "/api/v1/runs/{run_id}/target-http/exchanges",
            "/api/v1/runs/{run_id}/target-http/exchanges/{exchange_id}",
        }
        assert all(set(item) == {"get"} for item in traffic_paths.values())


@pytest.mark.parametrize("field", ["attacker_field", "actor", "role", "capability"])
async def test_unknown_traffic_query_is_rejected_without_echoing_values(
    tmp_path: Path,
    field: str,
) -> None:
    canary = f"RIFTX_TEST_SECRET_DO_NOT_LEAK_TRAFFIC_QUERY_{field.upper()}"
    service = _RecordingTrafficService()
    async with _traffic_api(
        tmp_path,
        service,
        name=f"traffic-query-{field}",
    ) as (_app, client):
        response = await client.get(
            "/api/v1/runs/run-traffic/target-http/exchanges",
            params={field: canary},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"][0]["input"] == "[redacted]"
    assert canary not in response.text
    assert service.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("method", "GET-RIFTX_TEST_SECRET_DO_NOT_LEAK_TRAFFIC_METHOD"),
        ("status_class", "RIFTX_TEST_SECRET_DO_NOT_LEAK_TRAFFIC_STATUS"),
        ("cursor", "bad.cursor.RIFTX_TEST_SECRET_DO_NOT_LEAK_TRAFFIC_CURSOR"),
        ("limit", "RIFTX_TEST_SECRET_DO_NOT_LEAK_TRAFFIC_LIMIT"),
    ],
)
async def test_invalid_traffic_query_values_are_value_free(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    service = _RecordingTrafficService()
    async with _traffic_api(
        tmp_path,
        service,
        name=f"traffic-invalid-{field}",
    ) as (_app, client):
        response = await client.get(
            "/api/v1/runs/run-traffic/target-http/exchanges",
            params={field: value},
        )

    assert response.status_code == 422
    assert value not in response.text
    assert service.calls == []


async def test_invalid_traffic_exchange_path_is_value_free(tmp_path: Path) -> None:
    canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_TRAFFIC_PATH\u200b"
    service = _RecordingTrafficService()
    async with _traffic_api(tmp_path, service, name="traffic-invalid-path") as (_app, client):
        response = await client.get(f"/api/v1/runs/run-traffic/target-http/exchanges/{canary}")

    assert response.status_code == 422
    assert "RIFTX_TEST_SECRET_DO_NOT_LEAK_TRAFFIC_PATH" not in response.text
    assert response.json()["error"]["details"][0]["input"] == "[redacted]"
    assert service.calls == []


async def test_traffic_routes_require_authentication_and_read_capability(
    tmp_path: Path,
) -> None:
    unauthenticated_service = _RecordingTrafficService()
    async with _traffic_api(
        tmp_path,
        unauthenticated_service,
        name="traffic-401",
        authenticated=False,
    ) as (_app, client):
        unauthenticated = await client.get("/api/v1/runs/run-traffic/target-http/exchanges")
    assert unauthenticated.status_code == 401
    assert unauthenticated_service.calls == []

    unauthorized_service = _RecordingTrafficService()
    async with _traffic_api(
        tmp_path,
        unauthorized_service,
        name="traffic-403",
        capabilities=frozenset(),
    ) as (_app, client):
        unauthorized = await client.get("/api/v1/runs/run-traffic/target-http/exchanges")
    assert unauthorized.status_code == 403
    assert unauthorized.json()["error"]["code"] == "local_operator_capability_denied"
    assert unauthorized_service.calls == []


async def test_unknown_and_foreign_traffic_resources_are_indistinguishable(
    tmp_path: Path,
) -> None:
    responses: list[dict[str, object]] = []
    for suffix in ("missing", "foreign"):
        service = _RecordingTrafficService(
            error=ResourceNotAccessibleError(
                "resource_not_accessible",
                "The requested resource was not found",
            )
        )
        async with _traffic_api(
            tmp_path,
            service,
            name=f"traffic-{suffix}",
        ) as (_app, client):
            response = await client.get(
                f"/api/v1/runs/run-{suffix}/target-http/exchanges/exchange-{suffix}"
            )
        assert response.status_code == 404
        responses.append(response.json())

    assert responses[0] == responses[1]
    assert responses[0]["error"] == {
        "code": "resource_not_accessible",
        "message": "The requested resource was not found",
        "details": {},
    }


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InvalidTrafficCursorError(), 422, "invalid_traffic_cursor"),
        (StaleTrafficCursorError(), 409, "stale_traffic_cursor"),
    ],
)
async def test_traffic_cursor_errors_are_generic(
    tmp_path: Path,
    error: Exception,
    status: int,
    code: str,
) -> None:
    canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_TRAFFIC_SIGNED_CURSOR"
    service = _RecordingTrafficService(error=error)
    async with _traffic_api(
        tmp_path,
        service,
        name=f"traffic-cursor-{status}",
    ) as (_app, client):
        response = await client.get(
            "/api/v1/runs/run-traffic/target-http/exchanges",
            params={"cursor": canary},
        )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["details"] == {}
    assert canary not in response.text


async def test_traffic_routes_are_fail_closed_read_only_policy_entries(
    tmp_path: Path,
) -> None:
    service = _RecordingTrafficService()
    async with _traffic_api(tmp_path, service, name="traffic-policy") as (app, client):
        records = {
            record.name: record
            for record in app.state.route_policy_inventory
            if record.name in {"list_target_http_exchanges", "get_target_http_exchange"}
        }
        post = await client.post("/api/v1/runs/run-traffic/target-http/exchanges")

    assert set(records) == {"list_target_http_exchanges", "get_target_http_exchange"}
    assert all(record.methods == ("GET",) for record in records.values())
    assert all(
        record.policy.authorization is RouteAuthorization.LOCAL_OPERATOR
        and record.policy.effect is RouteEffect.READ_ONLY
        for record in records.values()
    )
    assert post.status_code in {404, 405}
    assert service.calls == []
