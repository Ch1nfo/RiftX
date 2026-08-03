"""Immutable Run artifact endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from riftx.application.services import RegisterArtifact
from riftx.application.services.runs import require_general_run_operation

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
    require_general_run_operation(await runs.get_run(run_id))
    artifact = await service.register(
        run_id,
        RegisterArtifact(**request.model_dump()),
    )
    return ArtifactResponse.from_domain(artifact)


@router.get(
    "/runs/{run_id}/artifacts",
    response_model=ArtifactListResponse,
    responses={404: {"model": ErrorResponse}},
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
    responses={404: {"model": ErrorResponse}},
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
    response_class=FileResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def download_artifact(
    artifact_id: str,
    service: ArtifactServiceDependency,
    authorizer: RunReadAuthorizerDependency,
) -> FileResponse:
    run_id = await service.resolve_run_id(artifact_id)
    await authorizer.require(run_id)
    artifact, path = await load_authorized_child(
        service.content_path(
            artifact_id,
            expected_run_id=run_id,
        )
    )
    return FileResponse(
        path,
        media_type=artifact.mime_type,
        filename=artifact.name,
        headers={
            "ETag": f'"sha256:{artifact.sha256}"',
            "X-Artifact-SHA256": artifact.sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )
