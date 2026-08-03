"""Manual long-term Memory management and deterministic retrieval endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    ResourceNotAccessibleError,
    resource_not_accessible,
)
from riftx.application.services.runs import (
    RunApplicationService,
    require_general_run_operation,
)
from riftx.memory import MemoryRecord, MemoryRetrievalScope, MemoryScopeType

from ..dependencies import (
    MemoryServiceDependency,
    RunReadAuthorizerDependency,
    RunServiceDependency,
    load_authorized_child,
)
from ..schemas import (
    CreateMemoryRequest,
    ErrorResponse,
    MemoryListResponse,
    MemoryResponse,
    PinMemoryRequest,
    UpdateMemoryRequest,
)

router = APIRouter(prefix="/memories", tags=["memories"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
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
    runs: RunServiceDependency,
) -> MemoryResponse:
    command = request.to_command()
    await _require_general_memory_scope(
        runs,
        scope_type=command.scope_type,
        scope_id=command.scope_id,
    )
    if command.supersedes is not None:
        current = await service.get(command.supersedes)
        await _require_general_memory_scope(
            runs,
            scope_type=current.scope_type,
            scope_id=current.scope_id,
        )
    return MemoryResponse.from_domain(await service.create(command))


@router.get("", response_model=MemoryListResponse, responses=_ERROR_RESPONSES)
async def list_memories(
    service: MemoryServiceDependency,
    authorizer: RunReadAuthorizerDependency,
    scope_type: MemoryScopeType | None = None,
    scope_id: str | None = None,
    include_inactive: bool = False,
) -> MemoryListResponse:
    if scope_type is MemoryScopeType.RUN and scope_id is not None:
        authorized_run = await authorizer.require(scope_id)
        require_general_run_operation(authorized_run)
    memories = await service.list_scope(
        scope_type=scope_type,
        scope_id=scope_id,
        include_inactive=include_inactive,
    )
    if scope_type is None:
        memories = await _filter_authorized_memories(memories, authorizer)
    return MemoryListResponse(items=[MemoryResponse.from_domain(item) for item in memories])


@router.get("/search", response_model=MemoryListResponse, responses=_ERROR_RESPONSES)
async def search_memories(
    service: MemoryServiceDependency,
    authorizer: RunReadAuthorizerDependency,
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
    if run_id is not None:
        authorized_run = await authorizer.require(run_id)
        require_general_run_operation(authorized_run)
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
    authorizer: RunReadAuthorizerDependency,
) -> MemoryResponse:
    scope = await service.resolve_scope(memory_id)
    if scope.scope_type is MemoryScopeType.RUN:
        authorized_run = await authorizer.require(scope.scope_id)
        require_general_run_operation(authorized_run)
    memory = await load_authorized_child(service.get(memory_id))
    if memory.scope != scope:
        raise resource_not_accessible()
    return MemoryResponse.from_domain(memory)


@router.patch("/{memory_id}", response_model=MemoryResponse, responses=_ERROR_RESPONSES)
async def update_memory(
    memory_id: str,
    request: UpdateMemoryRequest,
    service: MemoryServiceDependency,
    runs: RunServiceDependency,
) -> MemoryResponse:
    current = await service.get(memory_id)
    await _require_general_memory_scope(
        runs,
        scope_type=current.scope_type,
        scope_id=current.scope_id,
    )
    changes = request.changes()
    await _require_general_memory_scope(
        runs,
        scope_type=changes.get("scope_type", current.scope_type),
        scope_id=changes.get("scope_id", current.scope_id),
    )
    return MemoryResponse.from_domain(await service.update(memory_id, changes))


@router.delete("/{memory_id}", response_model=MemoryResponse, responses=_ERROR_RESPONSES)
async def delete_memory(
    memory_id: str,
    service: MemoryServiceDependency,
    runs: RunServiceDependency,
) -> MemoryResponse:
    current = await service.get(memory_id)
    await _require_general_memory_scope(
        runs,
        scope_type=current.scope_type,
        scope_id=current.scope_id,
    )
    return MemoryResponse.from_domain(await service.delete(memory_id))


@router.post("/{memory_id}/pin", response_model=MemoryResponse, responses=_ERROR_RESPONSES)
async def pin_memory(
    memory_id: str,
    request: PinMemoryRequest,
    service: MemoryServiceDependency,
    runs: RunServiceDependency,
) -> MemoryResponse:
    current = await service.get(memory_id)
    await _require_general_memory_scope(
        runs,
        scope_type=current.scope_type,
        scope_id=current.scope_id,
    )
    return MemoryResponse.from_domain(await service.pin(memory_id, pinned=request.pinned))


async def _require_general_memory_scope(
    runs: RunApplicationService,
    *,
    scope_type: object,
    scope_id: object,
) -> None:
    if scope_type is not MemoryScopeType.RUN:
        return
    if not isinstance(scope_id, str):
        return
    require_general_run_operation(await runs.get_run(scope_id))


async def _filter_authorized_memories(
    memories: list[MemoryRecord],
    authorizer: RunReadAuthorizerDependency,
) -> list[MemoryRecord]:
    visible: list[MemoryRecord] = []
    for memory in memories:
        if memory.scope_type is MemoryScopeType.RUN:
            try:
                authorized_run = await authorizer.require(memory.scope_id)
                require_general_run_operation(authorized_run)
            except (EntityNotFoundError, ResourceNotAccessibleError):
                continue
            except ApplicationConflictError as exc:
                if exc.code != "run_kind_operation_unsupported":
                    raise
                continue
        visible.append(memory)
    return visible
