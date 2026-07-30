"""Manual long-term Memory management and deterministic retrieval endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from riftx.memory import MemoryRetrievalScope, MemoryScopeType

from ..dependencies import MemoryServiceDependency
from ..schemas import (
    CreateMemoryRequest,
    ErrorResponse,
    MemoryListResponse,
    MemoryResponse,
    PinMemoryRequest,
    UpdateMemoryRequest,
)

router = APIRouter(prefix="/memories", tags=["memories"])

_ERROR_RESPONSES = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=MemoryResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def create_memory(
    request: CreateMemoryRequest,
    service: MemoryServiceDependency,
) -> MemoryResponse:
    return MemoryResponse.from_domain(await service.create(request.to_command()))


@router.get("", response_model=MemoryListResponse, responses=_ERROR_RESPONSES)
async def list_memories(
    service: MemoryServiceDependency,
    scope_type: MemoryScopeType | None = None,
    scope_id: str | None = None,
    include_inactive: bool = False,
) -> MemoryListResponse:
    memories = await service.list_scope(
        scope_type=scope_type,
        scope_id=scope_id,
        include_inactive=include_inactive,
    )
    return MemoryListResponse(items=[MemoryResponse.from_domain(item) for item in memories])


@router.get("/search", response_model=MemoryListResponse, responses=_ERROR_RESPONSES)
async def search_memories(
    service: MemoryServiceDependency,
    query: Annotated[str, Query(alias="q", min_length=1)],
    user_id: str | None = None,
    node_id: str | None = None,
    workspace_id: str | None = None,
    run_id: str | None = None,
    engagement_id: str | None = None,
    asset_id: Annotated[list[str] | None, Query()] = None,
    tool_id: Annotated[list[str] | None, Query()] = None,
    skill_id: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> MemoryListResponse:
    memories = await service.retrieve(
        query,
        scope=MemoryRetrievalScope(
            user_id=user_id,
            node_id=node_id,
            workspace_id=workspace_id,
            run_id=run_id,
            engagement_id=engagement_id,
            asset_ids=asset_id or [],
            tool_ids=tool_id or [],
            skill_ids=skill_id or [],
        ),
        limit=limit,
    )
    return MemoryListResponse(items=[MemoryResponse.from_domain(item) for item in memories])


@router.get("/{memory_id}", response_model=MemoryResponse, responses=_ERROR_RESPONSES)
async def get_memory(
    memory_id: str,
    service: MemoryServiceDependency,
) -> MemoryResponse:
    return MemoryResponse.from_domain(await service.get(memory_id))


@router.patch("/{memory_id}", response_model=MemoryResponse, responses=_ERROR_RESPONSES)
async def update_memory(
    memory_id: str,
    request: UpdateMemoryRequest,
    service: MemoryServiceDependency,
) -> MemoryResponse:
    return MemoryResponse.from_domain(await service.update(memory_id, request.changes()))


@router.delete("/{memory_id}", response_model=MemoryResponse, responses=_ERROR_RESPONSES)
async def delete_memory(
    memory_id: str,
    service: MemoryServiceDependency,
) -> MemoryResponse:
    return MemoryResponse.from_domain(await service.delete(memory_id))


@router.post("/{memory_id}/pin", response_model=MemoryResponse, responses=_ERROR_RESPONSES)
async def pin_memory(
    memory_id: str,
    request: PinMemoryRequest,
    service: MemoryServiceDependency,
) -> MemoryResponse:
    return MemoryResponse.from_domain(await service.pin(memory_id, pinned=request.pinned))
