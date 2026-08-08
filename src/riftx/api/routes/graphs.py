"""Authenticated, read-only Run Graph endpoint."""

from typing import Annotated

from fastapi import APIRouter, Query

from riftx.application.services.runs import require_interactive_run_operation

from ..dependencies import (
    AuthorizedRunReadDependency,
    GraphServiceDependency,
    LocalPrincipalDependency,
)
from ..schemas import ErrorResponse, GraphViewPage, GraphViewQuery

router = APIRouter(prefix="/runs/{run_id}/graph", tags=["graphs"])

_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "",
    response_model=GraphViewPage,
    responses=_ERROR_RESPONSES,
)
async def get_run_graph(
    run_id: str,
    service: GraphServiceDependency,
    principal: LocalPrincipalDependency,
    authorized_run: AuthorizedRunReadDependency,
    query: Annotated[GraphViewQuery, Query()],
) -> GraphViewPage:
    require_interactive_run_operation(authorized_run)
    return await service.get_view(
        run_id,
        principal=principal,
        view=query.view,
        node_type=query.node_type,
        edge_type=query.edge_type,
        focus=query.focus,
        search=query.search,
        limit=query.limit,
        cursor=query.cursor,
    )
