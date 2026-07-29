"""Runner registration and execution-node management endpoints."""

from typing import Annotated

from fastapi import APIRouter, Header, Query

from riftx.application.services import NodeHeartbeat, NodeRegistration
from riftx.domain import NodeStatus

from ..auth import bearer_token
from ..dependencies import NodeServiceDependency, RunnerControlServiceDependency
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
    status: Annotated[NodeStatus | None, Query()] = None,
) -> NodeListResponse:
    nodes = await service.list(status=status)
    return NodeListResponse(items=[NodeResponse.from_domain(node) for node in nodes])


@router.get(
    "/{node_id}",
    response_model=NodeResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_node(node_id: str, service: NodeServiceDependency) -> NodeResponse:
    return NodeResponse.from_domain(await service.get(node_id))
