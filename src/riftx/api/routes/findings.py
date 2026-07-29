"""Editable structured Finding endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query

from riftx.application.services import CreateFinding, UpdateFinding
from riftx.domain import Finding, FindingSeverity, FindingStatus

from ..dependencies import FindingServiceDependency
from ..schemas import (
    CreateFindingRequest,
    ErrorResponse,
    FindingListResponse,
    UpdateFindingRequest,
)

router = APIRouter(tags=["findings"])


@router.post(
    "/runs/{run_id}/findings",
    response_model=Finding,
    status_code=201,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def create_finding(
    run_id: str,
    request: CreateFindingRequest,
    service: FindingServiceDependency,
) -> Finding:
    values = request.model_dump(exclude={"evidence"})
    return await service.create_finding(
        run_id,
        CreateFinding(**values, evidence=request.evidence),
    )


@router.get(
    "/runs/{run_id}/findings",
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


@router.get(
    "/findings/{finding_id}",
    response_model=Finding,
    responses={404: {"model": ErrorResponse}},
)
async def get_finding(
    finding_id: str,
    service: FindingServiceDependency,
) -> Finding:
    return await service.get_finding(finding_id)


@router.patch(
    "/findings/{finding_id}",
    response_model=Finding,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def update_finding(
    finding_id: str,
    request: UpdateFindingRequest,
    service: FindingServiceDependency,
) -> Finding:
    values = request.model_dump(exclude_unset=True, exclude={"evidence"})
    if "evidence" in request.model_fields_set:
        values["evidence"] = request.evidence
    return await service.update_finding(finding_id, UpdateFinding(**values))
