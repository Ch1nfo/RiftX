"""FastAPI dependency accessors for the control-plane service container."""

import secrets
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request, WebSocket

from riftx.application.errors import (
    AuthenticationError,
    EntityNotFoundError,
    ResourceNotAccessibleError,
    ServiceUnavailableError,
    resource_not_accessible,
)
from riftx.application.ports import AuditObjectAuthorizer
from riftx.application.services import (
    ActionApplicationService,
    ApprovalApplicationService,
    ArtifactApplicationService,
    AuditApplicationService,
    AuditControlApplicationService,
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
    TrafficMetadataApplicationService,
)
from riftx.application.services.audit_preflight_runner import (
    AuditPreflightRunnerService,
)
from riftx.application.services.graphs import GraphApplicationService
from riftx.application.traffic import TrafficMetadataCapability
from riftx.browser.service import BrowserApplicationService
from riftx.connectors.service import ConnectorApplicationService
from riftx.context import ContextApplicationService
from riftx.domain import LocalPrincipal, OperatorCapability, Run, RunKind, RunnerPrincipal
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
    protocol_capabilities: tuple[str, ...] = ()


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


class _TrafficObjectAuthorizer:
    """Map the typed traffic capability onto Profile A's server READ grant."""

    def __init__(self, delegate: LocalObjectAuthorizer) -> None:
        self.delegate = delegate

    def require_traffic_metadata(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        parent_engagement_id: str,
        resource_engagement_id: str | None,
        capability: TrafficMetadataCapability,
    ) -> None:
        if capability is not TrafficMetadataCapability.READ:
            raise ResourceNotAccessibleError(
                "resource_not_accessible",
                "The requested resource was not found",
            )
        self.delegate.require_child_run(
            principal,
            parent_run_id=parent_run_id,
            resource_run_id=resource_run_id,
            capability=OperatorCapability.READ,
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


def get_audit_service(request: Request) -> AuditApplicationService:
    return request.app.state.control_plane.audit_service


def get_audit_control_service(request: Request) -> AuditControlApplicationService:
    service = getattr(request.app.state.control_plane, "audit_control_service", None)
    if service is None:
        raise ServiceUnavailableError(
            "audit_control_unavailable",
            "RiftX Code Audit controls are temporarily unavailable",
        )
    return service


def get_audit_object_authorizer(request: Request) -> AuditObjectAuthorizer:
    return request.app.state.local_object_authorizer


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


def get_traffic_metadata_service(request: Request) -> TrafficMetadataApplicationService:
    """Build the read service without injecting Runner or Artifact body services."""

    service = getattr(request.app.state, "traffic_metadata_service", None)
    if service is None:
        authorizer = _TrafficObjectAuthorizer(request.app.state.local_object_authorizer)
        service = TrafficMetadataApplicationService(
            request.app.state.control_plane.traffic_repository,
            authorizer=authorizer,
            cursor_signing_key=request.app.state.traffic_cursor_signing_key,
        )
        request.app.state.traffic_object_authorizer = authorizer
        request.app.state.traffic_metadata_service = service
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


def get_audit_preflight_runner_service(request: Request) -> AuditPreflightRunnerService:
    control_plane = getattr(request.app.state, "control_plane", None)
    service = getattr(
        control_plane,
        "audit_preflight_runner_service",
        None,
    )
    if not isinstance(service, AuditPreflightRunnerService):
        raise ServiceUnavailableError(
            "audit_preflight_runner_unavailable",
            "RiftX Code Audit Preflight Runner transport is temporarily unavailable",
        )
    return service


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
AuditServiceDependency = Annotated[AuditApplicationService, Depends(get_audit_service)]
AuditControlServiceDependency = Annotated[
    AuditControlApplicationService,
    Depends(get_audit_control_service),
]
AuditObjectAuthorizerDependency = Annotated[
    AuditObjectAuthorizer,
    Depends(get_audit_object_authorizer),
]
ActionServiceDependency = Annotated[ActionApplicationService, Depends(get_action_service)]
GraphServiceDependency = Annotated[GraphApplicationService, Depends(get_graph_service)]
TrafficMetadataServiceDependency = Annotated[
    TrafficMetadataApplicationService,
    Depends(get_traffic_metadata_service),
]
EventServiceDependency = Annotated[EventApplicationService, Depends(get_event_service)]
ExecutionServiceDependency = Annotated[ExecutionApplicationService, Depends(get_execution_service)]
FindingServiceDependency = Annotated[FindingApplicationService, Depends(get_finding_service)]
NodeServiceDependency = Annotated[NodeApplicationService, Depends(get_node_service)]
ReportServiceDependency = Annotated[ReportApplicationService, Depends(get_report_service)]
RunnerControlServiceDependency = Annotated[
    RunnerControlService, Depends(get_runner_control_service)
]
AuditPreflightRunnerServiceDependency = Annotated[
    AuditPreflightRunnerService,
    Depends(get_audit_preflight_runner_service),
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


@dataclass(frozen=True, slots=True)
class RunReadAuthorizationSnapshot:
    """Frozen owner and authenticated principal for one long-lived Run read."""

    run_id: str
    run_kind: RunKind
    engagement_id: str
    node_id: str
    principal: LocalPrincipal
    audit_id: str | None = None
    audit_project_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunReadAuthorizer:
    run_service: RunApplicationService
    audit_service: AuditApplicationService
    principal: LocalPrincipal
    audit_authorizer: AuditObjectAuthorizer

    async def require(self, run_id: str) -> Run:
        """Route an Audit-owned generic read through its Audit ACL root."""

        try:
            kind = await self.run_service.resolve_kind(run_id)
            if kind is RunKind.CODE_AUDIT:
                aggregate = await self.audit_service.get_by_run_authorized(
                    run_id,
                    principal=self.principal,
                    authorizer=self.audit_authorizer,
                )
                return aggregate.run
            return await self.run_service.get_run(run_id)
        except (EntityNotFoundError, ResourceNotAccessibleError):
            raise resource_not_accessible() from None

    async def require_stream_snapshot(self, run_id: str) -> RunReadAuthorizationSnapshot:
        """Authorize and freeze the owner graph used by a long-lived stream."""

        try:
            kind = await self.run_service.resolve_kind(run_id)
            if kind is RunKind.CODE_AUDIT:
                aggregate = await self.audit_service.get_by_run_authorized(
                    run_id,
                    principal=self.principal,
                    authorizer=self.audit_authorizer,
                )
                run = aggregate.run
                return RunReadAuthorizationSnapshot(
                    run_id=run.id,
                    run_kind=run.kind,
                    engagement_id=run.engagement_id,
                    node_id=run.node_id,
                    principal=self.principal,
                    audit_id=aggregate.audit.value.id,
                    audit_project_id=aggregate.project.value.id,
                )
            run = await self.run_service.get_run(run_id)
            return RunReadAuthorizationSnapshot(
                run_id=run.id,
                run_kind=run.kind,
                engagement_id=run.engagement_id,
                node_id=run.node_id,
                principal=self.principal,
            )
        except (EntityNotFoundError, ResourceNotAccessibleError):
            raise resource_not_accessible() from None

    async def revalidate_stream_snapshot(
        self,
        request: Request,
        frozen: RunReadAuthorizationSnapshot,
    ) -> None:
        """Reauthenticate and reauthorize the exact frozen owner before one batch."""

        principal = await authorize_local_operator(request)
        current = await RunReadAuthorizer(
            run_service=self.run_service,
            audit_service=self.audit_service,
            principal=principal,
            audit_authorizer=self.audit_authorizer,
        ).require_stream_snapshot(frozen.run_id)
        if current != frozen:
            raise resource_not_accessible()


def require_run_read_binding(expected_run_id: str, actual_run_id: str | None) -> None:
    """Revalidate immutable child ownership after the authorized full read."""

    if actual_run_id is None or not secrets.compare_digest(expected_run_id, actual_run_id):
        raise resource_not_accessible()


async def load_authorized_child[ReadT](awaitable: Awaitable[ReadT]) -> ReadT:
    """Keep a post-authorization disappearance opaque to the caller."""

    try:
        return await awaitable
    except (EntityNotFoundError, ResourceNotAccessibleError):
        raise resource_not_accessible() from None


def get_run_read_authorizer(
    run_service: RunServiceDependency,
    audit_service: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    audit_authorizer: AuditObjectAuthorizerDependency,
) -> RunReadAuthorizer:
    return RunReadAuthorizer(
        run_service=run_service,
        audit_service=audit_service,
        principal=principal,
        audit_authorizer=audit_authorizer,
    )


RunReadAuthorizerDependency = Annotated[
    RunReadAuthorizer,
    Depends(get_run_read_authorizer),
]


def websocket_run_read_authorizer(websocket: WebSocket) -> RunReadAuthorizer:
    """Build the same typed read authorizer after WebSocket policy authentication."""

    control_plane = websocket.app.state.control_plane
    return RunReadAuthorizer(
        run_service=control_plane.run_service,
        audit_service=control_plane.audit_service,
        principal=get_authenticated_local_principal(websocket),
        audit_authorizer=websocket.app.state.local_object_authorizer,
    )


async def authorize_run_read(
    run_id: str,
    authorizer: RunReadAuthorizerDependency,
) -> Run:
    return await authorizer.require(run_id)


AuthorizedRunReadDependency = Annotated[Run, Depends(authorize_run_read)]


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
    return RunnerAuthorization(
        node_id=node_id,
        token=token,
        principal=principal,
        protocol_capabilities=credential.protocol_capabilities,
    )


async def authorize_audit_preflight_runner(
    service: AuditPreflightRunnerServiceDependency,
    node_id: Annotated[str, Header(alias="X-RiftX-Node-ID")],
    runner_instance_id: Annotated[
        str,
        Header(alias="X-RiftX-Runner-Instance-ID", min_length=1, max_length=64),
    ],
    runner_epoch: Annotated[int, Header(alias="X-RiftX-Runner-Epoch", ge=1)],
    authorization: Annotated[str | None, Header()] = None,
) -> RunnerAuthorization:
    """Authenticate the dedicated Preflight credential and immutable capability."""

    token = bearer_token(authorization)
    principal = RunnerPrincipal(instance_id=runner_instance_id, epoch=runner_epoch)
    credential = await service.authenticate(
        node_id,
        token,
        declared_principal=principal,
    )
    return RunnerAuthorization(
        node_id=node_id,
        token=token,
        principal=principal,
        protocol_capabilities=credential.protocol_capabilities,
    )


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
    return RunnerAuthorization(
        node_id=node_id,
        token=token,
        principal=principal,
        protocol_capabilities=credential.protocol_capabilities,
    )


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
AuditPreflightRunnerDependency = Annotated[
    RunnerAuthorization,
    Depends(authorize_audit_preflight_runner),
]
RunnerNodeDependency = Annotated[
    RunnerAuthorization,
    Depends(authorize_runner_node),
]
