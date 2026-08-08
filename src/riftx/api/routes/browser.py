"""Managed browser REST and observation stream endpoints."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from riftx.application.errors import (
    ApplicationServiceError,
    EntityNotFoundError,
    resource_not_accessible,
)
from riftx.application.services.runs import require_interactive_run_operation
from riftx.domain.errors import DomainError

from ..auth import accept_local_operator_websocket
from ..dependencies import (
    BrowserServiceDependency,
    RunReadAuthorizerDependency,
    RunServiceDependency,
    load_authorized_child,
    require_run_read_binding,
    websocket_run_read_authorizer,
)
from ..schemas.browser import (
    BrowserActionRequest,
    BrowserObserveRequest,
    BrowserSessionCreateRequest,
    BrowserViewResponse,
)
from ..schemas.errors import ErrorResponse

router = APIRouter(prefix="/browser/sessions", tags=["browser"])


@router.post(
    "",
    response_model=BrowserViewResponse,
    status_code=201,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def open_browser(
    request: BrowserSessionCreateRequest,
    service: BrowserServiceDependency,
    runs: RunServiceDependency,
) -> BrowserViewResponse:
    require_interactive_run_operation(await runs.get_run(request.run_id))
    return BrowserViewResponse.from_view(await service.open(request.to_command()))


@router.get(
    "/{session_id}",
    response_model=BrowserViewResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def get_browser(
    session_id: str,
    service: BrowserServiceDependency,
    authorizer: RunReadAuthorizerDependency,
) -> BrowserViewResponse:
    run_id = await service.resolve_run_id(session_id)
    authorized_run = await authorizer.require(run_id)
    require_interactive_run_operation(authorized_run)
    view = await load_authorized_child(
        service.get(session_id, expected_run_id=run_id)
    )
    require_run_read_binding(run_id, view.session.run_id)
    return BrowserViewResponse.from_view(view)


@router.delete(
    "/{session_id}",
    response_model=BrowserViewResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def close_browser(
    session_id: str,
    service: BrowserServiceDependency,
    runs: RunServiceDependency,
) -> BrowserViewResponse:
    await _require_interactive_session_operation(session_id, service, runs)
    return BrowserViewResponse.from_view(await service.close(session_id))


@router.post(
    "/{session_id}/observe",
    response_model=BrowserViewResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def observe_browser(
    session_id: str,
    request: BrowserObserveRequest,
    service: BrowserServiceDependency,
    runs: RunServiceDependency,
) -> BrowserViewResponse:
    await _require_interactive_session_operation(session_id, service, runs)
    return BrowserViewResponse.from_view(await service.observe(session_id, **request.model_dump()))


@router.post(
    "/{session_id}/actions",
    response_model=BrowserViewResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def act_browser(
    session_id: str,
    request: BrowserActionRequest,
    service: BrowserServiceDependency,
    runs: RunServiceDependency,
) -> BrowserViewResponse:
    await _require_interactive_session_operation(session_id, service, runs)
    return BrowserViewResponse.from_view(await service.act(session_id, request.to_command()))


@router.post(
    "/{session_id}/takeover",
    response_model=BrowserViewResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def takeover_browser(
    session_id: str,
    service: BrowserServiceDependency,
    runs: RunServiceDependency,
) -> BrowserViewResponse:
    await _require_interactive_session_operation(session_id, service, runs)
    return BrowserViewResponse.from_view(await service.takeover(session_id))


@router.post(
    "/{session_id}/release",
    response_model=BrowserViewResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def release_browser(
    session_id: str,
    service: BrowserServiceDependency,
    runs: RunServiceDependency,
) -> BrowserViewResponse:
    await _require_interactive_session_operation(session_id, service, runs)
    return BrowserViewResponse.from_view(await service.release(session_id))


@router.websocket("/{session_id}/stream")
async def stream_browser(session_id: str, websocket: WebSocket) -> None:
    await accept_local_operator_websocket(websocket)
    service = websocket.app.state.control_plane.browser_service
    authorizer = websocket_run_read_authorizer(websocket)
    send_lock = asyncio.Lock()

    async def send(payload: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    try:
        run_id = await service.resolve_run_id(session_id)
        authorized_run = await authorizer.require(run_id)
        require_interactive_run_operation(authorized_run)
        initial = await service.get(session_id, expected_run_id=run_id)
        require_run_read_binding(run_id, initial.session.run_id)
    except EntityNotFoundError:
        error = resource_not_accessible()
        await send({"type": "error", "code": error.code, "message": error.message})
        await websocket.close(code=4409)
        return
    except (ApplicationServiceError, DomainError, ValueError) as exc:
        await send(
            {
                "type": "error",
                "code": getattr(exc, "code", "browser_unavailable"),
                "message": str(exc),
            }
        )
        await websocket.close(code=4409)
        return

    version = initial.observation.observation_version if initial.observation else 0
    await send(
        {
            "type": "browser_state",
            "state": BrowserViewResponse.from_view(initial).model_dump(mode="json"),
        }
    )

    async def stream_observations() -> None:
        nonlocal version
        while True:
            observations = await service.observations_after(session_id, version, limit=100)
            for observation in observations:
                version = observation.observation_version
                await send(
                    {
                        "type": "browser_observation",
                        "observation": observation.model_dump(mode="json"),
                    }
                )
            await asyncio.sleep(0.25)

    async def receive_controls() -> None:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            try:
                if message_type == "observe":
                    view = await service.observe(
                        session_id,
                        page_id=message.get("page_id"),
                        include_screenshot=bool(message.get("include_screenshot", False)),
                        include_network=bool(message.get("include_network", True)),
                    )
                    await send(
                        {
                            "type": "browser_state",
                            "state": BrowserViewResponse.from_view(view).model_dump(mode="json"),
                        }
                    )
                elif message_type == "takeover":
                    view = await service.takeover(session_id)
                    await send(
                        {
                            "type": "browser_state",
                            "state": BrowserViewResponse.from_view(view).model_dump(mode="json"),
                        }
                    )
                elif message_type == "release":
                    view = await service.release(session_id)
                    await send(
                        {
                            "type": "browser_takeover_summary",
                            "summary": (
                                view.takeover_summary.model_dump(mode="json")
                                if view.takeover_summary
                                else None
                            ),
                        }
                    )
                    await send(
                        {
                            "type": "browser_state",
                            "state": BrowserViewResponse.from_view(view).model_dump(mode="json"),
                        }
                    )
                elif message_type == "close":
                    view = await service.close(session_id)
                    await send(
                        {
                            "type": "browser_state",
                            "state": BrowserViewResponse.from_view(view).model_dump(mode="json"),
                        }
                    )
                    return
                elif message_type == "ping":
                    await send({"type": "pong"})
                else:
                    raise ValueError(f"unknown browser message type {message_type!r}")
            except (ApplicationServiceError, DomainError, KeyError, TypeError, ValueError) as exc:
                await send(
                    {
                        "type": "error",
                        "code": getattr(exc, "code", "browser_message_invalid"),
                        "message": str(exc),
                    }
                )

    try:
        output_task = asyncio.create_task(stream_observations())
        input_task = asyncio.create_task(receive_controls())
        done, pending = await asyncio.wait(
            {output_task, input_task}, return_when=asyncio.FIRST_COMPLETED
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


async def _require_interactive_session_operation(
    session_id: str,
    service: BrowserServiceDependency,
    runs: RunServiceDependency,
) -> None:
    current = await service.get(session_id)
    require_interactive_run_operation(await runs.get_run(current.session.run_id))
