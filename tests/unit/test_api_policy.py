from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI

from riftx.api.app import create_app
from riftx.api.dependencies import authorize_admin, authorize_runner
from riftx.api.policy import (
    ROUTE_POLICIES,
    RouteAuthorization,
    RouteEffect,
    apply_route_policy_inventory,
)


def test_control_plane_route_policy_inventory_is_complete_and_in_openapi() -> None:
    app = create_app()
    inventory = app.state.route_policy_inventory

    assert len(inventory) == len(ROUTE_POLICIES)
    assert {record.name for record in inventory} == set(ROUTE_POLICIES)
    assert ROUTE_POLICIES["cancel_run"].effect is RouteEffect.WORKFLOW_CONTROL
    assert ROUTE_POLICIES["terminal_websocket"].authorization is RouteAuthorization.LOCAL_OPERATOR
    assert ROUTE_POLICIES["upsert_model_profile"].authorization is RouteAuthorization.ADMIN_TOKEN
    assert (
        ROUTE_POLICIES["report_execution_status"].authorization is RouteAuthorization.RUNNER_TOKEN
    )

    openapi = app.openapi()
    cancel = openapi["paths"]["/api/v1/runs/{run_id}/cancel"]["post"]
    assert cancel["x-riftx-authorization"] == "local_operator_or_authenticated_proxy"
    assert cancel["x-riftx-effect"] == "workflow_control"
    update_model = openapi["paths"]["/api/v1/model-profiles/{profile_name}"]["put"]
    assert update_model["x-riftx-authorization"] == "admin_token"
    assert update_model["x-riftx-effect"] == "administration"

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


def test_control_plane_route_policy_inventory_rejects_unknown_route() -> None:
    app = FastAPI()

    @app.post("/api/v1/unclassified", name="unclassified_effect")
    async def unclassified_effect() -> None:
        return None

    with pytest.raises(RuntimeError, match="unclassified_effect:/api/v1/unclassified"):
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


def test_local_operator_policy_rejects_an_added_token_dependency() -> None:
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
        match="authentication_dependency_mismatches=.*list_runs:expected=none",
    ):
        apply_route_policy_inventory(app)
