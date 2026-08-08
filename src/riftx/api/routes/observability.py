"""Run-scoped runtime metric endpoints."""

from fastapi import APIRouter

from riftx.application.services.runs import require_interactive_run_operation
from riftx.observability import RuntimeMetricsSnapshot

from ..dependencies import (
    AuthorizedRunReadDependency,
    RuntimeObservabilityServiceDependency,
)
from ..schemas import ErrorResponse

router = APIRouter(tags=["observability"])

_ERROR_RESPONSES = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "/runs/{run_id}/metrics",
    response_model=RuntimeMetricsSnapshot,
    responses=_ERROR_RESPONSES,
)
async def get_run_metrics(
    run_id: str,
    service: RuntimeObservabilityServiceDependency,
    authorized_run: AuthorizedRunReadDependency,
) -> RuntimeMetricsSnapshot:
    require_interactive_run_operation(authorized_run)
    return await service.snapshot(run_id)
