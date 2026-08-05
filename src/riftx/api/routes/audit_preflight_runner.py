"""Authenticated Runner transport for the dedicated Audit Preflight owner."""

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from riftx.domain.audit_preflight_wire import (
    AuditPreflightCallbackAck,
    AuditPreflightLeaseGrant,
    AuditPreflightStartGrant,
)

from ..dependencies import (
    AuditPreflightRunnerDependency,
    AuditPreflightRunnerServiceDependency,
)
from ..schemas.audit_preflight_runner import (
    AuditPreflightRunnerPollResponse,
    FinishAuditPreflightRequest,
    RenewAuditPreflightLeaseRequest,
    StartAuditPreflightRequest,
    StopAuditPreflightRequest,
)
from ..schemas.errors import ErrorResponse

router = APIRouter(
    prefix="/runner/audit-preflight",
    tags=["audit-preflight-runner"],
)

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}

PreflightJobId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$",
    ),
]


@router.get(
    "/next",
    response_model=AuditPreflightRunnerPollResponse,
    responses=_ERROR_RESPONSES,
)
async def poll_audit_preflight_job(
    service: AuditPreflightRunnerServiceDependency,
    authorized: AuditPreflightRunnerDependency,
    wait_seconds: Annotated[float, Query(ge=0, le=30)] = 0,
) -> AuditPreflightRunnerPollResponse:
    dispatch = await service.poll(
        node_id=authorized.node_id,
        principal=authorized.principal,
        protocol_capabilities=authorized.protocol_capabilities,
        wait_seconds=wait_seconds,
    )
    return AuditPreflightRunnerPollResponse(dispatch=dispatch)


@router.post(
    "/{job_id}/lease",
    response_model=AuditPreflightLeaseGrant,
    responses=_ERROR_RESPONSES,
)
async def renew_audit_preflight_lease(
    job_id: PreflightJobId,
    payload: RenewAuditPreflightLeaseRequest,
    service: AuditPreflightRunnerServiceDependency,
    authorized: AuditPreflightRunnerDependency,
) -> AuditPreflightLeaseGrant:
    return await service.renew_lease(
        job_id,
        node_id=authorized.node_id,
        principal=authorized.principal,
        protocol_capabilities=authorized.protocol_capabilities,
        owner=payload.owner,
        lease=payload.lease,
        state_version=payload.state_version,
        capsule_id=payload.capsule_id,
    )


@router.post(
    "/{job_id}/start",
    response_model=AuditPreflightStartGrant,
    responses=_ERROR_RESPONSES,
)
async def start_audit_preflight_job(
    job_id: PreflightJobId,
    payload: StartAuditPreflightRequest,
    service: AuditPreflightRunnerServiceDependency,
    authorized: AuditPreflightRunnerDependency,
) -> AuditPreflightStartGrant:
    return await service.start(
        job_id,
        node_id=authorized.node_id,
        principal=authorized.principal,
        protocol_capabilities=authorized.protocol_capabilities,
        owner=payload.owner,
        lease=payload.lease,
        state_version=payload.state_version,
        capsule_id=payload.capsule_id,
        capsule_prepare_proof_digest=payload.capsule_prepare_proof_digest,
    )


@router.post(
    "/{job_id}/finish",
    response_model=AuditPreflightCallbackAck,
    responses=_ERROR_RESPONSES,
)
async def finish_audit_preflight_job(
    job_id: PreflightJobId,
    payload: FinishAuditPreflightRequest,
    service: AuditPreflightRunnerServiceDependency,
    authorized: AuditPreflightRunnerDependency,
) -> AuditPreflightCallbackAck:
    return await service.finish(
        job_id,
        node_id=authorized.node_id,
        principal=authorized.principal,
        protocol_capabilities=authorized.protocol_capabilities,
        owner=payload.owner,
        lease=payload.lease,
        state_version=payload.state_version,
        capsule_id=payload.capsule_id,
        status=payload.status,
        result=payload.result,
        safe_error_code=payload.safe_error_code,
        exit_receipt=payload.exit_receipt,
    )


@router.post(
    "/{job_id}/stop",
    response_model=AuditPreflightCallbackAck,
    responses=_ERROR_RESPONSES,
)
async def stop_audit_preflight_job(
    job_id: PreflightJobId,
    payload: StopAuditPreflightRequest,
    service: AuditPreflightRunnerServiceDependency,
    authorized: AuditPreflightRunnerDependency,
) -> AuditPreflightCallbackAck:
    return await service.stop(
        job_id,
        node_id=authorized.node_id,
        principal=authorized.principal,
        protocol_capabilities=authorized.protocol_capabilities,
        owner=payload.owner,
        lease=payload.lease,
        state_version=payload.state_version,
        capsule_id=payload.capsule_id,
        status=payload.status,
        safe_error_code=payload.safe_error_code,
        stop_receipt=payload.stop_receipt,
    )


__all__ = ["router"]
