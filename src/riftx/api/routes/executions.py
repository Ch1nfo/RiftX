"""Execution inspection, cancellation, and cursor-based output endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from ..dependencies import ExecutionServiceDependency
from ..schemas import (
    ErrorResponse,
    ExecutionListResponse,
    ExecutionOutputResponse,
    ExecutionResponse,
    ExecutionWaitResponse,
)

router = APIRouter(tags=["executions"])
_ERROR_RESPONSES = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "/executions/{execution_id}",
    response_model=ExecutionResponse,
    responses=_ERROR_RESPONSES,
)
async def get_execution(
    execution_id: str,
    service: ExecutionServiceDependency,
) -> ExecutionResponse:
    return ExecutionResponse.from_domain(await service.get(execution_id))


@router.post(
    "/executions/{execution_id}/wait",
    response_model=ExecutionWaitResponse,
    responses=_ERROR_RESPONSES,
)
async def wait_execution(
    execution_id: str,
    service: ExecutionServiceDependency,
    timeout_seconds: Annotated[float, Query(gt=0, le=120)] = 30.0,
    stdout_cursor: Annotated[int, Query(ge=0)] = 0,
    stderr_cursor: Annotated[int, Query(ge=0)] = 0,
    max_bytes: Annotated[int, Query(ge=1, le=1024 * 1024)] = 64 * 1024,
    next_poll_after_seconds: Annotated[int, Query(ge=1, le=3600)] = 10,
) -> ExecutionWaitResponse:
    return ExecutionWaitResponse.from_domain(
        await service.wait(
            execution_id,
            timeout_seconds=timeout_seconds,
            stdout_cursor=stdout_cursor,
            stderr_cursor=stderr_cursor,
            max_bytes=max_bytes,
            next_poll_after_seconds=next_poll_after_seconds,
        )
    )


@router.post(
    "/executions/{execution_id}/cancel",
    response_model=ExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def cancel_execution(
    execution_id: str,
    service: ExecutionServiceDependency,
) -> ExecutionResponse:
    return ExecutionResponse.from_domain(await service.cancel(execution_id))


@router.get(
    "/executions/{execution_id}/output",
    response_model=ExecutionOutputResponse,
    responses=_ERROR_RESPONSES,
)
async def get_execution_output(
    execution_id: str,
    service: ExecutionServiceDependency,
    stdout_cursor: Annotated[int, Query(ge=0)] = 0,
    stderr_cursor: Annotated[int, Query(ge=0)] = 0,
    max_bytes: Annotated[int, Query(ge=1, le=1024 * 1024)] = 64 * 1024,
) -> ExecutionOutputResponse:
    return ExecutionOutputResponse.from_domain(
        await service.output(
            execution_id,
            stdout_cursor=stdout_cursor,
            stderr_cursor=stderr_cursor,
            max_bytes=max_bytes,
        )
    )


@router.get(
    "/runs/{run_id}/executions",
    response_model=ExecutionListResponse,
    responses=_ERROR_RESPONSES,
)
async def list_run_executions(
    run_id: str,
    service: ExecutionServiceDependency,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExecutionListResponse:
    executions = await service.list(run_id, limit=limit, offset=offset)
    return ExecutionListResponse(
        items=[ExecutionResponse.from_domain(item) for item in executions],
        limit=limit,
        offset=offset,
    )
