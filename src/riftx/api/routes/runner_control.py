"""Authenticated long-poll control channel used by outbound remote Runners."""

from typing import Annotated

from fastapi import APIRouter, Query

from riftx.application.services import ExecutionStatusReport
from riftx.domain import ExecutionStatus
from riftx.domain.base import utc_now

from ..dependencies import RunnerControlServiceDependency, RunnerDependency
from ..schemas import (
    ErrorResponse,
    ExecutionOutputReportRequest,
    ExecutionOutputReportResponse,
    ExecutionStatusReportRequest,
    FinishRunnerCommandRequest,
    FinishRunnerCommandResponse,
    RenewRunnerCommandLeaseRequest,
    RenewRunnerCommandLeaseResponse,
    RunnerCommandOutputReportRequest,
    RunnerCommandResponse,
    RunnerPollResponse,
)

router = APIRouter(prefix="/runner", tags=["runner-control"])

_PHYSICAL_STOP_STATUSES = frozenset(
    {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.EXITED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.HARD_TIMEOUT,
    }
)


@router.get(
    "/commands/next",
    response_model=RunnerPollResponse,
    responses={401: {"model": ErrorResponse}},
)
async def poll_runner_command(
    service: RunnerControlServiceDependency,
    authorized: RunnerDependency,
    wait_seconds: Annotated[float, Query(ge=0, le=30)] = 0,
    safety_only: bool = False,
) -> RunnerPollResponse:
    command = await service.poll(
        authorized.node_id,
        authorized.token,
        wait_seconds=wait_seconds,
        safety_only=safety_only,
    )
    return RunnerPollResponse(
        command=RunnerCommandResponse.from_domain(command) if command else None
    )


@router.post(
    "/commands/{command_id}/finish",
    response_model=FinishRunnerCommandResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def finish_runner_command(
    command_id: str,
    payload: FinishRunnerCommandRequest,
    service: RunnerControlServiceDependency,
    authorized: RunnerDependency,
) -> FinishRunnerCommandResponse:
    command = await service.finish_command(
        authorized.node_id,
        authorized.token,
        command_id,
        lease_id=payload.lease_id,
        succeeded=payload.succeeded,
        result=payload.result,
        error=payload.error,
    )
    return FinishRunnerCommandResponse(
        id=command.id,
        status=command.status,
        completed_at=command.completed_at,
    )


@router.post(
    "/commands/{command_id}/lease",
    response_model=RenewRunnerCommandLeaseResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def renew_runner_command_lease(
    command_id: str,
    payload: RenewRunnerCommandLeaseRequest,
    service: RunnerControlServiceDependency,
    authorized: RunnerDependency,
) -> RenewRunnerCommandLeaseResponse:
    command = await service.renew_command_lease(
        authorized.node_id,
        authorized.token,
        command_id,
        lease_id=payload.lease_id,
    )
    if command.lease_expires_at is None:
        raise RuntimeError("renewed Runner command omitted its lease expiry")
    return RenewRunnerCommandLeaseResponse(
        id=command.id,
        lease_expires_at=command.lease_expires_at,
        lease_duration_seconds=max(
            0.001,
            (command.lease_expires_at - utc_now()).total_seconds(),
        ),
    )


@router.post(
    "/commands/{command_id}/output",
    response_model=ExecutionOutputReportResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def report_runner_command_output(
    command_id: str,
    payload: RunnerCommandOutputReportRequest,
    service: RunnerControlServiceDependency,
    authorized: RunnerDependency,
) -> ExecutionOutputReportResponse:
    next_offset = await service.append_command_output(
        authorized.node_id,
        authorized.token,
        command_id,
        lease_id=payload.lease_id,
        offset=payload.offset,
        data=payload.data,
    )
    return ExecutionOutputReportResponse(next_offset=next_offset)


@router.post(
    "/executions/{execution_id}/status",
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def report_execution_status(
    execution_id: str,
    payload: ExecutionStatusReportRequest,
    service: RunnerControlServiceDependency,
    authorized: RunnerDependency,
) -> dict[str, object]:
    report = ExecutionStatusReport(
        status=payload.status,
        pid=payload.pid,
        process_group_id=payload.process_group_id,
        exit_code=payload.exit_code,
        executable_path=payload.executable_path,
        tool_id=payload.tool_id,
        tool_version=payload.tool_version,
        platform_system=payload.platform_system,
        platform_release=payload.platform_release,
        platform_architecture=payload.platform_architecture,
        process_created_at=payload.process_created_at,
        physical_stop_confirmed=payload.physical_stop_confirmed,
    )
    await service.require_execution_callback_kind(
        node_id=authorized.node_id,
        principal=authorized.principal,
        execution_id=execution_id,
        allow_safety_stop=(
            report.physical_stop_confirmed is True
            and report.status in _PHYSICAL_STOP_STATUSES
        ),
    )
    execution = await service.report_execution(
        authorized.node_id,
        authorized.token,
        execution_id,
        report,
    )
    return execution.model_dump(mode="json")


@router.post(
    "/executions/{execution_id}/output",
    response_model=ExecutionOutputReportResponse,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def report_execution_output(
    execution_id: str,
    payload: ExecutionOutputReportRequest,
    service: RunnerControlServiceDependency,
    authorized: RunnerDependency,
) -> ExecutionOutputReportResponse:
    await service.require_execution_callback_kind(
        node_id=authorized.node_id,
        principal=authorized.principal,
        execution_id=execution_id,
    )
    next_offset = await service.append_output(
        authorized.node_id,
        authorized.token,
        execution_id,
        stream=payload.stream,
        offset=payload.offset,
        data=payload.data,
    )
    return ExecutionOutputReportResponse(next_offset=next_offset)
