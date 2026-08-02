"""Structured Run report generation and retrieval endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from riftx.domain import ReportFormat

from ..dependencies import ReportServiceDependency
from ..schemas import (
    ErrorResponse,
    GenerateReportsRequest,
    ReportListResponse,
    ReportResponse,
)

router = APIRouter(tags=["reports"])


@router.post(
    "/runs/{run_id}/reports",
    response_model=ReportListResponse,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def generate_reports(
    run_id: str,
    request: GenerateReportsRequest,
    service: ReportServiceDependency,
) -> ReportListResponse:
    reports = await service.generate(run_id, request.to_command())
    return ReportListResponse(
        items=[ReportResponse.from_domain(item) for item in reports],
        limit=len(reports),
        offset=0,
    )


@router.get(
    "/runs/{run_id}/reports",
    response_model=ReportListResponse,
    responses={404: {"model": ErrorResponse}},
)
async def list_reports(
    run_id: str,
    service: ReportServiceDependency,
    report_format: Annotated[ReportFormat | None, Query(alias="format")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReportListResponse:
    reports = await service.list(
        run_id,
        format=report_format,
        limit=limit,
        offset=offset,
    )
    return ReportListResponse(
        items=[ReportResponse.from_domain(item) for item in reports],
        limit=limit,
        offset=offset,
    )


@router.get(
    "/reports/{report_id}",
    response_model=ReportResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_report(
    report_id: str,
    service: ReportServiceDependency,
) -> ReportResponse:
    return ReportResponse.from_domain(await service.get(report_id))
