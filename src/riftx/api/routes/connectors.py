"""Unified API consumed by browser DevTools and Burp extensions."""

from __future__ import annotations

import ipaddress
from dataclasses import replace
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import RedirectResponse

from riftx.application.errors import ServiceUnavailableError
from riftx.application.services.runs import require_general_run_operation
from riftx.domain import RunKind, RunStatus, Scope

from ..dependencies import (
    AuthorizedRunReadDependency,
    ConnectorServiceDependency,
    RunServiceDependency,
    ToolServiceDependency,
)
from ..schemas import ErrorResponse, RunActionResponse, RunListResponse, RunResponse
from ..schemas.connectors import (
    ConnectorReceiptResponse,
    ConnectorSubmissionRequest,
    ConnectorWebUIResponse,
)

router = APIRouter(prefix="/connectors", tags=["connectors"])

_ERROR_RESPONSES = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.post(
    "/submissions",
    response_model=ConnectorReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def submit_http_capture(
    payload: ConnectorSubmissionRequest,
    connector: ConnectorServiceDependency,
    runs: RunServiceDependency,
    tools: ToolServiceDependency,
) -> ConnectorReceiptResponse:
    created = False
    run_id = payload.run_id
    if payload.new_run is not None:
        command = payload.new_run.to_command(default_node_id=tools.node_id)
        if not _has_positive_scope(command.scope):
            command = replace(
                command, scope=_scope_for_url(payload.capture.url, command.scope)
            )
        try:
            run = await runs.create_run(command)
        except ServiceUnavailableError as exc:
            saved_run_id = exc.details.get("run_id")
            if not isinstance(saved_run_id, str) or not saved_run_id:
                raise
            run = await runs.get_run(saved_run_id)
        run_id = run.id
        created = True
    assert run_id is not None
    require_general_run_operation(await runs.get_run(run_id))
    receipt = await connector.ingest(run_id, payload.capture, created_run=created)
    return ConnectorReceiptResponse(receipt=receipt)


@router.get("/runs", response_model=RunListResponse, responses=_ERROR_RESPONSES)
async def list_connector_runs(
    runs: RunServiceDependency,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListResponse:
    items = await runs.list_runs(
        status=run_status,
        kind=RunKind.GENERAL,
        limit=limit,
        offset=offset,
    )
    return RunListResponse(
        items=[RunResponse.from_domain(item) for item in items],
        limit=limit,
        offset=offset,
    )


@router.get("/runs/{run_id}/events", responses=_ERROR_RESPONSES)
async def connector_events(
    run_id: str,
    authorized_run: AuthorizedRunReadDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
) -> RedirectResponse:
    require_general_run_operation(authorized_run)
    return RedirectResponse(
        url=(
            f"/api/v1/runs/{run_id}/events/stream?"
            f"after_sequence={after_sequence}&follow=true"
        ),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def cancel_connector_run(
    run_id: str, runs: RunServiceDependency
) -> RunActionResponse:
    require_general_run_operation(await runs.get_run(run_id))
    return RunActionResponse(run=RunResponse.from_domain(await runs.cancel(run_id)))


@router.get(
    "/runs/{run_id}/webui",
    response_model=ConnectorWebUIResponse,
    responses=_ERROR_RESPONSES,
)
async def connector_webui(
    run_id: str,
    request: Request,
    authorized_run: AuthorizedRunReadDependency,
) -> ConnectorWebUIResponse:
    require_general_run_operation(authorized_run)
    base = str(request.base_url).rstrip("/")
    return ConnectorWebUIResponse(run_id=run_id, url=f"{base}/runs/{run_id}")


def _has_positive_scope(scope: Scope) -> bool:
    return bool(scope.cidrs or scope.ips or scope.domains or scope.url_prefixes)


def _scope_for_url(url: str, base: Scope | None = None) -> Scope:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("connector capture URL is invalid")
    host = parsed.hostname.lower().rstrip(".")
    scope = base or Scope()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return scope.model_copy(update={"domains": [host]})
    return scope.model_copy(update={"ips": [host]})
