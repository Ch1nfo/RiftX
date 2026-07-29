"""FastAPI dependency accessors for the control-plane service container."""

from typing import Annotated

from fastapi import Depends, Request

from riftx.application.services import (
    EventApplicationService,
    FindingApplicationService,
    RunApplicationService,
    ToolApplicationService,
)


def get_control_plane(request: Request) -> object:
    return request.app.state.control_plane


def get_run_service(request: Request) -> RunApplicationService:
    return request.app.state.control_plane.run_service


def get_event_service(request: Request) -> EventApplicationService:
    return request.app.state.control_plane.event_service


def get_finding_service(request: Request) -> FindingApplicationService:
    return request.app.state.control_plane.finding_service


def get_tool_service(request: Request) -> ToolApplicationService:
    return request.app.state.control_plane.tool_service


RunServiceDependency = Annotated[RunApplicationService, Depends(get_run_service)]
EventServiceDependency = Annotated[EventApplicationService, Depends(get_event_service)]
FindingServiceDependency = Annotated[FindingApplicationService, Depends(get_finding_service)]
ToolServiceDependency = Annotated[ToolApplicationService, Depends(get_tool_service)]
