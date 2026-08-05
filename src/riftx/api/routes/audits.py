"""Historical Audit compatibility plus simplified local Code Audit endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    ServiceUnavailableError,
)
from riftx.audit import LocalAuditJob, LocalAuditJobService, SourcePathAuthorizationError
from riftx.domain import OperatorCapability

from ..dependencies import (
    AuditControlServiceDependency,
    AuditObjectAuthorizerDependency,
    AuditServiceDependency,
    LocalAuditJobServiceDependency,
    LocalPrincipalDependency,
    OptionalLocalAuditJobServiceDependency,
)
from ..errors import APIError
from ..schemas import (
    AuditDraftResponse,
    AuditListQuery,
    AuditListResponse,
    AuditResponse,
    CreateAuditDraftRequest,
    CreateAuditDraftRequestV2,
    CreateLocalAuditRequest,
    ErrorResponse,
    LocalAuditFindingListResponse,
    LocalAuditFindingResponse,
    LocalAuditJobResponse,
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
    response_model=LocalAuditJobResponse | AuditDraftResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": AuditDraftResponse, "description": "Exact idempotent replay"},
        **_ERROR_RESPONSES,
    },
)
async def create_audit(
    request: CreateLocalAuditRequest | CreateAuditDraftRequestV2 | CreateAuditDraftRequest,
    local_service: OptionalLocalAuditJobServiceDependency,
    service: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> JSONResponse:
    """Create a local Audit Job or preserve the historical draft API."""

    if isinstance(request, CreateLocalAuditRequest):
        local = _require_local_service(local_service)
        try:
            local_job = await local.create(
                request.source_path,
                include_paths=request.include_patterns,
                exclude_paths=request.exclude_patterns,
            )
        except SourcePathAuthorizationError as exc:
            raise APIError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                exc.failure.value,
                "Local Audit source path or filters are invalid",
            ) from None
        response: LocalAuditJobResponse | AuditDraftResponse = LocalAuditJobResponse.from_domain(
            local_job
        )
        response_status = status.HTTP_201_CREATED
    elif isinstance(request, CreateAuditDraftRequestV2):
        result = await service.create_draft_v2_authorized(
            request.to_command(),
            principal=principal,
            authorizer=authorizer,
        )
        response = AuditDraftResponse.from_result(result)
        response_status = (
            status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        )
    else:
        service.require_legacy_draft_api_enabled()
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
        response_status = (
            status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        )
    return JSONResponse(
        status_code=response_status,
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
    response_model=LocalAuditJobResponse | AuditResponse,
    responses=_ERROR_RESPONSES,
)
async def get_audit(
    audit_id: AuditId,
    local_service: OptionalLocalAuditJobServiceDependency,
    service: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> LocalAuditJobResponse | AuditResponse:
    if local_service is not None:
        local_job = await local_service.status(audit_id)
        if local_job is not None:
            return LocalAuditJobResponse.from_domain(local_job)
    aggregate = await service.get_authorized(
        audit_id,
        principal=principal,
        authorizer=authorizer,
    )
    return AuditResponse.from_aggregate(aggregate)


@router.post(
    "/{audit_id}/pause",
    response_model=AuditResponse,
    responses=_ERROR_RESPONSES,
)
async def pause_audit(
    audit_id: AuditId,
    controls: AuditControlServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> AuditResponse:
    return AuditResponse.from_aggregate(
        await controls.pause(
            audit_id,
            principal=principal,
            authorizer=authorizer,
        )
    )


@router.post(
    "/{audit_id}/resume",
    response_model=AuditResponse,
    responses=_ERROR_RESPONSES,
)
async def resume_audit(
    audit_id: AuditId,
    controls: AuditControlServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> AuditResponse:
    return AuditResponse.from_aggregate(
        await controls.resume(
            audit_id,
            principal=principal,
            authorizer=authorizer,
        )
    )


@router.post(
    "/{audit_id}/start",
    response_model=LocalAuditJobResponse,
    responses=_ERROR_RESPONSES,
)
async def start_audit(
    audit_id: AuditId,
    service: LocalAuditJobServiceDependency,
) -> LocalAuditJobResponse:
    if not service.runnable:
        raise ServiceUnavailableError(
            "local_audit_scanner_unavailable",
            "Local Code Audit requires an enabled source root on this machine",
        )
    return LocalAuditJobResponse.from_domain(await service.start(audit_id))


@router.get(
    "/{audit_id}/findings",
    response_model=LocalAuditFindingListResponse,
    responses=_ERROR_RESPONSES,
)
async def list_local_audit_findings(
    audit_id: AuditId,
    service: LocalAuditJobServiceDependency,
    severity: Annotated[
        str | None,
        Query(pattern=r"^(critical|high|medium|low|info)$"),
    ] = None,
    category: Annotated[
        str | None,
        Query(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"),
    ] = None,
    file: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LocalAuditFindingListResponse:
    job = await _get_local_job(service, audit_id)
    findings = tuple(
        finding
        for finding in job.findings
        if (severity is None or finding.severity == severity)
        and (category is None or finding.rule_id.partition(".")[0] == category)
        and (file is None or finding.relative_path == file)
    )
    page = findings[offset : offset + limit]
    return LocalAuditFindingListResponse(
        items=tuple(LocalAuditFindingResponse.from_domain(value) for value in page),
        total=len(findings),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{audit_id}/findings/{finding_id}",
    response_model=LocalAuditFindingResponse,
    responses=_ERROR_RESPONSES,
)
async def get_local_audit_finding(
    audit_id: AuditId,
    finding_id: AuditId,
    service: LocalAuditJobServiceDependency,
) -> LocalAuditFindingResponse:
    job = await _get_local_job(service, audit_id)
    for finding in job.findings:
        if finding.id == finding_id:
            return LocalAuditFindingResponse.from_domain(finding)
    raise EntityNotFoundError("LocalAuditFinding", finding_id)


@router.get(
    "/{audit_id}/report",
    response_class=Response,
    responses=_ERROR_RESPONSES,
)
async def get_local_audit_report(
    audit_id: AuditId,
    service: LocalAuditJobServiceDependency,
    format: Annotated[str, Query(pattern=r"^(json|markdown)$")] = "json",
) -> Response:
    job = await _get_local_job(service, audit_id)
    content = job.json_report if format == "json" else job.markdown_report
    if content is None:
        raise ApplicationConflictError(
            "local_audit_report_unavailable",
            "The local Audit report is not available yet",
            details={"status": job.status.value},
        )
    return Response(
        content=content,
        media_type=("application/json" if format == "json" else "text/markdown"),
    )


@router.post(
    "/{audit_id}/cancel",
    response_model=LocalAuditJobResponse | AuditResponse,
    responses=_ERROR_RESPONSES,
)
async def cancel_audit(
    audit_id: AuditId,
    local_service: OptionalLocalAuditJobServiceDependency,
    controls: AuditControlServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> LocalAuditJobResponse | AuditResponse:
    if local_service is not None:
        local_job = await local_service.status(audit_id)
        if local_job is not None:
            return LocalAuditJobResponse.from_domain(
                await local_service.cancel(audit_id)
            )
    return AuditResponse.from_aggregate(
        await controls.cancel(
            audit_id,
            principal=principal,
            authorizer=authorizer,
        )
    )


def _require_local_service(
    service: LocalAuditJobService | None,
) -> LocalAuditJobService:
    if service is None:
        raise ServiceUnavailableError(
            "local_audit_unavailable",
            "Local Code Audit is temporarily unavailable",
        )
    return service


async def _get_local_job(
    service: LocalAuditJobService,
    audit_id: str,
) -> LocalAuditJob:
    job = await service.status(audit_id)
    if job is None:
        raise EntityNotFoundError("LocalAuditJob", audit_id)
    return job


__all__ = ["router"]
