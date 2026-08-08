"""Authorized, read-only Run Action endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query

from riftx.application.services.runs import require_interactive_run_operation

from ..dependencies import (
    ActionServiceDependency,
    AuthorizedRunReadDependency,
    LocalPrincipalDependency,
)
from ..schemas import ErrorResponse, RunActionListView, RunActionView

router = APIRouter(prefix="/runs/{run_id}/actions", tags=["actions"])

_DEFAULT_SORT = "created_at_desc"
_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "",
    response_model=RunActionListView,
    responses=_ERROR_RESPONSES,
)
async def list_run_actions(
    run_id: str,
    service: ActionServiceDependency,
    principal: LocalPrincipalDependency,
    authorized_run: AuthorizedRunReadDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    sort: Annotated[
        str,
        Query(
            description="Action ordering; only created_at_desc is supported.",
            json_schema_extra={"enum": [_DEFAULT_SORT]},
        ),
    ] = _DEFAULT_SORT,
) -> RunActionListView:
    require_interactive_run_operation(authorized_run)
    return await service.list(
        run_id,
        principal=principal,
        limit=limit,
        cursor=cursor,
        sort=sort,
    )


@router.get(
    "/{action_id}",
    response_model=RunActionView,
    responses=_ERROR_RESPONSES,
)
async def get_run_action(
    run_id: str,
    action_id: str,
    service: ActionServiceDependency,
    principal: LocalPrincipalDependency,
    authorized_run: AuthorizedRunReadDependency,
) -> RunActionView:
    require_interactive_run_operation(authorized_run)
    return await service.get(
        run_id,
        action_id,
        principal=principal,
    )
