"""Immutable Run artifact endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from riftx.application.services import RegisterArtifact

from ..dependencies import ArtifactServiceDependency
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
) -> ArtifactResponse:
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
) -> ArtifactResponse:
    return ArtifactResponse.from_domain(await service.get(artifact_id))


@router.get(
    "/artifacts/{artifact_id}/content",
    response_class=FileResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def download_artifact(
    artifact_id: str,
    service: ArtifactServiceDependency,
) -> FileResponse:
    artifact, path = await service.content_path(artifact_id)
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
