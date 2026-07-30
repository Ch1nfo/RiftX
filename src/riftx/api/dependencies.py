"""FastAPI dependency accessors for the control-plane service container."""

from typing import Annotated

from fastapi import Depends, Request

from riftx.application.services import (
    ApprovalApplicationService,
    ArtifactApplicationService,
    EventApplicationService,
    ExecutionApplicationService,
    FindingApplicationService,
    NodeApplicationService,
    ReportApplicationService,
    RunApplicationService,
    RunnerControlService,
    TerminalApplicationService,
    ToolApplicationService,
)


def get_control_plane(request: Request) -> object:
    return request.app.state.control_plane


def get_run_service(request: Request) -> RunApplicationService:
    return request.app.state.control_plane.run_service


def get_event_service(request: Request) -> EventApplicationService:
    return request.app.state.control_plane.event_service


def get_execution_service(request: Request) -> ExecutionApplicationService:
    return request.app.state.control_plane.execution_service


def get_finding_service(request: Request) -> FindingApplicationService:
    return request.app.state.control_plane.finding_service


def get_node_service(request: Request) -> NodeApplicationService:
    return request.app.state.control_plane.node_service


def get_runner_control_service(request: Request) -> RunnerControlService:
    return request.app.state.control_plane.runner_control_service


def get_report_service(request: Request) -> ReportApplicationService:
    return request.app.state.control_plane.report_service


def get_tool_service(request: Request) -> ToolApplicationService:
    return request.app.state.control_plane.tool_service


def get_approval_service(request: Request) -> ApprovalApplicationService:
    return request.app.state.control_plane.approval_service


def get_artifact_service(request: Request) -> ArtifactApplicationService:
    return request.app.state.control_plane.artifact_service


def get_terminal_service(request: Request) -> TerminalApplicationService:
    return request.app.state.control_plane.terminal_service


RunServiceDependency = Annotated[RunApplicationService, Depends(get_run_service)]
EventServiceDependency = Annotated[EventApplicationService, Depends(get_event_service)]
ExecutionServiceDependency = Annotated[ExecutionApplicationService, Depends(get_execution_service)]
FindingServiceDependency = Annotated[FindingApplicationService, Depends(get_finding_service)]
NodeServiceDependency = Annotated[NodeApplicationService, Depends(get_node_service)]
ReportServiceDependency = Annotated[ReportApplicationService, Depends(get_report_service)]
RunnerControlServiceDependency = Annotated[
    RunnerControlService, Depends(get_runner_control_service)
]
ToolServiceDependency = Annotated[ToolApplicationService, Depends(get_tool_service)]
ApprovalServiceDependency = Annotated[
    ApprovalApplicationService,
    Depends(get_approval_service),
]
ArtifactServiceDependency = Annotated[
    ArtifactApplicationService,
    Depends(get_artifact_service),
]
TerminalServiceDependency = Annotated[
    TerminalApplicationService,
    Depends(get_terminal_service),
]
