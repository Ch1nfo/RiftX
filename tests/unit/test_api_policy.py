from __future__ import annotations

import tomllib
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from starlette.requests import HTTPConnection, Request

from riftx.api.app import create_app
from riftx.api.auth import _required_admin_capability, _required_local_capability
from riftx.api.dependencies import (
    authorize_admin,
    authorize_local_operator,
    authorize_runner,
)
from riftx.api.policy import (
    ROUTE_POLICIES,
    RouteAuthorization,
    RouteEffect,
    RoutePolicy,
    _authentication_dependencies,
    apply_route_policy_inventory,
)
from riftx.api.runtime import APISettings
from riftx.application.errors import AuthenticationError, AuthorizationError
from riftx.domain import LocalPrincipal, OperatorCapability, TrustProfile
from riftx.security import LocalOperatorSecurity

_TEST_OPERATOR_TOKEN = "test-only-local-operator-token-0001"


def test_supported_fastapi_range_preserves_eager_route_inventory() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "fastapi>=0.136,<0.137" in project["project"]["dependencies"]


def test_control_plane_route_policy_inventory_is_complete_and_in_openapi(tmp_path) -> None:
    app = create_app(
        settings=APISettings(
            trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
            local_principal_path=tmp_path / "principal.json",
            admin_token=_TEST_OPERATOR_TOKEN,
            web_dist_path=tmp_path / "missing-web",
            connectors_enabled=True,
        )
    )
    inventory = app.state.route_policy_inventory

    assert len(inventory) == len(ROUTE_POLICIES)
    assert {record.name for record in inventory} == set(ROUTE_POLICIES)
    assert ROUTE_POLICIES["cancel_run"].effect is RouteEffect.WORKFLOW_CONTROL
    assert ROUTE_POLICIES["observe_browser"].effect is RouteEffect.HOST_CONTROL
    assert ROUTE_POLICIES["terminal_websocket"].authorization is RouteAuthorization.LOCAL_OPERATOR
    assert ROUTE_POLICIES["upsert_model_profile"].authorization is RouteAuthorization.ADMIN_TOKEN
    assert (
        ROUTE_POLICIES["report_execution_status"].authorization is RouteAuthorization.RUNNER_TOKEN
    )

    openapi = app.openapi()
    cancel = openapi["paths"]["/api/v1/runs/{run_id}/cancel"]["post"]
    assert cancel["x-riftx-authorization"] == "local_operator"
    assert cancel["x-riftx-effect"] == "workflow_control"
    update_model = openapi["paths"]["/api/v1/model-profiles/{profile_name}"]["put"]
    assert update_model["x-riftx-authorization"] == "admin_token"
    assert update_model["x-riftx-effect"] == "durable_write"
    assert not any(path.startswith("/api/v1/audits") for path in openapi["paths"])
    assert not any(
        path.startswith("/api/v1/runner/audit-preflight")
        for path in openapi["paths"]
    )

    register_parameters = {
        (parameter["in"], parameter["name"]): parameter
        for parameter in openapi["paths"]["/api/v1/nodes/register"]["post"]["parameters"]
    }
    assert register_parameters[("header", "authorization")]["required"] is False

    heartbeat_parameters = {
        (parameter["in"], parameter["name"]): parameter
        for parameter in openapi["paths"]["/api/v1/nodes/{node_id}/heartbeat"]["post"]["parameters"]
    }
    assert heartbeat_parameters[("path", "node_id")]["required"] is True
    assert heartbeat_parameters[("header", "authorization")]["required"] is False
    assert ("header", "X-RiftX-Node-ID") not in heartbeat_parameters
    assert heartbeat_parameters[("header", "X-RiftX-Runner-Instance-ID")]["required"] is True
    assert heartbeat_parameters[("header", "X-RiftX-Runner-Epoch")]["required"] is True

    runner_parameters = {
        (parameter["in"], parameter["name"]): parameter
        for parameter in openapi["paths"]["/api/v1/runner/commands/next"]["get"]["parameters"]
    }
    assert runner_parameters[("header", "X-RiftX-Node-ID")]["required"] is True
    assert runner_parameters[("header", "X-RiftX-Runner-Instance-ID")]["required"] is True
    assert runner_parameters[("header", "X-RiftX-Runner-Epoch")]["required"] is True
    assert runner_parameters[("header", "authorization")]["required"] is False

    routes = {route.name: route for route in app.routes if hasattr(route, "dependant")}
    assert _authentication_dependencies(routes["cancel_run"].dependant) == (
        authorize_local_operator,
    )
    assert set(_authentication_dependencies(routes["upsert_model_profile"].dependant)) == {
        authorize_admin,
    }
    assert set(_authentication_dependencies(routes["report_execution_status"].dependant)) == {
        authorize_runner,
    }


def test_control_plane_route_policy_inventory_rejects_unknown_route() -> None:
    app = FastAPI()

    @app.post("/api/v1/unclassified", name="unclassified_effect")
    async def unclassified_effect() -> None:
        return None

    with pytest.raises(RuntimeError, match="unclassified_effect:/api/v1/unclassified"):
        apply_route_policy_inventory(app)


@pytest.mark.parametrize(
    ("route_name", "capability"),
    [
        ("list_runs", OperatorCapability.READ),
        ("create_run", OperatorCapability.WRITE),
        ("cancel_run", OperatorCapability.CONTROL),
        ("create_terminal", OperatorCapability.HOST_EXECUTE),
        ("observe_browser", OperatorCapability.HOST_CONTROL),
    ],
)
def test_route_effect_maps_to_the_required_local_operator_capability(
    route_name: str,
    capability: OperatorCapability,
) -> None:
    connection = HTTPConnection(
        {
            "type": "http",
            "headers": [],
            "route": SimpleNamespace(name=route_name),
        }
    )

    assert _required_local_capability(connection) is capability


@pytest.mark.parametrize("route_name", ["missing_route", "upsert_model_profile"])
def test_local_capability_resolution_rejects_missing_or_nonlocal_policy(
    route_name: str,
) -> None:
    connection = HTTPConnection(
        {
            "type": "http",
            "headers": [],
            "route": SimpleNamespace(name=route_name),
        }
    )

    with pytest.raises(AuthorizationError) as captured:
        _required_local_capability(connection)

    assert captured.value.code == "local_operator_policy_denied"


@pytest.mark.parametrize(
    ("route_name", "capability"),
    [
        ("list_tools_for_admin", OperatorCapability.READ),
        ("update_tool", OperatorCapability.WRITE),
        ("disconnect_node", OperatorCapability.CONTROL),
    ],
)
def test_admin_route_effect_maps_to_shared_operator_capability(
    route_name: str,
    capability: OperatorCapability,
) -> None:
    connection = HTTPConnection(
        {
            "type": "http",
            "headers": [],
            "route": SimpleNamespace(name=route_name),
        }
    )

    assert _required_admin_capability(connection) is capability


@pytest.mark.parametrize(
    ("route_name", "required"),
    [
        ("list_tools_for_admin", OperatorCapability.READ),
        ("update_tool", OperatorCapability.WRITE),
        ("disconnect_node", OperatorCapability.CONTROL),
    ],
)
def test_admin_authentication_enforces_shared_server_capabilities(
    route_name: str,
    required: OperatorCapability,
) -> None:
    principal = LocalPrincipal(
        id="local-principal:v1:test",
        capabilities=frozenset({required}),
    )
    security = LocalOperatorSecurity(
        principal=principal,
        configured_token=_TEST_OPERATOR_TOKEN,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(local_operator_security=security),
    )
    request = Request(
        {
            "type": "http",
            "headers": [],
            "app": app,
            "route": SimpleNamespace(name=route_name),
        }
    )

    assert authorize_admin(request, f"Bearer {_TEST_OPERATOR_TOKEN}") is principal

    restricted = LocalPrincipal(
        id="local-principal:v1:restricted",
        capabilities=frozenset(set(OperatorCapability) - {required}),
    )
    app.state.local_operator_security = LocalOperatorSecurity(
        principal=restricted,
        configured_token=_TEST_OPERATOR_TOKEN,
    )
    with pytest.raises(AuthorizationError) as captured:
        authorize_admin(request, f"Bearer {_TEST_OPERATOR_TOKEN}")

    assert captured.value.code == "local_operator_capability_denied"


def test_admin_authentication_rejects_duplicate_or_smuggled_authorization_headers() -> None:
    principal = LocalPrincipal(
        id="local-principal:v1:test",
        capabilities=frozenset(OperatorCapability),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            local_operator_security=LocalOperatorSecurity(
                principal=principal,
                configured_token=_TEST_OPERATOR_TOKEN,
            )
        )
    )
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"authorization", f"Bearer {_TEST_OPERATOR_TOKEN}".encode()),
                (b"authorization", b"Basic smuggled-credential"),
            ],
            "app": app,
            "route": SimpleNamespace(name="list_tools_for_admin"),
        }
    )

    with pytest.raises(AuthenticationError) as captured:
        authorize_admin(request, f"Bearer {_TEST_OPERATOR_TOKEN}")

    assert captured.value.code == "admin_authentication_failed"


def test_admin_capability_resolution_rejects_unknown_effect(monkeypatch) -> None:
    route_name = "unknown_admin_effect"
    monkeypatch.setattr(
        "riftx.api.policy.ROUTE_POLICIES",
        MappingProxyType(
            {
                route_name: RoutePolicy(
                    authorization=RouteAuthorization.ADMIN_TOKEN,
                    effect=RouteEffect.ADMINISTRATION,
                )
            }
        ),
    )
    connection = HTTPConnection(
        {
            "type": "http",
            "headers": [],
            "route": SimpleNamespace(name=route_name),
        }
    )

    with pytest.raises(AuthorizationError) as captured:
        _required_admin_capability(connection)

    assert captured.value.code == "local_operator_policy_denied"


def test_policy_inventory_rejects_unsupported_local_operator_effect(monkeypatch) -> None:
    app = FastAPI()

    @app.post(
        "/api/v1/unsupported-local-effect",
        name="unsupported_local_effect",
        dependencies=[Depends(authorize_local_operator)],
    )
    async def unsupported_local_effect() -> None:
        return None

    monkeypatch.setattr(
        "riftx.api.policy.ROUTE_POLICIES",
        MappingProxyType(
            {
                "unsupported_local_effect": RoutePolicy(
                    authorization=RouteAuthorization.LOCAL_OPERATOR,
                    effect=RouteEffect.ADMINISTRATION,
                )
            }
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="unsupported_operator_effects=.*unsupported_local_effect:administration",
    ):
        apply_route_policy_inventory(app)


def test_policy_inventory_rejects_unsupported_admin_effect(monkeypatch) -> None:
    app = FastAPI()

    @app.post(
        "/api/v1/unsupported-admin-effect",
        name="unsupported_admin_effect",
        dependencies=[Depends(authorize_admin)],
    )
    async def unsupported_admin_effect() -> None:
        return None

    monkeypatch.setattr(
        "riftx.api.policy.ROUTE_POLICIES",
        MappingProxyType(
            {
                "unsupported_admin_effect": RoutePolicy(
                    authorization=RouteAuthorization.ADMIN_TOKEN,
                    effect=RouteEffect.ADMINISTRATION,
                )
            }
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="unsupported_operator_effects=.*unsupported_admin_effect:administration",
    ):
        apply_route_policy_inventory(app)


def test_admin_policy_requires_admin_dependency() -> None:
    app = FastAPI()

    @app.put("/api/v1/model-profiles/default", name="set_default_model_profile")
    async def unprotected_admin_route() -> None:
        return None

    with pytest.raises(RuntimeError, match="missing_admin_dependency=.*set_default_model_profile"):
        apply_route_policy_inventory(app)


@pytest.mark.parametrize(
    ("route_name", "path", "expected_dependency"),
    [
        (
            "register_node",
            "/api/v1/nodes/register",
            "authorize_runner_bootstrap",
        ),
        (
            "poll_runner_command",
            "/api/v1/runner/commands/next",
            "authorize_runner",
        ),
        (
            "heartbeat_node",
            "/api/v1/nodes/{node_id}/heartbeat",
            "authorize_runner_node",
        ),
    ],
)
def test_runner_policy_requires_its_authentication_dependency(
    route_name: str,
    path: str,
    expected_dependency: str,
) -> None:
    app = FastAPI()

    @app.post(path, name=route_name)
    async def unprotected_runner_route() -> None:
        return None

    with pytest.raises(
        RuntimeError,
        match=(
            rf"authentication_dependency_mismatches=.*{route_name}:"
            rf"expected={expected_dependency}"
        ),
    ):
        apply_route_policy_inventory(app)


def test_runner_policy_rejects_the_wrong_authentication_dependency() -> None:
    app = FastAPI()

    @app.post(
        "/api/v1/nodes/register",
        name="register_node",
        dependencies=[Depends(authorize_admin)],
    )
    async def runner_route_with_admin_authentication() -> None:
        return None

    with pytest.raises(
        RuntimeError,
        match=(
            "authentication_dependency_mismatches=.*register_node:"
            "expected=authorize_runner_bootstrap"
        ),
    ):
        apply_route_policy_inventory(app)


def test_local_operator_policy_requires_exact_local_principal_dependency() -> None:
    app = FastAPI()

    @app.get(
        "/api/v1/runs",
        name="list_runs",
        dependencies=[Depends(authorize_runner)],
    )
    async def overprotected_local_route() -> None:
        return None

    with pytest.raises(
        RuntimeError,
        match=(
            "authentication_dependency_mismatches=.*list_runs:expected=authorize_local_operator"
        ),
    ):
        apply_route_policy_inventory(app)
