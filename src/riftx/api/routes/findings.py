"""Finding query endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query

from riftx.domain import FindingSeverity, FindingStatus

from ..dependencies import FindingServiceDependency
from ..schemas import ErrorResponse, FindingListResponse

router = APIRouter(prefix="/runs/{run_id}/findings", tags=["findings"])


@router.get(
    "",
    response_model=FindingListResponse,
    responses={404: {"model": ErrorResponse}},
)
async def list_findings(
    run_id: str,
    service: FindingServiceDependency,
    severity: FindingSeverity | None = None,
    finding_status: Annotated[FindingStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FindingListResponse:
    findings = await service.list_findings(
        run_id,
        severity=severity,
        status=finding_status,
        limit=limit,
        offset=offset,
    )
    return FindingListResponse(items=findings, limit=limit, offset=offset)
