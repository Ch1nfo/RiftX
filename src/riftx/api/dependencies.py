"""FastAPI dependency accessors for the control-plane service container."""

import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from riftx.application.errors import AuthenticationError, ResourceNotAccessibleError
from riftx.application.services import (
    ActionApplicationService,
    ApprovalApplicationService,
    ArtifactApplicationService,
    EventApplicationService,
    ExecutionApplicationService,
    FindingApplicationService,
    ModelProfileApplicationService,
    NodeApplicationService,
    ReportApplicationService,
    RunApplicationService,
    RunnerControlService,
    TerminalApplicationService,
    ToolApplicationService,
)
from riftx.application.services.graphs import GraphApplicationService
from riftx.browser.service import BrowserApplicationService
from riftx.connectors.service import ConnectorApplicationService
from riftx.context import ContextApplicationService
from riftx.domain import LocalPrincipal, OperatorCapability, RunnerPrincipal
from riftx.memory import MemoryService
from riftx.observability import RuntimeObservabilityService
from riftx.security import LocalObjectAuthorizer

from .auth import (
    authorize_local_operator as authorize_local_operator,
)
from .auth import (
    bearer_token,
    get_authenticated_local_principal,
    require_admin_token,
)


@dataclass(frozen=True, slots=True)
class RunnerAuthorization:
    """A Runner identity already authenticated by a FastAPI dependency."""

    node_id: str
    token: str
    principal: RunnerPrincipal


class _GraphObjectAuthorizer:
    """Adapt the app-owned Run authorizer to the Graph service's scope port."""

    def __init__(self, delegate: LocalObjectAuthorizer) -> None:
        self.delegate = delegate

    def require_run_engagement(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        parent_engagement_id: str,
        resource_engagement_id: str | None,
        capability: OperatorCapability,
    ) -> None:
        self.delegate.require_child_run(
            principal,
            parent_run_id=parent_run_id,
            resource_run_id=resource_run_id,
            capability=capability,
        )
        if resource_engagement_id is None or not secrets.compare_digest(
            parent_engagement_id,
            resource_engagement_id,
        ):
            raise ResourceNotAccessibleError(
                "resource_not_accessible",
                "The requested resource was not found",
            )


def get_control_plane(request: Request) -> object:
    return request.app.state.control_plane


def get_run_service(request: Request) -> RunApplicationService:
    return request.app.state.control_plane.run_service


def get_action_service(request: Request) -> ActionApplicationService:
    return request.app.state.control_plane.action_service


def get_graph_service(request: Request) -> GraphApplicationService:
    """Build one app-resident Graph service with the server's exact authorizer/key."""

    service = getattr(request.app.state, "graph_service", None)
    if service is None:
        repository = request.app.state.control_plane.graph_repository
        authorizer = _GraphObjectAuthorizer(
            request.app.state.local_object_authorizer,
        )
        service = GraphApplicationService(
            repository,
            authorizer=authorizer,
            cursor_signing_key=request.app.state.graph_cursor_signing_key,
        )
        request.app.state.graph_object_authorizer = authorizer
        request.app.state.graph_service = service
    return service


def get_event_service(request: Request) -> EventApplicationService:
    return request.app.state.control_plane.event_service


def get_execution_service(request: Request) -> ExecutionApplicationService:
    return request.app.state.control_plane.execution_service


def get_finding_service(request: Request) -> FindingApplicationService:
    return request.app.state.control_plane.finding_service


def get_node_service(request: Request) -> NodeApplicationService:
    return request.app.state.control_plane.node_service


def get_runner_control_service(request: Request) -> RunnerControlService:
    return request.app.state.control_plane.runner_control_service


def get_report_service(request: Request) -> ReportApplicationService:
    return request.app.state.control_plane.report_service


def get_tool_service(request: Request) -> ToolApplicationService:
    return request.app.state.control_plane.tool_service


def get_approval_service(request: Request) -> ApprovalApplicationService:
    return request.app.state.control_plane.approval_service


def get_artifact_service(request: Request) -> ArtifactApplicationService:
    return request.app.state.control_plane.artifact_service


def get_terminal_service(request: Request) -> TerminalApplicationService:
    return request.app.state.control_plane.terminal_service


def get_browser_service(request: Request) -> BrowserApplicationService:
    return request.app.state.control_plane.browser_service


def get_connector_service(request: Request) -> ConnectorApplicationService:
    return request.app.state.control_plane.connector_service


def get_context_service(request: Request) -> ContextApplicationService:
    return request.app.state.control_plane.context_service


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.control_plane.memory_service


def get_runtime_observability_service(request: Request) -> RuntimeObservabilityService:
    return request.app.state.control_plane.runtime_observability_service


def get_model_profile_service(request: Request) -> ModelProfileApplicationService:
    return request.app.state.control_plane.model_profile_service


def authorize_admin(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> LocalPrincipal:
    return require_admin_token(request, authorization)


RunServiceDependency = Annotated[RunApplicationService, Depends(get_run_service)]
ActionServiceDependency = Annotated[ActionApplicationService, Depends(get_action_service)]
GraphServiceDependency = Annotated[GraphApplicationService, Depends(get_graph_service)]
EventServiceDependency = Annotated[EventApplicationService, Depends(get_event_service)]
ExecutionServiceDependency = Annotated[ExecutionApplicationService, Depends(get_execution_service)]
FindingServiceDependency = Annotated[FindingApplicationService, Depends(get_finding_service)]
NodeServiceDependency = Annotated[NodeApplicationService, Depends(get_node_service)]
ReportServiceDependency = Annotated[ReportApplicationService, Depends(get_report_service)]
RunnerControlServiceDependency = Annotated[
    RunnerControlService, Depends(get_runner_control_service)
]
ToolServiceDependency = Annotated[ToolApplicationService, Depends(get_tool_service)]
ApprovalServiceDependency = Annotated[
    ApprovalApplicationService,
    Depends(get_approval_service),
]
ArtifactServiceDependency = Annotated[
    ArtifactApplicationService,
    Depends(get_artifact_service),
]
TerminalServiceDependency = Annotated[
    TerminalApplicationService,
    Depends(get_terminal_service),
]
BrowserServiceDependency = Annotated[
    BrowserApplicationService,
    Depends(get_browser_service),
]
ConnectorServiceDependency = Annotated[
    ConnectorApplicationService,
    Depends(get_connector_service),
]
ContextServiceDependency = Annotated[
    ContextApplicationService,
    Depends(get_context_service),
]
MemoryServiceDependency = Annotated[MemoryService, Depends(get_memory_service)]

RuntimeObservabilityServiceDependency = Annotated[
    RuntimeObservabilityService, Depends(get_runtime_observability_service)
]
ModelProfileServiceDependency = Annotated[
    ModelProfileApplicationService,
    Depends(get_model_profile_service),
]
LocalPrincipalDependency = Annotated[
    LocalPrincipal,
    Depends(get_authenticated_local_principal),
]


async def authorize_runner_bootstrap(
    service: RunnerControlServiceDependency,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Authenticate the shared bootstrap credential before registration runs."""

    token = bearer_token(authorization)
    service.authenticate_bootstrap(token)
    return token


async def authorize_runner(
    service: RunnerControlServiceDependency,
    node_id: Annotated[str, Header(alias="X-RiftX-Node-ID")],
    runner_instance_id: Annotated[
        str,
        Header(alias="X-RiftX-Runner-Instance-ID", min_length=1, max_length=64),
    ],
    runner_epoch: Annotated[int, Header(alias="X-RiftX-Runner-Epoch", ge=1)],
    authorization: Annotated[str | None, Header()] = None,
) -> RunnerAuthorization:
    """Authenticate a Runner callback carrying its node ID in a header."""

    token = bearer_token(authorization)
    principal = RunnerPrincipal(instance_id=runner_instance_id, epoch=runner_epoch)
    credential = await service.authenticate(node_id, token)
    _require_matching_runner_principal(credential.principal, principal)
    return RunnerAuthorization(node_id=node_id, token=token, principal=principal)


async def authorize_runner_node(
    node_id: str,
    service: RunnerControlServiceDependency,
    runner_instance_id: Annotated[
        str,
        Header(alias="X-RiftX-Runner-Instance-ID", min_length=1, max_length=64),
    ],
    runner_epoch: Annotated[int, Header(alias="X-RiftX-Runner-Epoch", ge=1)],
    authorization: Annotated[str | None, Header()] = None,
) -> RunnerAuthorization:
    """Authenticate a Runner callback whose node ID is a path parameter."""

    token = bearer_token(authorization)
    principal = RunnerPrincipal(instance_id=runner_instance_id, epoch=runner_epoch)
    credential = await service.authenticate(node_id, token)
    _require_matching_runner_principal(credential.principal, principal)
    return RunnerAuthorization(node_id=node_id, token=token, principal=principal)


def _require_matching_runner_principal(
    authenticated: RunnerPrincipal,
    declared: RunnerPrincipal,
) -> None:
    if authenticated != declared:
        # Keep the response indistinguishable from an invalid token. A caller
        # must possess one complete server-issued credential tuple; mixing a
        # token with cloned or stale principal metadata fails closed.
        raise AuthenticationError(
            "runner_authentication_failed",
            "Runner credentials are missing or invalid",
        )


AdminDependency = Annotated[
    LocalPrincipal,
    Depends(authorize_admin),
]
ModelProfileAdminDependency = AdminDependency
RunnerBootstrapDependency = Annotated[
    str,
    Depends(authorize_runner_bootstrap),
]
RunnerDependency = Annotated[
    RunnerAuthorization,
    Depends(authorize_runner),
]
RunnerNodeDependency = Annotated[
    RunnerAuthorization,
    Depends(authorize_runner_node),
]
