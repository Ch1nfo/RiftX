"""Principal-owned Code Audit Preflight Operator endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, status

from riftx.application.errors import ServiceUnavailableError
from riftx.application.services.audit_preflight import (
    AuditPreflightApplicationService,
)

from ..dependencies import (
    AuditObjectAuthorizerDependency,
    LocalPrincipalDependency,
)
from ..errors import APIError
from ..schemas.audit_preflight import (
    AuditPreflightJobResponse,
)
from ..schemas.errors import ErrorResponse

router = APIRouter(prefix="/audits/preflight", tags=["code-audit-preflight"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
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


def get_audit_preflight_service(request: Request) -> AuditPreflightApplicationService:
    control_plane = getattr(request.app.state, "control_plane", None)
    service = getattr(control_plane, "audit_preflight_service", None)
    if not isinstance(service, AuditPreflightApplicationService):
        raise ServiceUnavailableError(
            "audit_preflight_unavailable",
            "RiftX Code Audit Preflight is temporarily unavailable",
        )
    return service


AuditPreflightServiceDependency = Annotated[
    AuditPreflightApplicationService,
    Depends(get_audit_preflight_service),
]


@router.post(
    "",
    status_code=status.HTTP_410_GONE,
    responses={410: {"model": ErrorResponse}, **_ERROR_RESPONSES},
)
async def create_audit_preflight() -> None:
    raise APIError(
        status.HTTP_410_GONE,
        "code_audit_retired",
        "Code Audit Preflight creation is retired",
    )


@router.get(
    "/{job_id}",
    response_model=AuditPreflightJobResponse,
    responses=_ERROR_RESPONSES,
)
async def get_audit_preflight(
    job_id: PreflightJobId,
    service: AuditPreflightServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> AuditPreflightJobResponse:
    return AuditPreflightJobResponse.from_job(
        await service.get_authorized(
            job_id,
            principal=principal,
            authorizer=authorizer,
        )
    )


@router.post(
    "/{job_id}/cancel",
    response_model=AuditPreflightJobResponse,
    responses=_ERROR_RESPONSES,
)
async def cancel_audit_preflight(
    job_id: PreflightJobId,
    service: AuditPreflightServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> AuditPreflightJobResponse:
    return AuditPreflightJobResponse.from_job(
        await service.cancel_authorized(
            job_id,
            principal=principal,
            authorizer=authorizer,
        )
    )


@router.post(
    "/{job_id}/plan",
    status_code=status.HTTP_410_GONE,
    responses={410: {"model": ErrorResponse}, **_ERROR_RESPONSES},
)
async def issue_audit_preflight_plan(job_id: PreflightJobId) -> None:
    raise APIError(
        status.HTTP_410_GONE,
        "code_audit_retired",
        "Code Audit Preflight Plan issuance is retired",
    )


__all__ = [
    "get_audit_preflight_service",
    "issue_audit_preflight_plan",
    "router",
]
