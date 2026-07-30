"""Runner registration and execution-node management endpoints."""

from typing import Annotated

from fastapi import APIRouter, Header, Query

from riftx.application.services import NodeHeartbeat, NodeRegistration
from riftx.domain import NodeStatus

from ..auth import bearer_token
from ..dependencies import (
    ExecutionServiceDependency,
    NodeServiceDependency,
    RunnerControlServiceDependency,
    ToolServiceDependency,
)
from ..schemas import (
    ErrorResponse,
    HeartbeatNodeRequest,
    NodeListResponse,
    NodeRegistrationResponse,
    NodeResponse,
    RegisterNodeRequest,
)

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.post(
    "/register",
    response_model=NodeRegistrationResponse,
    responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def register_node(
    payload: RegisterNodeRequest,
    service: RunnerControlServiceDependency,
    authorization: Annotated[str | None, Header()] = None,
) -> NodeRegistrationResponse:
    result = await service.register(
        NodeRegistration(
            node_id=payload.node_id,
            name=payload.name,
            platform=payload.platform,
            architecture=payload.architecture,
            runner_version=payload.runner_version,
            capabilities=tuple(payload.capabilities),
            labels=payload.labels,
        ),
        bootstrap_token=bearer_token(authorization),
    )
    return NodeRegistrationResponse(
        node=NodeResponse.from_domain(result.node),
        created=result.created,
        runner_token=result.token,
    )


@router.post(
    "/{node_id}/heartbeat",
    response_model=NodeResponse,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def heartbeat_node(
    node_id: str,
    payload: HeartbeatNodeRequest,
    service: RunnerControlServiceDependency,
    authorization: Annotated[str | None, Header()] = None,
) -> NodeResponse:
    node = await service.heartbeat(
        node_id,
        bearer_token(authorization),
        NodeHeartbeat(
            status=payload.status,
            capabilities=(
                tuple(payload.capabilities) if payload.capabilities is not None else None
            ),
            labels=payload.labels,
            runner_version=payload.runner_version,
        ),
    )
    return NodeResponse.from_domain(node)


@router.post(
    "/{node_id}/disconnect",
    response_model=NodeResponse,
    responses={404: {"model": ErrorResponse}},
)
async def disconnect_node(
    node_id: str,
    service: NodeServiceDependency,
) -> NodeResponse:
    return NodeResponse.from_domain(await service.disconnect(node_id))


@router.get("", response_model=NodeListResponse)
async def list_nodes(
    service: NodeServiceDependency,
    execution_service: ExecutionServiceDependency,
    tool_service: ToolServiceDependency,
    status: Annotated[NodeStatus | None, Query()] = None,
) -> NodeListResponse:
    nodes = await service.list(status=status)
    active = await execution_service.list_active()
    local_tools = await tool_service.list_tools(tool_service.node_id)
    return NodeListResponse(
        items=[
            NodeResponse.from_domain(
                node,
                active_executions=[item for item in active if item.node_id == node.id],
                tool_count=(len(local_tools.tools) if node.id == tool_service.node_id else None),
            )
            for node in nodes
        ]
    )


@router.get(
    "/{node_id}",
    response_model=NodeResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_node(
    node_id: str,
    service: NodeServiceDependency,
    execution_service: ExecutionServiceDependency,
    tool_service: ToolServiceDependency,
) -> NodeResponse:
    node = await service.get(node_id)
    active = [item for item in await execution_service.list_active() if item.node_id == node.id]
    tool_count = None
    if node.id == tool_service.node_id:
        tool_count = len((await tool_service.list_tools(node.id)).tools)
    return NodeResponse.from_domain(
        node,
        active_executions=active,
        tool_count=tool_count,
    )
