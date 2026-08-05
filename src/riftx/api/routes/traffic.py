"""Authenticated, read-only Target HTTP metadata endpoints."""

from typing import Annotated

from fastapi import APIRouter, Path, Query
from pydantic import AfterValidator

from riftx.application.services.runs import require_general_run_operation

from ..dependencies import (
    AuthorizedRunReadDependency,
    LocalPrincipalDependency,
    TrafficMetadataServiceDependency,
)
from ..schemas import (
    ErrorResponse,
    TrafficExchangeDetail,
    TrafficExchangeListQuery,
    TrafficExchangePage,
)
from ..schemas.traffic import validate_exchange_id

router = APIRouter(prefix="/runs/{run_id}/target-http/exchanges", tags=["target-http"])

_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

TrafficExchangeId = Annotated[
    str,
    Path(min_length=1, max_length=256),
    AfterValidator(validate_exchange_id),
]


@router.get(
    "",
    response_model=TrafficExchangePage,
    responses=_ERROR_RESPONSES,
)
async def list_target_http_exchanges(
    run_id: str,
    service: TrafficMetadataServiceDependency,
    principal: LocalPrincipalDependency,
    authorized_run: AuthorizedRunReadDependency,
    query: Annotated[TrafficExchangeListQuery, Query()],
) -> TrafficExchangePage:
    require_general_run_operation(authorized_run)
    return await service.list(
        run_id,
        principal=principal,
        method=query.method,
        status_class=query.status_class,
        limit=query.limit,
        cursor=query.cursor,
    )


@router.get(
    "/{exchange_id}",
    response_model=TrafficExchangeDetail,
    responses=_ERROR_RESPONSES,
)
async def get_target_http_exchange(
    run_id: str,
    exchange_id: TrafficExchangeId,
    service: TrafficMetadataServiceDependency,
    principal: LocalPrincipalDependency,
    authorized_run: AuthorizedRunReadDependency,
) -> TrafficExchangeDetail:
    require_general_run_operation(authorized_run)
    return await service.get(
        run_id,
        exchange_id,
        principal=principal,
    )
