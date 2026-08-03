"""Run lifecycle and control endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from riftx.application.run_kind_effects import (
    EffectMode,
    EffectOrigin,
    OperationEffect,
    RunEffectOperation,
    global_effect_ownership_for_local_principal,
)
from riftx.application.services.runs import (
    require_global_effect_operation,
    require_run_kind_effect_operation,
)
from riftx.domain import RunKind, RunStatus

from ..dependencies import (
    AuditObjectAuthorizerDependency,
    AuditServiceDependency,
    AuthorizedRunReadDependency,
    LocalPrincipalDependency,
    RunServiceDependency,
    ToolServiceDependency,
)
from ..schemas import (
    CompactRunRequest,
    CreateRunRequest,
    ErrorResponse,
    RunActionResponse,
    RunListResponse,
    RunMessageRequest,
    RunResponse,
    SwitchRunModelRequest,
)
from ..schemas.runs import RunReadResponse, run_read_response_from_domain

router = APIRouter(prefix="/runs", tags=["runs"])

_ERROR_RESPONSES = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def create_run(
    request: CreateRunRequest,
    run_service: RunServiceDependency,
    tool_service: ToolServiceDependency,
    principal: LocalPrincipalDependency,
) -> RunResponse:
    ownership = global_effect_ownership_for_local_principal(principal)
    require_global_effect_operation(
        ownership,
        operation=RunEffectOperation.CREATE_RUN,
        origin=EffectOrigin.LOCAL_OPERATOR_API,
        effect=OperationEffect.DURABLE_WRITE,
    )
    run = await run_service.create_run(
        request.to_command(default_node_id=tool_service.node_id),
        principal=principal,
    )
    return RunResponse.from_domain(run)


@router.get("", response_model=RunListResponse, responses=_ERROR_RESPONSES)
async def list_runs(
    run_service: RunServiceDependency,
    audit_service: AuditServiceDependency,
    principal: LocalPrincipalDependency,
    audit_authorizer: AuditObjectAuthorizerDependency,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    run_kind: Annotated[RunKind, Query(alias="kind")] = RunKind.GENERAL,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListResponse:
    if run_kind is RunKind.CODE_AUDIT:
        runs = []
        page_offset = offset
        remaining = limit
        while remaining:
            page_limit = min(remaining, 200)
            aggregates = await audit_service.list_authorized(
                principal=principal,
                authorizer=audit_authorizer,
                run_status=run_status,
                limit=page_limit,
                offset=page_offset,
            )
            runs.extend(aggregate.run for aggregate in aggregates)
            if len(aggregates) < page_limit:
                break
            remaining -= len(aggregates)
            page_offset += len(aggregates)
    else:
        runs = await run_service.list_runs(
            status=run_status,
            kind=run_kind,
            limit=limit,
            offset=offset,
        )
    return RunListResponse(
        items=[run_read_response_from_domain(run) for run in runs],
        limit=limit,
        offset=offset,
    )


@router.get("/{run_id}", response_model=RunReadResponse, responses=_ERROR_RESPONSES)
async def get_run(
    authorized_run: AuthorizedRunReadDependency,
) -> RunReadResponse:
    return run_read_response_from_domain(authorized_run)


@router.post(
    "/{run_id}/pause",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def pause_run(run_id: str, run_service: RunServiceDependency) -> RunActionResponse:
    require_run_kind_effect_operation(
        await run_service.get_run(run_id),
        operation=RunEffectOperation.PAUSE_RUN,
        origin=EffectOrigin.LOCAL_OPERATOR_API,
        effect=OperationEffect.WORKFLOW_CONTROL,
        mode=EffectMode.NORMAL,
    )
    return RunActionResponse(run=RunResponse.from_domain(await run_service.pause(run_id)))


@router.post(
    "/{run_id}/resume",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def resume_run(run_id: str, run_service: RunServiceDependency) -> RunActionResponse:
    require_run_kind_effect_operation(
        await run_service.get_run(run_id),
        operation=RunEffectOperation.RESUME_RUN,
        origin=EffectOrigin.LOCAL_OPERATOR_API,
        effect=OperationEffect.WORKFLOW_CONTROL,
        mode=EffectMode.NORMAL,
    )
    return RunActionResponse(run=RunResponse.from_domain(await run_service.resume(run_id)))


@router.post(
    "/{run_id}/cancel",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def cancel_run(run_id: str, run_service: RunServiceDependency) -> RunActionResponse:
    require_run_kind_effect_operation(
        await run_service.get_run(run_id),
        operation=RunEffectOperation.CANCEL_RUN,
        origin=EffectOrigin.LOCAL_OPERATOR_API,
        effect=OperationEffect.WORKFLOW_CONTROL,
        mode=EffectMode.NORMAL,
    )
    return RunActionResponse(run=RunResponse.from_domain(await run_service.cancel(run_id)))


@router.post(
    "/{run_id}/compact",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def compact_run(
    run_id: str,
    request: CompactRunRequest,
    run_service: RunServiceDependency,
) -> RunActionResponse:
    require_run_kind_effect_operation(
        await run_service.get_run(run_id),
        operation=RunEffectOperation.COMPACT_RUN,
        origin=EffectOrigin.LOCAL_OPERATOR_API,
        effect=OperationEffect.WORKFLOW_CONTROL,
        mode=EffectMode.NORMAL,
    )
    return RunActionResponse(
        run=RunResponse.from_domain(
            await run_service.compact(run_id, max_history_items=request.max_history_items)
        )
    )


@router.post(
    "/{run_id}/model",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def switch_run_model(
    run_id: str,
    request: SwitchRunModelRequest,
    run_service: RunServiceDependency,
) -> RunActionResponse:
    require_run_kind_effect_operation(
        await run_service.get_run(run_id),
        operation=RunEffectOperation.SWITCH_RUN_MODEL,
        origin=EffectOrigin.LOCAL_OPERATOR_API,
        effect=OperationEffect.WORKFLOW_CONTROL,
        mode=EffectMode.NORMAL,
    )
    return RunActionResponse(
        run=RunResponse.from_domain(await run_service.switch_model(run_id, request.model_profile))
    )


@router.post(
    "/{run_id}/cancel-current-execution",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def cancel_current_execution(
    run_id: str,
    run_service: RunServiceDependency,
) -> RunActionResponse:
    require_run_kind_effect_operation(
        await run_service.get_run(run_id),
        operation=RunEffectOperation.CANCEL_CURRENT_EXECUTION,
        origin=EffectOrigin.LOCAL_OPERATOR_API,
        effect=OperationEffect.WORKFLOW_CONTROL,
        mode=EffectMode.NORMAL,
    )
    return RunActionResponse(
        run=RunResponse.from_domain(await run_service.cancel_current_execution(run_id))
    )


@router.post(
    "/{run_id}/message",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def append_message(
    run_id: str,
    request: RunMessageRequest,
    run_service: RunServiceDependency,
) -> RunActionResponse:
    require_run_kind_effect_operation(
        await run_service.get_run(run_id),
        operation=RunEffectOperation.APPEND_MESSAGE,
        origin=EffectOrigin.LOCAL_OPERATOR_API,
        effect=OperationEffect.WORKFLOW_CONTROL,
        mode=EffectMode.NORMAL,
    )
    return RunActionResponse(
        run=RunResponse.from_domain(
            await run_service.append_user_message(
                run_id,
                request.message,
                message_event_id=(
                    str(request.message_event_id) if request.message_event_id is not None else None
                ),
            )
        )
    )
