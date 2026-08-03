"""RiftX Code Audit draft creation and authorized read endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from riftx.domain import OperatorCapability

from ..dependencies import (
    AuditObjectAuthorizerDependency,
    AuditServiceDependency,
    LocalPrincipalDependency,
)
from ..schemas import (
    AuditDraftResponse,
    AuditListQuery,
    AuditListResponse,
    AuditResponse,
    CreateAuditDraftRequest,
    ErrorResponse,
)

router = APIRouter(prefix="/audits", tags=["code-audit"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}

AuditId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$",
    ),
]


@router.post(
    "",
    response_model=AuditDraftResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": AuditDraftResponse, "description": "Exact idempotent replay"},
        **_ERROR_RESPONSES,
    },
)
async def create_audit(
    request: CreateAuditDraftRequest,
    service: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> JSONResponse:
    """Persist a draft only; this route never performs Preflight or Start."""

    authorization_reference = authorizer.draft_authorization_reference(
        principal,
        capability=OperatorCapability.WRITE,
    )
    result = await service.create_draft_authorized(
        request.to_command(authorization_reference=authorization_reference),
        principal=principal,
        authorizer=authorizer,
    )
    response = AuditDraftResponse.from_result(result)
    return JSONResponse(
        status_code=(status.HTTP_201_CREATED if result.created else status.HTTP_200_OK),
        content=jsonable_encoder(response),
    )


@router.get(
    "",
    response_model=AuditListResponse,
    responses=_ERROR_RESPONSES,
)
async def list_audits(
    service: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
    query: Annotated[AuditListQuery, Query()],
) -> AuditListResponse:
    aggregates = await service.list_authorized(
        principal=principal,
        authorizer=authorizer,
        run_id=query.run_id,
        project_id=query.project_id,
        engagement_id=query.engagement_id,
        lifecycle_status=query.lifecycle_status,
        mode=query.mode,
        created_from=query.created_from,
        created_to=query.created_to,
        limit=query.limit,
        offset=query.offset,
    )
    return AuditListResponse.from_aggregates(
        aggregates,
        limit=query.limit,
        offset=query.offset,
    )


@router.get(
    "/{audit_id}",
    response_model=AuditResponse,
    responses=_ERROR_RESPONSES,
)
async def get_audit(
    audit_id: AuditId,
    service: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> AuditResponse:
    aggregate = await service.get_authorized(
        audit_id,
        principal=principal,
        authorizer=authorizer,
    )
    return AuditResponse.from_aggregate(aggregate)


__all__ = ["router"]
