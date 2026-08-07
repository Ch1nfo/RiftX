"""Immutable interactive Run artifact endpoints."""

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Response

from riftx.api.artifact_response import ArtifactFDResponse
from riftx.application.services import RegisterArtifact
from riftx.application.services.runs import require_interactive_run_operation
from riftx.domain import Artifact
from riftx.runner import OpenedArtifactContent

from ..dependencies import (
    ArtifactServiceDependency,
    AuthorizedRunReadDependency,
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
    try:
        return ArtifactFDResponse(
            lease,
            media_type=artifact.mime_type,
            headers=headers,
        )
    except BaseException:
        lease.close()
        raise
