"""Principal-owned Code Audit Preflight Operator endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from riftx.application.errors import ServiceUnavailableError
from riftx.application.services.audit_preflight import (
    AuditPreflightApplicationService,
)

from ..dependencies import (
    AuditObjectAuthorizerDependency,
    LocalPrincipalDependency,
)
from ..schemas.audit_preflight import (
    AuditPreflightCreateResponse,
    AuditPreflightJobResponse,
    CreateAuditPreflightRequest,
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


def require_audit_preflight_create_enabled(
    service: AuditPreflightServiceDependency,
) -> None:
    """Run the feature gate before FastAPI validates the sensitive request body."""

    service.require_create_enabled()


@router.post(
    "",
    response_model=AuditPreflightCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {
            "model": AuditPreflightCreateResponse,
            "description": "Exact idempotent replay",
        },
        **_ERROR_RESPONSES,
    },
    dependencies=[Depends(require_audit_preflight_create_enabled)],
)
async def create_audit_preflight(
    request: CreateAuditPreflightRequest,
    service: AuditPreflightServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> JSONResponse:
    result = await service.create_authorized(
        request.to_domain(),
        principal=principal,
        authorizer=authorizer,
    )
    response = AuditPreflightCreateResponse.from_result(result)
    return JSONResponse(
        status_code=(status.HTTP_202_ACCEPTED if result.created else status.HTTP_200_OK),
        content=jsonable_encoder(response),
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


__all__ = [
    "get_audit_preflight_service",
    "require_audit_preflight_create_enabled",
    "router",
]
