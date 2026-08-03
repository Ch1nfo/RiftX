"""Durable Run event list and SSE stream endpoints."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from ..dependencies import AuthorizedRunReadDependency, EventServiceDependency
from ..errors import APIError
from ..schemas import ErrorResponse, RunEventListResponse, RunEventResponse

router = APIRouter(prefix="/runs/{run_id}/events", tags=["events"])


@router.get(
    "",
    response_model=RunEventListResponse,
    responses={404: {"model": ErrorResponse}},
)
async def list_events(
    run_id: str,
    service: EventServiceDependency,
    _authorized_run: AuthorizedRunReadDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> RunEventListResponse:
    events = await service.list_events(
        run_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    return RunEventListResponse(
        items=[RunEventResponse.from_domain(event) for event in events],
        after_sequence=after_sequence,
    )


async def _prepare_stream_cursor(
    _authorized_run: AuthorizedRunReadDependency,
    after_sequence: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> int:
    del _authorized_run
    cursor = _resolve_cursor(after_sequence, last_event_id)
    return cursor


EventCursorDependency = Annotated[int, Depends(_prepare_stream_cursor)]


@router.get(
    "/stream",
    response_class=EventSourceResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def stream_events(
    run_id: str,
    request: Request,
    service: EventServiceDependency,
    cursor: EventCursorDependency,
    follow: bool = True,
) -> AsyncIterator[ServerSentEvent]:
    settings = request.app.state.control_plane.settings
    poll_interval = settings.sse_poll_interval_seconds
    heartbeat_interval = settings.sse_heartbeat_seconds
    last_sent_at = time.monotonic()

    while True:
        events = await service.list_events(
            run_id,
            after_sequence=cursor,
            limit=1000,
            require_run=False,
        )
        for event in events:
            cursor = event.sequence
            last_sent_at = time.monotonic()
            response = RunEventResponse.from_domain(event)
            yield ServerSentEvent(
                id=str(event.sequence),
                event=event.event_type,
                data=response.model_dump(mode="json"),
            )

        if not follow or await request.is_disconnected():
            return

        now = time.monotonic()
        if now - last_sent_at >= heartbeat_interval:
            last_sent_at = now
            yield ServerSentEvent(comment="heartbeat")
        await asyncio.sleep(poll_interval)


def _resolve_cursor(after_sequence: int | None, last_event_id: str | None) -> int:
    cursor = after_sequence or 0
    if last_event_id is None or not last_event_id.strip():
        return cursor
    try:
        header_cursor = int(last_event_id)
    except ValueError as exc:
        raise APIError(
            400,
            "invalid_last_event_id",
            "Last-Event-ID must be a non-negative integer",
        ) from exc
    if header_cursor < 0:
        raise APIError(
            400,
            "invalid_last_event_id",
            "Last-Event-ID must be a non-negative integer",
        )
    return max(cursor, header_cursor)
