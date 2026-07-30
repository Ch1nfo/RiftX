"""Run lifecycle and control endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from riftx.domain import RunStatus

from ..dependencies import RunServiceDependency, ToolServiceDependency
from ..schemas import (
    CreateRunRequest,
    ErrorResponse,
    RunActionResponse,
    RunListResponse,
    RunMessageRequest,
    RunResponse,
)

router = APIRouter(prefix="/runs", tags=["runs"])

_ERROR_RESPONSES = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def create_run(
    request: CreateRunRequest,
    run_service: RunServiceDependency,
    tool_service: ToolServiceDependency,
) -> RunResponse:
    run = await run_service.create_run(request.to_command(default_node_id=tool_service.node_id))
    return RunResponse.from_domain(run)


@router.get("", response_model=RunListResponse, responses=_ERROR_RESPONSES)
async def list_runs(
    run_service: RunServiceDependency,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListResponse:
    runs = await run_service.list_runs(status=run_status, limit=limit, offset=offset)
    return RunListResponse(
        items=[RunResponse.from_domain(run) for run in runs],
        limit=limit,
        offset=offset,
    )


@router.get("/{run_id}", response_model=RunResponse, responses=_ERROR_RESPONSES)
async def get_run(run_id: str, run_service: RunServiceDependency) -> RunResponse:
    return RunResponse.from_domain(await run_service.get_run(run_id))


@router.post(
    "/{run_id}/pause",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def pause_run(run_id: str, run_service: RunServiceDependency) -> RunActionResponse:
    return RunActionResponse(run=RunResponse.from_domain(await run_service.pause(run_id)))


@router.post(
    "/{run_id}/resume",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def resume_run(run_id: str, run_service: RunServiceDependency) -> RunActionResponse:
    return RunActionResponse(run=RunResponse.from_domain(await run_service.resume(run_id)))


@router.post(
    "/{run_id}/cancel",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def cancel_run(run_id: str, run_service: RunServiceDependency) -> RunActionResponse:
    return RunActionResponse(run=RunResponse.from_domain(await run_service.cancel(run_id)))


@router.post(
    "/{run_id}/cancel-current-execution",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def cancel_current_execution(
    run_id: str,
    run_service: RunServiceDependency,
) -> RunActionResponse:
    return RunActionResponse(
        run=RunResponse.from_domain(await run_service.cancel_current_execution(run_id))
    )


@router.post(
    "/{run_id}/message",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def append_message(
    run_id: str,
    request: RunMessageRequest,
    run_service: RunServiceDependency,
) -> RunActionResponse:
    return RunActionResponse(
        run=RunResponse.from_domain(await run_service.append_user_message(run_id, request.message))
    )
