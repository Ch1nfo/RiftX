"""Authenticated, read-only Observer Projector endpoint."""

from typing import Annotated, Any

from fastapi import APIRouter, Query

from riftx.application.services.runs import require_interactive_run_operation

from ..dependencies import (
    AuthorizedRunReadDependency,
    LocalPrincipalDependency,
    ObserverProjectorDependency,
)
from ..schemas import ErrorResponse, ObserverProjection, ObserverProjectionQuery

router = APIRouter(prefix="/runs/{run_id}/projection", tags=["observer"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "",
    response_model=ObserverProjection,
    responses=_ERROR_RESPONSES,
)
async def get_observer_projection(
    run_id: str,
    service: ObserverProjectorDependency,
    principal: LocalPrincipalDependency,
    authorized_run: AuthorizedRunReadDependency,
    query: Annotated[ObserverProjectionQuery, Query()],
) -> ObserverProjection:
    require_interactive_run_operation(authorized_run)
    return await service.project(
        run_id,
        principal=principal,
        graph_limit=query.graph_limit,
        timeline_limit=query.timeline_limit,
    )
