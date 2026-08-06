"""Immutable Run artifact endpoints."""

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Path, Query, Response

from riftx.api.artifact_response import ArtifactFDResponse
from riftx.application.errors import resource_not_accessible
from riftx.application.ports import AuditObjectAuthorizer
from riftx.application.services import AuditApplicationService, RegisterArtifact
from riftx.application.services.artifacts import ArtifactApplicationService
from riftx.application.services.runs import require_interactive_run_operation
from riftx.domain import Artifact, LocalPrincipal, RunKind
from riftx.runner import OpenedArtifactContent

from ..dependencies import (
    ArtifactServiceDependency,
    AuditObjectAuthorizerDependency,
    AuditServiceDependency,
    AuthorizedRunReadDependency,
    LocalPrincipalDependency,
    RunReadAuthorizerDependency,
    RunServiceDependency,
    load_authorized_child,
    require_run_read_binding,
)
from ..schemas import (
    ArtifactListResponse,
    ArtifactResponse,
    ErrorResponse,
    RegisterArtifactRequest,
)

router = APIRouter(tags=["artifacts"])

AuditId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$",
    ),
]

_AUDIT_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}

_GENERIC_ARTIFACT_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}

_ARTIFACT_BINARY_RESPONSE: dict[str, Any] = {
    "description": "Immutable Artifact content",
    "content": {
        "application/octet-stream": {
            "schema": {"type": "string", "format": "binary"},
        }
    },
}

_GENERIC_ARTIFACT_CONTENT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: _ARTIFACT_BINARY_RESPONSE,
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}

_AUDIT_ARTIFACT_CONTENT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_AUDIT_READ_RESPONSES,
    200: _ARTIFACT_BINARY_RESPONSE,
}


@router.post(
    "/runs/{run_id}/artifacts",
    response_model=ArtifactResponse,
    status_code=201,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def register_artifact(
    run_id: str,
    request: RegisterArtifactRequest,
    service: ArtifactServiceDependency,
    runs: RunServiceDependency,
) -> ArtifactResponse:
    require_interactive_run_operation(await runs.get_run(run_id))
    artifact = await service.register(
        run_id,
        RegisterArtifact(**request.model_dump()),
    )
    return ArtifactResponse.from_domain(artifact)


@router.get(
    "/runs/{run_id}/artifacts",
    response_model=ArtifactListResponse,
    responses=_GENERIC_ARTIFACT_READ_RESPONSES,
)
async def list_artifacts(
    run_id: str,
    service: ArtifactServiceDependency,
    _authorized_run: AuthorizedRunReadDependency,
    execution_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ArtifactListResponse:
    artifacts = await service.list(
        run_id,
        execution_id=execution_id,
        limit=limit,
        offset=offset,
    )
    return ArtifactListResponse(
        items=[ArtifactResponse.from_domain(item) for item in artifacts],
        limit=limit,
        offset=offset,
    )


@router.get(
    "/artifacts/{artifact_id}",
    response_model=ArtifactResponse,
    responses=_GENERIC_ARTIFACT_READ_RESPONSES,
)
async def get_artifact(
    artifact_id: str,
    service: ArtifactServiceDependency,
    authorizer: RunReadAuthorizerDependency,
) -> ArtifactResponse:
    run_id = await service.resolve_run_id(artifact_id)
    await authorizer.require(run_id)
    artifact = await load_authorized_child(service.get(artifact_id))
    require_run_read_binding(run_id, artifact.run_id)
    return ArtifactResponse.from_domain(artifact)


@router.get(
    "/artifacts/{artifact_id}/content",
    response_class=Response,
    responses=_GENERIC_ARTIFACT_CONTENT_RESPONSES,
)
async def download_artifact(
    artifact_id: str,
    service: ArtifactServiceDependency,
    authorizer: RunReadAuthorizerDependency,
) -> ArtifactFDResponse:
    run_id = await service.resolve_run_id(artifact_id)
    await authorizer.require(run_id)
    artifact, lease = await load_authorized_child(
        service.open_public_content(
            artifact_id,
            expected_run_id=run_id,
        )
    )
    return _artifact_fd_response(artifact, lease)


@router.get(
    "/audits/{audit_id}/artifacts",
    response_model=ArtifactListResponse,
    responses=_AUDIT_READ_RESPONSES,
)
async def list_audit_artifacts(
    audit_id: AuditId,
    service: ArtifactServiceDependency,
    audits: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
    execution_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ArtifactListResponse:
    aggregate = await load_authorized_child(
        audits.get_authorized(
            audit_id,
            principal=principal,
            authorizer=authorizer,
        )
    )
    artifacts = await service.list_for_audit(
        audit_id,
        aggregate.run.id,
        execution_id=execution_id,
        limit=limit,
        offset=offset,
    )
    return ArtifactListResponse(
        items=[ArtifactResponse.from_domain(item) for item in artifacts],
        limit=limit,
        offset=offset,
    )


@router.get(
    "/audits/{audit_id}/artifacts/{artifact_id}",
    response_model=ArtifactResponse,
    responses=_AUDIT_READ_RESPONSES,
)
async def get_audit_artifact(
    audit_id: AuditId,
    artifact_id: str,
    service: ArtifactServiceDependency,
    audits: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> ArtifactResponse:
    run_id = await _authorize_audit_artifact(
        audit_id,
        artifact_id,
        service=service,
        audits=audits,
        principal=principal,
        authorizer=authorizer,
    )
    artifact = await load_authorized_child(
        service.get_for_audit(
            artifact_id,
            audit_id=audit_id,
            run_id=run_id,
        )
    )
    return ArtifactResponse.from_domain(artifact)


@router.get(
    "/audits/{audit_id}/artifacts/{artifact_id}/content",
    response_class=Response,
    responses=_AUDIT_ARTIFACT_CONTENT_RESPONSES,
)
async def download_audit_artifact(
    audit_id: AuditId,
    artifact_id: str,
    service: ArtifactServiceDependency,
    audits: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    authorizer: AuditObjectAuthorizerDependency,
) -> ArtifactFDResponse:
    run_id = await _authorize_audit_artifact(
        audit_id,
        artifact_id,
        service=service,
        audits=audits,
        principal=principal,
        authorizer=authorizer,
    )
    artifact, lease = await load_authorized_child(
        service.open_audit_content(
            artifact_id,
            audit_id=audit_id,
            run_id=run_id,
        )
    )
    return _artifact_fd_response(artifact, lease)


async def _authorize_audit_artifact(
    audit_id: str,
    artifact_id: str,
    *,
    service: ArtifactApplicationService,
    audits: AuditApplicationService,
    principal: LocalPrincipal,
    authorizer: AuditObjectAuthorizer,
) -> str:
    binding = await service.resolve_owner(artifact_id)
    if (
        binding.artifact_id != artifact_id
        or binding.audit_id is None
        or binding.audit_id != audit_id
        or binding.run_kind is not RunKind.CODE_AUDIT
        or binding.audit_run_id != binding.run_id
    ):
        raise resource_not_accessible()
    aggregate = await load_authorized_child(
        audits.get_authorized(
            audit_id,
            principal=principal,
            authorizer=authorizer,
        )
    )
    if binding.run_id != aggregate.run.id:
        raise resource_not_accessible()
    return aggregate.run.id


def _artifact_fd_response(
    artifact: Artifact,
    lease: OpenedArtifactContent,
) -> ArtifactFDResponse:
    headers = {
        "Content-Length": str(artifact.size),
        "Content-Disposition": ("attachment; filename*=UTF-8''" + quote(artifact.name, safe="")),
        "ETag": f'"sha256:{artifact.sha256}"',
        "X-Artifact-SHA256": artifact.sha256,
        "X-Content-Type-Options": "nosniff",
    }
    if artifact.audit_id is not None:
        headers["Cache-Control"] = "no-store"
    try:
        return ArtifactFDResponse(
            lease,
            media_type=artifact.mime_type,
            headers=headers,
        )
    except BaseException:
        lease.close()
        raise
