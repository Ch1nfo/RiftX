"""Context Manifest and compilation inspection endpoints."""

from fastapi import APIRouter

from riftx.context import ContextCompilation

from ..dependencies import ContextServiceDependency
from ..schemas import ErrorResponse

router = APIRouter(tags=["context"])

_ERROR_RESPONSES = {404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


@router.get(
    "/sessions/{session_id}/context",
    response_model=ContextCompilation,
    responses=_ERROR_RESPONSES,
)
async def get_session_context(
    session_id: str,
    context_service: ContextServiceDependency,
) -> ContextCompilation:
    return await context_service.latest_for_session(session_id)


@router.get(
    "/context-compilations/{compilation_id}",
    response_model=ContextCompilation,
    responses=_ERROR_RESPONSES,
)
async def get_context_compilation(
    compilation_id: str,
    context_service: ContextServiceDependency,
) -> ContextCompilation:
    return await context_service.get(compilation_id)


@router.get(
    "/runs/{run_id}/context",
    response_model=ContextCompilation,
    responses=_ERROR_RESPONSES,
)
async def get_run_context(
    run_id: str,
    context_service: ContextServiceDependency,
) -> ContextCompilation:
    """CLI convenience endpoint for the latest Session compilation in a Run."""

    return await context_service.latest_for_run(run_id)
