"""Durable human-in-the-loop approval endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query

from riftx.application.services import DecideApproval
from riftx.domain import ApprovalStatus

from ..dependencies import ApprovalServiceDependency, LocalPrincipalDependency
from ..schemas import (
    ApprovalDecisionRequest,
    ApprovalListResponse,
    ApprovalResponse,
    ErrorResponse,
)

router = APIRouter(tags=["approvals"])

_ERROR_RESPONSES = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.get(
    "/runs/{run_id}/approvals",
    response_model=ApprovalListResponse,
    responses=_ERROR_RESPONSES,
)
async def list_approvals(
    run_id: str,
    service: ApprovalServiceDependency,
    approval_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> ApprovalListResponse:
    approvals = await service.list(run_id, status=approval_status)
    return ApprovalListResponse(
        items=[ApprovalResponse.from_domain(approval) for approval in approvals]
    )


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ApprovalResponse,
    responses=_ERROR_RESPONSES,
)
async def approve(
    approval_id: str,
    request: ApprovalDecisionRequest,
    service: ApprovalServiceDependency,
    principal: LocalPrincipalDependency,
) -> ApprovalResponse:
    approval = await service.approve(
        approval_id,
        DecideApproval(
            decided_by=principal.id,
            reason=request.reason,
            approve_for_run=request.approve_for_run,
        ),
    )
    return ApprovalResponse.from_domain(approval)


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=ApprovalResponse,
    responses=_ERROR_RESPONSES,
)
async def reject(
    approval_id: str,
    request: ApprovalDecisionRequest,
    service: ApprovalServiceDependency,
    principal: LocalPrincipalDependency,
) -> ApprovalResponse:
    approval = await service.reject(
        approval_id,
        DecideApproval(
            decided_by=principal.id,
            reason=request.reason,
            approve_for_run=request.approve_for_run,
        ),
    )
    return ApprovalResponse.from_domain(approval)
