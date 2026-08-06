"""Machine-enumerable, fail-closed policy inventory for Control Plane routes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_parameterless_sub_dependant
from fastapi.params import Depends
from fastapi.routing import APIRoute, APIWebSocketRoute

from .dependencies import (
    authorize_admin,
    authorize_audit_preflight_runner,
    authorize_local_operator,
    authorize_runner,
    authorize_runner_bootstrap,
    authorize_runner_node,
)


class RouteAuthorization(StrEnum):
    LOCAL_OPERATOR = "local_operator"
    ADMIN_TOKEN = "admin_token"
    RUNNER_BOOTSTRAP_TOKEN = "runner_bootstrap_token"
    RUNNER_TOKEN = "runner_token"


class RouteEffect(StrEnum):
    READ_ONLY = "read_only"
    DURABLE_WRITE = "durable_write"
    WORKFLOW_CONTROL = "workflow_control"
    HOST_EXECUTION = "host_execution"
    HOST_CONTROL = "host_control"
    ADMINISTRATION = "administration"
    RUNNER_CALLBACK = "runner_callback"


_LOCAL_OPERATOR_EFFECTS = frozenset(
    {
        RouteEffect.READ_ONLY,
        RouteEffect.DURABLE_WRITE,
        RouteEffect.WORKFLOW_CONTROL,
        RouteEffect.HOST_EXECUTION,
        RouteEffect.HOST_CONTROL,
    }
)
_ADMIN_OPERATOR_EFFECTS = frozenset(
    {
        RouteEffect.READ_ONLY,
        RouteEffect.DURABLE_WRITE,
        RouteEffect.WORKFLOW_CONTROL,
    }
)


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    authorization: RouteAuthorization
    effect: RouteEffect


@dataclass(frozen=True, slots=True)
class RoutePolicyRecord:
    name: str
    path: str
    methods: tuple[str, ...]
    policy: RoutePolicy


def _policy(
    authorization: RouteAuthorization,
    effect: RouteEffect,
) -> RoutePolicy:
    return RoutePolicy(authorization=authorization, effect=effect)


_POLICY_GROUPS: tuple[tuple[RoutePolicy, frozenset[str]], ...] = (
    (
        _policy(RouteAuthorization.LOCAL_OPERATOR, RouteEffect.READ_ONLY),
        frozenset(
            {
                "list_runs",
                "get_run",
                "list_audits",
                "get_audit",
                "list_local_audit_findings",
                "get_local_audit_finding",
                "get_local_audit_report",
                "get_audit_preflight",
                "list_run_actions",
                "get_run_action",
                "get_run_graph",
                "get_observer_projection",
                "list_target_http_exchanges",
                "get_target_http_exchange",
                "list_nodes",
                "get_node",
                "get_run_metrics",
                "list_tools",
                "list_events",
                "stream_events",
                "get_execution",
                "wait_execution",
                "get_execution_output",
                "list_run_executions",
                "list_findings",
                "get_finding",
                "list_memories",
                "search_memories",
                "get_memory",
                "list_model_profiles",
                "list_reports",
                "get_report",
                "list_approvals",
                "list_artifacts",
                "get_artifact",
                "download_artifact",
                "list_audit_artifacts",
                "get_audit_artifact",
                "download_audit_artifact",
                "get_session_context",
                "get_context_compilation",
                "get_run_context",
                "get_terminal",
                "get_browser",
                "list_connector_runs",
                "connector_events",
                "connector_webui",
                "get_security_profile",
                "get_system_diagnostics",
                "api_not_found",
            }
        ),
    ),
    (
        _policy(RouteAuthorization.LOCAL_OPERATOR, RouteEffect.DURABLE_WRITE),
        frozenset(
            {
                "create_run",
                "create_audit",
                "issue_audit_preflight_plan",
                "create_finding",
                "update_finding",
                "create_memory",
                "update_memory",
                "delete_memory",
                "pin_memory",
                "generate_reports",
                "register_artifact",
                "submit_http_capture",
            }
        ),
    ),
    (
        _policy(RouteAuthorization.LOCAL_OPERATOR, RouteEffect.WORKFLOW_CONTROL),
        frozenset(
            {
                "pause_run",
                "pause_audit",
                "resume_run",
                "resume_audit",
                "cancel_run",
                "compact_run",
                "switch_run_model",
                "cancel_current_execution",
                "append_message",
                "cancel_execution",
                "approve",
                "reject",
                "cancel_connector_run",
            }
        ),
    ),
    (
        _policy(RouteAuthorization.LOCAL_OPERATOR, RouteEffect.HOST_EXECUTION),
        frozenset(
            {
                "create_audit_preflight",
                "start_audit",
                "create_terminal",
                "open_browser",
            }
        ),
    ),
    (
        _policy(RouteAuthorization.LOCAL_OPERATOR, RouteEffect.HOST_CONTROL),
        frozenset(
            {
                "close_terminal",
                "cancel_audit",
                "cancel_audit_preflight",
                "terminal_websocket",
                "close_browser",
                "act_browser",
                "takeover_browser",
                "release_browser",
                "stream_browser",
                "observe_browser",
            }
        ),
    ),
    (
        _policy(RouteAuthorization.ADMIN_TOKEN, RouteEffect.READ_ONLY),
        frozenset(
            {
                "list_tools_for_admin",
                "list_model_profiles_for_admin",
                "get_model_profile",
            }
        ),
    ),
    (
        _policy(RouteAuthorization.ADMIN_TOKEN, RouteEffect.DURABLE_WRITE),
        frozenset(
            {
                "refresh_tools",
                "update_tool",
                "set_default_model_profile",
                "upsert_model_profile",
                "delete_model_profile",
            }
        ),
    ),
    (
        _policy(RouteAuthorization.ADMIN_TOKEN, RouteEffect.WORKFLOW_CONTROL),
        frozenset({"disconnect_node"}),
    ),
    (
        _policy(RouteAuthorization.RUNNER_BOOTSTRAP_TOKEN, RouteEffect.RUNNER_CALLBACK),
        frozenset({"register_node"}),
    ),
    (
        _policy(RouteAuthorization.RUNNER_TOKEN, RouteEffect.RUNNER_CALLBACK),
        frozenset(
            {
                "heartbeat_node",
                "poll_audit_preflight_job",
                "renew_audit_preflight_lease",
                "start_audit_preflight_job",
                "finish_audit_preflight_job",
                "stop_audit_preflight_job",
                "poll_runner_command",
                "finish_legacy_runner_command",
                "finish_runner_command",
                "renew_runner_command_lease",
                "report_runner_command_output",
                "report_execution_status",
                "report_execution_output",
            }
        ),
    ),
)


def _build_route_policies() -> MappingProxyType[str, RoutePolicy]:
    policies: dict[str, RoutePolicy] = {}
    duplicates: list[str] = []
    for policy, names in _POLICY_GROUPS:
        for name in names:
            if name in policies:
                duplicates.append(name)
            policies[name] = policy
    if duplicates:
        raise RuntimeError(f"duplicate route policies: {sorted(duplicates)}")
    return MappingProxyType(policies)


ROUTE_POLICIES = _build_route_policies()

_AUTHENTICATION_DEPENDENCIES = (
    authorize_local_operator,
    authorize_admin,
    authorize_runner_bootstrap,
    authorize_audit_preflight_runner,
    authorize_runner,
    authorize_runner_node,
)


def _expected_authentication_dependency(
    route_name: str,
    authorization: RouteAuthorization,
) -> object | None:
    if authorization is RouteAuthorization.ADMIN_TOKEN:
        return authorize_admin
    if authorization is RouteAuthorization.RUNNER_BOOTSTRAP_TOKEN:
        return authorize_runner_bootstrap
    if authorization is RouteAuthorization.RUNNER_TOKEN:
        if route_name in {
            "poll_audit_preflight_job",
            "renew_audit_preflight_lease",
            "start_audit_preflight_job",
            "finish_audit_preflight_job",
            "stop_audit_preflight_job",
        }:
            return authorize_audit_preflight_runner
        return authorize_runner_node if route_name == "heartbeat_node" else authorize_runner
    if authorization is RouteAuthorization.LOCAL_OPERATOR:
        return authorize_local_operator
    return None


def install_local_operator_dependencies(app: FastAPI) -> None:
    """Attach local authentication only to routes classified for that principal."""

    for route in app.routes:
        if not isinstance(route, (APIRoute, APIWebSocketRoute)):
            continue
        policy = ROUTE_POLICIES.get(route.name)
        if policy is None or policy.authorization is not RouteAuthorization.LOCAL_OPERATOR:
            continue
        actual = _authentication_dependencies(route.dependant)
        if authorize_local_operator in actual:
            continue
        route.dependant.dependencies.insert(
            0,
            get_parameterless_sub_dependant(
                depends=Depends(authorize_local_operator),
                path=route.path,
            ),
        )


def apply_route_policy_inventory(app: FastAPI) -> tuple[RoutePolicyRecord, ...]:
    """Annotate all API routes and reject unclassified or stale policy entries."""

    records: list[RoutePolicyRecord] = []
    seen_names: set[str] = set()
    unclassified: list[str] = []
    duplicate_names: list[str] = []
    missing_admin_dependency: list[str] = []
    authentication_dependency_mismatches: list[str] = []
    unsupported_operator_effects: list[str] = []

    for route in app.routes:
        if not isinstance(route, (APIRoute, APIWebSocketRoute)):
            continue
        if not route.path.startswith("/api/") and route.path != "/api":
            continue
        name = route.name
        if name in seen_names:
            duplicate_names.append(name)
        seen_names.add(name)
        policy = ROUTE_POLICIES.get(name)
        if policy is None:
            unclassified.append(f"{name}:{route.path}")
            continue
        supported_operator_effects = {
            RouteAuthorization.LOCAL_OPERATOR: _LOCAL_OPERATOR_EFFECTS,
            RouteAuthorization.ADMIN_TOKEN: _ADMIN_OPERATOR_EFFECTS,
        }.get(policy.authorization)
        if (
            supported_operator_effects is not None
            and policy.effect not in supported_operator_effects
        ):
            unsupported_operator_effects.append(f"{name}:{policy.effect.value}")
        expected_dependency = _expected_authentication_dependency(name, policy.authorization)
        actual_dependencies = _authentication_dependencies(route.dependant)
        dependency_matches = (
            not actual_dependencies
            if expected_dependency is None
            else len(actual_dependencies) == 1 and actual_dependencies[0] is expected_dependency
        )
        if not dependency_matches:
            expected_name = (
                getattr(expected_dependency, "__name__", repr(expected_dependency))
                if expected_dependency is not None
                else "none"
            )
            actual_names = sorted(
                getattr(dependency, "__name__", repr(dependency))
                for dependency in actual_dependencies
            )
            authentication_dependency_mismatches.append(
                f"{name}:expected={expected_name}:actual={actual_names}"
            )
        if (
            policy.authorization is RouteAuthorization.ADMIN_TOKEN
            and authorize_admin not in actual_dependencies
        ):
            missing_admin_dependency.append(name)
        if isinstance(route, APIRoute):
            route.openapi_extra = {
                **(route.openapi_extra or {}),
                "x-riftx-authorization": policy.authorization.value,
                "x-riftx-effect": policy.effect.value,
            }
            methods = tuple(sorted((route.methods or set()) - {"HEAD", "OPTIONS"}))
        else:
            methods = ("WEBSOCKET",)
        records.append(
            RoutePolicyRecord(
                name=name,
                path=route.path,
                methods=methods,
                policy=policy,
            )
        )

    stale = sorted(ROUTE_POLICIES.keys() - seen_names)
    if (
        unclassified
        or duplicate_names
        or stale
        or missing_admin_dependency
        or authentication_dependency_mismatches
        or unsupported_operator_effects
    ):
        raise RuntimeError(
            "Control Plane route policy inventory validation failed: "
            f"unclassified={sorted(unclassified)}, "
            f"duplicate_names={sorted(duplicate_names)}, stale={stale}, "
            f"missing_admin_dependency={sorted(missing_admin_dependency)}, "
            "authentication_dependency_mismatches="
            f"{sorted(authentication_dependency_mismatches)}, "
            f"unsupported_operator_effects={sorted(unsupported_operator_effects)}"
        )
    inventory = tuple(sorted(records, key=lambda item: (item.path, item.methods, item.name)))
    app.state.route_policy_inventory = inventory
    return inventory


def _authentication_dependencies(dependant: Dependant) -> tuple[object, ...]:
    pending = [dependant]
    dependencies: list[object] = []
    while pending:
        current = pending.pop()
        if any(current.call is target for target in _AUTHENTICATION_DEPENDENCIES):
            dependencies.append(current.call)
        pending.extend(current.dependencies)
    return tuple(dependencies)
