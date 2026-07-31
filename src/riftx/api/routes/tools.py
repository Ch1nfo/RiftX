"""Node-local tool registry endpoints."""

from fastapi import APIRouter

from ..dependencies import AdminDependency, ToolServiceDependency
from ..schemas import (
    ErrorResponse,
    ToolRegistryResponse,
    ToolRegistrySummaryResponse,
    ToolUpdateRequest,
)

router = APIRouter(prefix="/nodes/{node_id}", tags=["tools"])


@router.get(
    "/tools",
    response_model=ToolRegistrySummaryResponse,
    responses={404: {"model": ErrorResponse}},
)
async def list_tools(
    node_id: str,
    service: ToolServiceDependency,
) -> ToolRegistrySummaryResponse:
    return ToolRegistrySummaryResponse.from_view(await service.list_tools(node_id))


@router.get(
    "/tools/admin",
    response_model=ToolRegistryResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def list_tools_for_admin(
    node_id: str,
    service: ToolServiceDependency,
    _authorized: AdminDependency,
) -> ToolRegistryResponse:
    return ToolRegistryResponse.from_view(await service.list_tools(node_id))


@router.post(
    "/refresh-tools",
    response_model=ToolRegistrySummaryResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def refresh_tools(
    node_id: str,
    service: ToolServiceDependency,
    _authorized: AdminDependency,
) -> ToolRegistrySummaryResponse:
    return ToolRegistrySummaryResponse.from_view(await service.refresh_tools(node_id))


@router.put(
    "/tools/{tool_id}",
    response_model=ToolRegistrySummaryResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def update_tool(
    node_id: str,
    tool_id: str,
    request: ToolUpdateRequest,
    service: ToolServiceDependency,
    _authorized: AdminDependency,
) -> ToolRegistrySummaryResponse:
    return ToolRegistrySummaryResponse.from_view(
        await service.update_tool(node_id, tool_id, request.to_definition())
    )
