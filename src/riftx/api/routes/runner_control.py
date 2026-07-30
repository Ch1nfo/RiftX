"""Authenticated long-poll control channel used by outbound remote Runners."""

from typing import Annotated

from fastapi import APIRouter, Header, Query

from riftx.application.services import ExecutionStatusReport

from ..auth import bearer_token
from ..dependencies import RunnerControlServiceDependency
from ..schemas import (
    ErrorResponse,
    ExecutionOutputReportRequest,
    ExecutionOutputReportResponse,
    ExecutionStatusReportRequest,
    FinishRunnerCommandRequest,
    FinishRunnerCommandResponse,
    RunnerCommandOutputReportRequest,
    RunnerCommandResponse,
    RunnerPollResponse,
)

router = APIRouter(prefix="/runner", tags=["runner-control"])


@router.get(
    "/commands/next",
    response_model=RunnerPollResponse,
    responses={401: {"model": ErrorResponse}},
)
async def poll_runner_command(
    service: RunnerControlServiceDependency,
    node_id: Annotated[str, Header(alias="X-RiftX-Node-ID")],
    authorization: Annotated[str | None, Header()] = None,
    wait_seconds: Annotated[float, Query(ge=0, le=30)] = 0,
) -> RunnerPollResponse:
    command = await service.poll(
        node_id,
        bearer_token(authorization),
        wait_seconds=wait_seconds,
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
    node_id: Annotated[str, Header(alias="X-RiftX-Node-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> FinishRunnerCommandResponse:
    command = await service.finish_command(
        node_id,
        bearer_token(authorization),
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
    "/commands/{command_id}/output",
    response_model=ExecutionOutputReportResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def report_runner_command_output(
    command_id: str,
    payload: RunnerCommandOutputReportRequest,
    service: RunnerControlServiceDependency,
    node_id: Annotated[str, Header(alias="X-RiftX-Node-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> ExecutionOutputReportResponse:
    next_offset = await service.append_command_output(
        node_id,
        bearer_token(authorization),
        command_id,
        lease_id=payload.lease_id,
        offset=payload.offset,
        data=payload.data,
    )
    return ExecutionOutputReportResponse(next_offset=next_offset)


@router.post(
    "/executions/{execution_id}/status",
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def report_execution_status(
    execution_id: str,
    payload: ExecutionStatusReportRequest,
    service: RunnerControlServiceDependency,
    node_id: Annotated[str, Header(alias="X-RiftX-Node-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    execution = await service.report_execution(
        node_id,
        bearer_token(authorization),
        execution_id,
        ExecutionStatusReport(
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
        ),
    )
    return execution.model_dump(mode="json")


@router.post(
    "/executions/{execution_id}/output",
    response_model=ExecutionOutputReportResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def report_execution_output(
    execution_id: str,
    payload: ExecutionOutputReportRequest,
    service: RunnerControlServiceDependency,
    node_id: Annotated[str, Header(alias="X-RiftX-Node-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> ExecutionOutputReportResponse:
    next_offset = await service.append_output(
        node_id,
        bearer_token(authorization),
        execution_id,
        stream=payload.stream,
        offset=payload.offset,
        data=payload.data,
    )
    return ExecutionOutputReportResponse(next_offset=next_offset)
