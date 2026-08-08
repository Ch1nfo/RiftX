"""Context Manifest and compilation inspection endpoints."""

import secrets
from typing import Any

from fastapi import APIRouter

from riftx.application.errors import resource_not_accessible
from riftx.application.services.runs import require_interactive_run_operation
from riftx.context import ContextCompilation

from ..dependencies import (
    AuthorizedRunReadDependency,
    ContextServiceDependency,
    RunReadAuthorizerDependency,
    load_authorized_child,
    require_run_read_binding,
)
from ..schemas import ErrorResponse

router = APIRouter(tags=["context"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "/sessions/{session_id}/context",
    response_model=ContextCompilation,
    responses=_ERROR_RESPONSES,
)
async def get_session_context(
    session_id: str,
    context_service: ContextServiceDependency,
    authorizer: RunReadAuthorizerDependency,
) -> ContextCompilation:
    compilation_id, run_id = await context_service.resolve_latest_for_session(session_id)
    authorized_run = await authorizer.require(run_id)
    require_interactive_run_operation(authorized_run)
    compilation = await load_authorized_child(context_service.get(compilation_id))
    _require_context_binding(
        compilation,
        expected_compilation_id=compilation_id,
        expected_run_id=run_id,
        expected_session_id=session_id,
    )
    return compilation


@router.get(
    "/context-compilations/{compilation_id}",
    response_model=ContextCompilation,
    responses=_ERROR_RESPONSES,
)
async def get_context_compilation(
    compilation_id: str,
    context_service: ContextServiceDependency,
    authorizer: RunReadAuthorizerDependency,
) -> ContextCompilation:
    run_id = await context_service.resolve_run_id(compilation_id)
    authorized_run = await authorizer.require(run_id)
    require_interactive_run_operation(authorized_run)
    compilation = await load_authorized_child(context_service.get(compilation_id))
    _require_context_binding(
        compilation,
        expected_compilation_id=compilation_id,
        expected_run_id=run_id,
    )
    return compilation


@router.get(
    "/runs/{run_id}/context",
    response_model=ContextCompilation,
    responses=_ERROR_RESPONSES,
)
async def get_run_context(
    run_id: str,
    context_service: ContextServiceDependency,
    authorized_run: AuthorizedRunReadDependency,
) -> ContextCompilation:
    """CLI convenience endpoint for the latest Session compilation in a Run."""

    require_interactive_run_operation(authorized_run)
    compilation = await load_authorized_child(context_service.latest_for_run(run_id))
    _require_context_binding(compilation, expected_run_id=run_id)
    return compilation


def _require_context_binding(
    compilation: ContextCompilation,
    *,
    expected_compilation_id: str | None = None,
    expected_run_id: str,
    expected_session_id: str | None = None,
) -> None:
    if expected_compilation_id is not None and not secrets.compare_digest(
        expected_compilation_id,
        compilation.id,
    ):
        raise resource_not_accessible()
    require_run_read_binding(expected_run_id, compilation.run_id)
    require_run_read_binding(expected_run_id, compilation.manifest.run_id)
    if (
        compilation.manifest.session_id != compilation.session_id
        or (
            expected_session_id is not None
            and (
                compilation.session_id != expected_session_id
                or compilation.manifest.session_id != expected_session_id
            )
        )
    ):
        raise resource_not_accessible()
