"""Structured Run report generation and retrieval endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from riftx.application.services.runs import require_general_run_operation
from riftx.domain import ReportFormat

from ..dependencies import (
    AuthorizedRunReadDependency,
    ReportServiceDependency,
    RunReadAuthorizerDependency,
    RunServiceDependency,
    load_authorized_child,
    require_run_read_binding,
)
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
    runs: RunServiceDependency,
) -> ReportListResponse:
    require_general_run_operation(await runs.get_run(run_id))
    reports = await service.generate(run_id, request.to_command())
    return ReportListResponse(
        items=[ReportResponse.from_domain(item) for item in reports],
        limit=len(reports),
        offset=0,
    )


@router.get(
    "/runs/{run_id}/reports",
    response_model=ReportListResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def list_reports(
    run_id: str,
    service: ReportServiceDependency,
    authorized_run: AuthorizedRunReadDependency,
    report_format: Annotated[ReportFormat | None, Query(alias="format")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReportListResponse:
    require_general_run_operation(authorized_run)
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
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def get_report(
    report_id: str,
    service: ReportServiceDependency,
    authorizer: RunReadAuthorizerDependency,
) -> ReportResponse:
    run_id = await service.resolve_run_id(report_id)
    authorized_run = await authorizer.require(run_id)
    require_general_run_operation(authorized_run)
    report = await load_authorized_child(service.get(report_id))
    require_run_read_binding(run_id, report.run_id)
    return ReportResponse.from_domain(report)
