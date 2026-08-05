"""Durable Run event list and SSE stream endpoints."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from ..dependencies import (
    AuthorizedRunReadDependency,
    EventServiceDependency,
    RunReadAuthorizationSnapshot,
    RunReadAuthorizerDependency,
    require_run_read_binding,
)
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


@dataclass(frozen=True, slots=True)
class _EventStreamAdmission:
    cursor: int
    authorization: RunReadAuthorizationSnapshot


async def _prepare_stream_cursor(
    _authorized_run: AuthorizedRunReadDependency,
    authorizer: RunReadAuthorizerDependency,
    after_sequence: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> _EventStreamAdmission:
    cursor = _resolve_cursor(after_sequence, last_event_id)
    authorization = await authorizer.require_stream_snapshot(_authorized_run.id)
    require_run_read_binding(_authorized_run.id, authorization.run_id)
    return _EventStreamAdmission(cursor=cursor, authorization=authorization)


EventStreamAdmissionDependency = Annotated[
    _EventStreamAdmission,
    Depends(_prepare_stream_cursor),
]


@router.get(
    "/stream",
    response_class=EventSourceResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def stream_events(
    run_id: str,
    request: Request,
    service: EventServiceDependency,
    admission: EventStreamAdmissionDependency,
    authorizer: RunReadAuthorizerDependency,
    follow: bool = True,
) -> AsyncIterator[ServerSentEvent]:
    settings = request.app.state.control_plane.settings
    poll_interval = settings.sse_poll_interval_seconds
    heartbeat_interval = settings.sse_heartbeat_seconds
    cursor = admission.cursor
    last_sent_at = time.monotonic()

    while True:
        await authorizer.revalidate_stream_snapshot(
            request,
            admission.authorization,
        )
        events = await service.list_events(
            run_id,
            after_sequence=cursor,
            limit=1000,
            require_run=False,
        )
        responses: list[tuple[int, str, RunEventResponse]] = []
        for event in events:
            require_run_read_binding(admission.authorization.run_id, event.run_id)
            responses.append(
                (
                    event.sequence,
                    event.event_type,
                    RunEventResponse.from_domain(event),
                )
            )

        for sequence, event_type, response in responses:
            cursor = sequence
            last_sent_at = time.monotonic()
            yield ServerSentEvent(
                id=str(sequence),
                event=event_type,
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
