"""Execution inspection, cancellation, and cursor-based output endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from riftx.application.services.runs import require_interactive_run_operation
from riftx.domain import Execution, RunKind

from ..dependencies import (
    AuthorizedRunReadDependency,
    ExecutionServiceDependency,
    RunReadAuthorizerDependency,
    RunServiceDependency,
    load_authorized_child,
    require_run_read_binding,
)
from ..schemas import (
    CodeAuditExecutionResponse,
    ErrorResponse,
    ExecutionListResponse,
    ExecutionOutputResponse,
    ExecutionReadResponse,
    ExecutionResponse,
    ExecutionWaitResponse,
)

router = APIRouter(tags=["executions"])
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "/executions/{execution_id}",
    response_model=ExecutionReadResponse,
    responses=_ERROR_RESPONSES,
)
async def get_execution(
    execution_id: str,
    service: ExecutionServiceDependency,
    authorizer: RunReadAuthorizerDependency,
) -> ExecutionReadResponse:
    run_id = await service.resolve_run_id(execution_id)
    authorized_run = await authorizer.require(run_id)
    execution = await load_authorized_child(service.get(execution_id))
    require_run_read_binding(run_id, execution.run_id)
    return _execution_response(execution, kind=authorized_run.kind)


@router.post(
    "/executions/{execution_id}/wait",
    response_model=ExecutionWaitResponse,
    responses=_ERROR_RESPONSES,
)
async def wait_execution(
    execution_id: str,
    service: ExecutionServiceDependency,
    authorizer: RunReadAuthorizerDependency,
    timeout_seconds: Annotated[float, Query(gt=0, le=120)] = 30.0,
    stdout_cursor: Annotated[int, Query(ge=0)] = 0,
    stderr_cursor: Annotated[int, Query(ge=0)] = 0,
    max_bytes: Annotated[int, Query(ge=1, le=1024 * 1024)] = 64 * 1024,
    next_poll_after_seconds: Annotated[int, Query(ge=1, le=3600)] = 10,
) -> ExecutionWaitResponse:
    run_id = await service.resolve_run_id(execution_id)
    await authorizer.require(run_id)
    return ExecutionWaitResponse.from_domain(
        await load_authorized_child(
            service.wait(
                execution_id,
                timeout_seconds=timeout_seconds,
                stdout_cursor=stdout_cursor,
                stderr_cursor=stderr_cursor,
                max_bytes=max_bytes,
                next_poll_after_seconds=next_poll_after_seconds,
                expected_run_id=run_id,
            )
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
    runs: RunServiceDependency,
) -> ExecutionResponse:
    current = await service.get(execution_id)
    require_interactive_run_operation(await runs.get_run(current.run_id))
    return ExecutionResponse.from_domain(await service.cancel(execution_id))


@router.get(
    "/executions/{execution_id}/output",
    response_model=ExecutionOutputResponse,
    responses=_ERROR_RESPONSES,
)
async def get_execution_output(
    execution_id: str,
    service: ExecutionServiceDependency,
    authorizer: RunReadAuthorizerDependency,
    stdout_cursor: Annotated[int, Query(ge=0)] = 0,
    stderr_cursor: Annotated[int, Query(ge=0)] = 0,
    max_bytes: Annotated[int, Query(ge=1, le=1024 * 1024)] = 64 * 1024,
) -> ExecutionOutputResponse:
    run_id = await service.resolve_run_id(execution_id)
    await authorizer.require(run_id)
    return ExecutionOutputResponse.from_domain(
        await load_authorized_child(
            service.output(
                execution_id,
                stdout_cursor=stdout_cursor,
                stderr_cursor=stderr_cursor,
                max_bytes=max_bytes,
                expected_run_id=run_id,
            )
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
    authorized_run: AuthorizedRunReadDependency,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExecutionListResponse:
    executions = await load_authorized_child(
        service.list(run_id, limit=limit, offset=offset)
    )
    for execution in executions:
        require_run_read_binding(run_id, execution.run_id)
    return ExecutionListResponse(
        items=[
            _execution_response(item, kind=authorized_run.kind)
            for item in executions
        ],
        limit=limit,
        offset=offset,
    )


def _execution_response(
    execution: Execution,
    *,
    kind: RunKind,
) -> ExecutionReadResponse:
    if kind is RunKind.CODE_AUDIT:
        return CodeAuditExecutionResponse.from_domain(execution)
    return ExecutionResponse.from_domain(execution)
