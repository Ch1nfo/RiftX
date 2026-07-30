"""Interactive terminal REST lifecycle and WebSocket transport."""

from __future__ import annotations

import asyncio
import codecs
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from riftx.application.errors import (
    ApplicationServiceError,
    EntityNotFoundError,
)
from riftx.domain import DomainError, TerminalOwner, TerminalStatus

from ..dependencies import TerminalServiceDependency
from ..schemas import ErrorResponse, TerminalCreateRequest, TerminalResponse

router = APIRouter(tags=["terminals"])

_ERROR_RESPONSES = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.post(
    "/runs/{run_id}/terminals",
    response_model=TerminalResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def create_terminal(
    run_id: str,
    request: TerminalCreateRequest,
    service: TerminalServiceDependency,
) -> TerminalResponse:
    return TerminalResponse.from_view(await service.create(run_id, request.to_command()))


@router.get(
    "/terminals/{session_id}",
    response_model=TerminalResponse,
    responses=_ERROR_RESPONSES,
)
async def get_terminal(
    session_id: str,
    service: TerminalServiceDependency,
) -> TerminalResponse:
    return TerminalResponse.from_view(await service.get(session_id))


@router.delete(
    "/terminals/{session_id}",
    response_model=TerminalResponse,
    responses=_ERROR_RESPONSES,
)
async def close_terminal(
    session_id: str,
    service: TerminalServiceDependency,
) -> TerminalResponse:
    return TerminalResponse.from_view(await service.close(session_id))


@router.websocket("/terminals/{session_id}/ws")
async def terminal_websocket(websocket: WebSocket, session_id: str) -> None:
    service = websocket.app.state.control_plane.terminal_service
    cursor = _cursor(websocket.query_params.get("cursor"))
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send(payload: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def send_state() -> TerminalResponse:
        response = TerminalResponse.from_view(await service.get(session_id))
        await send({"type": "state", "session": response.model_dump(mode="json")})
        return response

    async def stream_output(
        previous_state: tuple[TerminalStatus, TerminalOwner, int, int],
    ) -> None:
        nonlocal cursor
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            view = await service.get(session_id)
            state = (
                view.terminal.status,
                view.terminal.owner,
                view.terminal.cols,
                view.terminal.rows,
            )
            if state != previous_state:
                await send_state()
                previous_state = state
            try:
                output = await service.read(session_id, cursor=cursor)
            except ValueError as exc:
                await send(
                    {
                        "type": "error",
                        "code": "terminal_cursor_invalid",
                        "message": str(exc),
                    }
                )
                return
            terminal_finished = (
                view.terminal.status in {TerminalStatus.CLOSED, TerminalStatus.LOST} and output.eof
            )
            decoded = decoder.decode(output.data, final=terminal_finished)
            if output.data:
                cursor = output.next_cursor
            if decoded:
                await send(
                    {
                        "type": "output",
                        "data": decoded,
                        "cursor": output.cursor,
                        "next_cursor": output.next_cursor,
                    }
                )
            if terminal_finished:
                return
            await asyncio.sleep(0.05)

    async def receive_input() -> None:
        while True:
            message = await websocket.receive_json()
            try:
                message_type = str(message.get("type") or "")
                if message_type == "input":
                    raw_data = message.get("data")
                    if not isinstance(raw_data, str):
                        raise TypeError("terminal input data must be a string")
                    data = raw_data.encode()
                    if len(data) > 64 * 1024:
                        raise ValueError("terminal input exceeds 65536 bytes")
                    await service.write(session_id, data, actor=TerminalOwner.USER)
                elif message_type == "resize":
                    await service.resize(
                        session_id,
                        cols=int(message["cols"]),
                        rows=int(message["rows"]),
                    )
                    await send_state()
                elif message_type == "interrupt":
                    await service.interrupt(session_id, actor=TerminalOwner.USER)
                elif message_type == "takeover":
                    await service.take_over(session_id)
                    await send_state()
                elif message_type == "release":
                    released = await service.release(session_id)
                    if released.takeover_summary is not None:
                        await send(
                            {
                                "type": "terminal_takeover_summary",
                                "summary": released.takeover_summary.model_dump(mode="json"),
                            }
                        )
                    await send_state()
                elif message_type == "ping":
                    await send({"type": "pong"})
                else:
                    raise ValueError(f"unknown terminal message type {message_type!r}")
            except (ApplicationServiceError, DomainError, KeyError, TypeError, ValueError) as exc:
                await send(
                    {
                        "type": "error",
                        "code": getattr(exc, "code", "terminal_message_invalid"),
                        "message": str(exc),
                    }
                )

    try:
        initial = await send_state()
    except EntityNotFoundError as exc:
        await send(
            {
                "type": "error",
                "code": "terminal_session_not_found",
                "message": str(exc),
            }
        )
        await websocket.close(code=4404)
        return
    except (ApplicationServiceError, DomainError, ValueError) as exc:
        await send(
            {
                "type": "error",
                "code": getattr(exc, "code", "terminal_unavailable"),
                "message": str(exc),
            }
        )
        await websocket.close(code=4409)
        return

    initial_state = (initial.status, initial.owner, initial.cols, initial.rows)
    try:
        output_task = asyncio.create_task(stream_output(initial_state))
        input_task = asyncio.create_task(receive_input())
        done, pending = await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        return
    finally:
        with suppress(RuntimeError):
            await websocket.close()


def _cursor(value: str | None) -> int:
    if value is None:
        return 0
    try:
        cursor = int(value)
    except ValueError:
        return 0
    return max(cursor, 0)
