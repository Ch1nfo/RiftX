"""Authenticated long-poll control channel used by outbound remote Runners."""

from typing import Annotated

from fastapi import APIRouter, Query

from riftx.application.errors import ApplicationConflictError
from riftx.application.run_kind_effects import (
    EffectMode,
    EffectOrigin,
    LegacyRunnerCommandEffectOwnership,
    OperationEffect,
    RunEffectOperation,
    RunKindEffectPolicyDenied,
    require_run_kind_effect_policy,
)
from riftx.application.services import ExecutionStatusReport
from riftx.domain.base import utc_now

from ..dependencies import RunnerControlServiceDependency, RunnerDependency
from ..schemas import (
    ErrorResponse,
    ExecutionOutputReportRequest,
    ExecutionOutputReportResponse,
    ExecutionStatusReportRequest,
    FinishRunnerCommandRequest,
    FinishRunnerCommandResponse,
    LegacyFinishRunnerCommandRequest,
    RenewRunnerCommandLeaseRequest,
    RenewRunnerCommandLeaseResponse,
    RunnerCommandOutputReportRequest,
    RunnerCommandResponse,
    RunnerPollResponse,
)

router = APIRouter(prefix="/runner", tags=["runner-control"])


def _require_legacy_finish_route_policy(
    *,
    node_id: str,
    runner_principal: object,
    command_id: str,
    lease_id: str,
) -> None:
    try:
        require_run_kind_effect_policy(
            RunEffectOperation.FINISH_LEGACY_RUNNER_COMMAND,
            EffectOrigin.RUNNER_API,
            ownership=LegacyRunnerCommandEffectOwnership(
                node_id=node_id,
                runner_principal=runner_principal,
                runner_command_id=command_id,
                lease_identity=lease_id,
                quarantine_state="quarantined:legacy_ownership_missing",
            ),
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.STOP_PROOF,
        )
    except (RunKindEffectPolicyDenied, TypeError, ValueError):
        raise ApplicationConflictError(
            "run_kind_effect_policy_denied",
            "The legacy Runner finish callback is not admitted",
        ) from None


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
    deprecated=True,
)
async def finish_legacy_runner_command(
    command_id: str,
    payload: LegacyFinishRunnerCommandRequest,
    service: RunnerControlServiceDependency,
    authorized: RunnerDependency,
) -> FinishRunnerCommandResponse:
    _require_legacy_finish_route_policy(
        node_id=authorized.node_id,
        runner_principal=authorized.principal,
        command_id=command_id,
        lease_id=payload.lease_id,
    )
    command = await service.record_legacy_stop_ack(
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
        state_version=command.state_version,
        completed_at=command.completed_at,
    )


@router.post(
    "/commands/{command_id}/finish-owned",
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
        state_version=payload.state_version,
        envelope_digest=payload.envelope_digest,
        binding_digest=payload.binding_digest,
        succeeded=payload.succeeded,
        result=payload.result,
        error=payload.error,
    )
    return FinishRunnerCommandResponse(
        id=command.id,
        status=command.status,
        state_version=command.state_version,
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
        state_version=payload.state_version,
        envelope_digest=payload.envelope_digest,
        binding_digest=payload.binding_digest,
    )
    if command.lease_expires_at is None:
        raise RuntimeError("renewed Runner command omitted its lease expiry")
    if command.ownership is None:
        raise RuntimeError("renewed Runner command omitted its ownership envelope")
    return RenewRunnerCommandLeaseResponse(
        id=command.id,
        state_version=command.state_version,
        envelope_digest=command.ownership.envelope_digest,
        binding_digest=command.ownership.effect_binding.binding_digest,
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
        state_version=payload.state_version,
        envelope_digest=payload.envelope_digest,
        binding_digest=payload.binding_digest,
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
    execution = await service.report_execution(
        authorized.node_id,
        authorized.token,
        execution_id,
        report,
        command_id=payload.command_id,
        effect_binding_id=payload.effect_binding_id,
        envelope_digest=payload.envelope_digest,
        binding_digest=payload.binding_digest,
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
    next_offset = await service.append_output(
        authorized.node_id,
        authorized.token,
        execution_id,
        command_id=payload.command_id,
        effect_binding_id=payload.effect_binding_id,
        envelope_digest=payload.envelope_digest,
        binding_digest=payload.binding_digest,
        stream=payload.stream,
        offset=payload.offset,
        data=payload.data,
    )
    return ExecutionOutputReportResponse(next_offset=next_offset)
