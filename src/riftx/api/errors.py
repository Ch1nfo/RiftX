"""Unified error handling for every control-plane endpoint."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from riftx.application.actions import InvalidActionCursorError
from riftx.application.errors import (
    ApplicationConflictError,
    AuthenticationError,
    AuthorizationError,
    EntityNotFoundError,
    RepositoryConflictError,
    ResourceNotAccessibleError,
    ServiceUnavailableError,
)
from riftx.application.graphs import (
    InvalidGraphCursorError,
    StaleGraphCursorError,
    UnsupportedGraphViewError,
)
from riftx.application.traffic import (
    InvalidTrafficCursorError,
    StaleTrafficCursorError,
    TrafficSourceContractError,
)
from riftx.domain import InvalidStateTransitionError

from .schemas import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


class APIError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, object] | list[object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(APIError, _handle_api_error)
    app.add_exception_handler(EntityNotFoundError, _handle_not_found)
    app.add_exception_handler(ApplicationConflictError, _handle_application_conflict)
    app.add_exception_handler(AuthenticationError, _handle_authentication)
    app.add_exception_handler(AuthorizationError, _handle_authorization)
    app.add_exception_handler(ResourceNotAccessibleError, _handle_resource_not_accessible)
    app.add_exception_handler(RepositoryConflictError, _handle_repository_conflict)
    app.add_exception_handler(ServiceUnavailableError, _handle_service_unavailable)
    app.add_exception_handler(InvalidStateTransitionError, _handle_invalid_transition)
    app.add_exception_handler(InvalidActionCursorError, _handle_invalid_action_cursor)
    app.add_exception_handler(InvalidGraphCursorError, _handle_invalid_graph_cursor)
    app.add_exception_handler(StaleGraphCursorError, _handle_stale_graph_cursor)
    app.add_exception_handler(UnsupportedGraphViewError, _handle_unsupported_graph_view)
    app.add_exception_handler(InvalidTrafficCursorError, _handle_invalid_traffic_cursor)
    app.add_exception_handler(StaleTrafficCursorError, _handle_stale_traffic_cursor)
    app.add_exception_handler(TrafficSourceContractError, _handle_traffic_source_contract)
    app.add_exception_handler(RequestValidationError, _handle_validation)
    app.add_exception_handler(HTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)


async def _handle_api_error(_: Request, exc: Exception) -> JSONResponse:
    error = _expect(exc, APIError)
    return _response(error.status_code, error.code, error.message, error.details)


async def _handle_not_found(_: Request, exc: Exception) -> JSONResponse:
    error = _expect(exc, EntityNotFoundError)
    code = f"{error.entity.lower().replace(' ', '_')}_not_found"
    return _response(
        404,
        code,
        str(error),
        {"entity": error.entity, "entity_id": error.entity_id},
    )


async def _handle_authentication(_: Request, exc: Exception) -> JSONResponse:
    error = _expect(exc, AuthenticationError)
    return _response(401, error.code, error.message, error.details)


async def _handle_authorization(_: Request, exc: Exception) -> JSONResponse:
    error = _expect(exc, AuthorizationError)
    return _response(403, error.code, error.message, error.details)


async def _handle_resource_not_accessible(_: Request, exc: Exception) -> JSONResponse:
    error = _expect(exc, ResourceNotAccessibleError)
    return _response(404, error.code, error.message, error.details)


async def _handle_application_conflict(_: Request, exc: Exception) -> JSONResponse:
    error = _expect(exc, ApplicationConflictError)
    return _response(409, error.code, error.message, error.details)


async def _handle_repository_conflict(_: Request, exc: Exception) -> JSONResponse:
    return _response(409, "repository_conflict", str(exc))


async def _handle_service_unavailable(_: Request, exc: Exception) -> JSONResponse:
    error = _expect(exc, ServiceUnavailableError)
    return _response(503, error.code, error.message, error.details)


async def _handle_invalid_transition(_: Request, exc: Exception) -> JSONResponse:
    return _response(409, "invalid_state_transition", str(exc))


async def _handle_invalid_action_cursor(_: Request, exc: Exception) -> JSONResponse:
    _expect(exc, InvalidActionCursorError)
    return _response(422, "invalid_action_cursor", "The Action cursor is invalid")


async def _handle_invalid_graph_cursor(_: Request, exc: Exception) -> JSONResponse:
    _expect(exc, InvalidGraphCursorError)
    return _response(422, "invalid_graph_cursor", "The Graph cursor is invalid")


async def _handle_stale_graph_cursor(_: Request, exc: Exception) -> JSONResponse:
    _expect(exc, StaleGraphCursorError)
    return _response(409, "stale_graph_cursor", "The Graph cursor is stale")


async def _handle_unsupported_graph_view(_: Request, exc: Exception) -> JSONResponse:
    _expect(exc, UnsupportedGraphViewError)
    return _response(422, "unsupported_graph_view", "The Graph view is unavailable")


async def _handle_invalid_traffic_cursor(_: Request, exc: Exception) -> JSONResponse:
    _expect(exc, InvalidTrafficCursorError)
    return _response(422, "invalid_traffic_cursor", "The Traffic cursor is invalid")


async def _handle_stale_traffic_cursor(_: Request, exc: Exception) -> JSONResponse:
    _expect(exc, StaleTrafficCursorError)
    return _response(409, "stale_traffic_cursor", "The Traffic cursor is stale")


async def _handle_traffic_source_contract(_: Request, exc: Exception) -> JSONResponse:
    _expect(exc, TrafficSourceContractError)
    return _response(
        500,
        "traffic_source_contract_error",
        "Traffic metadata is temporarily unavailable",
    )


async def _handle_validation(_: Request, exc: Exception) -> JSONResponse:
    error = _expect(exc, RequestValidationError)
    sensitive_values = _sensitive_values(error.body)
    return _response(
        422,
        "validation_error",
        "Request validation failed",
        _redact_validation_errors(
            jsonable_encoder(error.errors()),
            sensitive_values=sensitive_values,
        ),
    )


async def _handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    error = _expect(exc, HTTPException)
    message = str(error.detail)
    details: dict[str, object] | list[object] = {}
    if isinstance(error.detail, dict):
        message = str(error.detail.get("message", message))
        details = error.detail
    return _response(error.status_code, "http_error", message, details)


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled control-plane error",
        extra={"method": request.method, "path": request.url.path},
        exc_info=exc,
    )
    return _response(500, "internal_error", "An unexpected internal error occurred")


def _response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | list[object] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message, details=details or {}))
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def _expect[ErrorT: Exception](exc: Exception, expected: type[ErrorT]) -> ErrorT:
    if not isinstance(exc, expected):
        raise TypeError(f"expected {expected.__name__}, got {type(exc).__name__}")
    return exc


def _redact_validation_errors(
    errors: list[object],
    *,
    sensitive_values: tuple[str, ...] = (),
) -> list[object]:
    redacted: list[object] = []
    for item in errors:
        if not isinstance(item, Mapping):
            redacted.append(item)
            continue
        copied = dict(item)
        copied.pop("ctx", None)
        location = copied.get("loc")
        external_value = (
            isinstance(location, (list, tuple))
            and bool(location)
            and location[0] in {"path", "query"}
        )
        sensitive_field = isinstance(location, (list, tuple)) and any(
            _redact_validation_field(part) for part in location if isinstance(part, str)
        )
        if external_value or sensitive_field:
            copied["input"] = "[redacted]"
        else:
            copied["input"] = _redact_sensitive_mapping(copied.get("input"))
        redacted.append(_redact_sensitive_literals(copied, sensitive_values))
    return redacted


def _redact_sensitive_mapping(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]"
            if _is_sensitive_field(str(key))
            else _redact_sensitive_mapping(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_mapping(item) for item in value)
    return value


def _sensitive_values(value: object) -> tuple[str, ...]:
    collected: set[str] = set()

    def visit(item: object, *, sensitive: bool = False) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                visit(child, sensitive=sensitive or _is_sensitive_field(str(key)))
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child, sensitive=sensitive)
            return
        if sensitive and isinstance(item, str) and item:
            collected.add(item)

    visit(value)
    return tuple(sorted(collected, key=len, reverse=True))


def _redact_sensitive_literals(value: object, sensitive_values: tuple[str, ...]) -> object:
    if isinstance(value, str):
        redacted = value
        for sensitive in sensitive_values:
            redacted = redacted.replace(sensitive, "[redacted]")
        return redacted
    if isinstance(value, Mapping):
        return {
            str(key): _redact_sensitive_literals(item, sensitive_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_literals(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_literals(item, sensitive_values) for item in value)
    return value


def _is_sensitive_field(name: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", name.lower())
    return compact in {"env", "environment"} or compact.endswith(
        ("apikey", "password", "secret", "token")
    )


def _redact_validation_field(name: str) -> bool:
    return _is_sensitive_field(name) or name in {
        "node_type",
        "edge_type",
        "focus",
        "search",
        "method",
        "status_class",
        "cursor",
        "exchange_id",
    }
