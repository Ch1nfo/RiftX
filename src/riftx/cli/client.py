"""Synchronous HTTP/SSE client shared by CLI command and interactive modes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx


class RiftXAPIError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class SSEEvent:
    id: str | None
    event: str | None
    data: object | None


class APIClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> APIClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_run(self, payload: dict[str, object]) -> dict[str, Any]:
        return self._json("POST", "/api/v1/runs", json=payload)

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return self._json("GET", "/api/v1/runs", params=params)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/runs/{run_id}")

    def pause_run(self, run_id: str) -> dict[str, Any]:
        return self._json("POST", f"/api/v1/runs/{run_id}/pause")

    def resume_run(self, run_id: str) -> dict[str, Any]:
        return self._json("POST", f"/api/v1/runs/{run_id}/resume")

    def cancel_current_execution(self, run_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/v1/runs/{run_id}/cancel-current-execution",
        )

    def append_message(self, run_id: str, message: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/v1/runs/{run_id}/message",
            json={"message": message},
        )

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/api/v1/runs/{run_id}/events",
            params={"after_sequence": after_sequence, "limit": limit},
        )

    def stream_events(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        follow: bool = True,
    ) -> Iterator[SSEEvent]:
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id
        with self._client.stream(
            "GET",
            f"/api/v1/runs/{run_id}/events/stream",
            headers=headers,
            params={"follow": str(follow).lower()},
            timeout=None,
        ) as response:
            self._raise_for_error(response)
            yield from parse_sse_lines(response.iter_lines())

    def list_tools(self, node_id: str = "local") -> dict[str, Any]:
        return self._json("GET", f"/api/v1/nodes/{node_id}/tools")

    def refresh_tools(self, node_id: str = "local") -> dict[str, Any]:
        return self._json("POST", f"/api/v1/nodes/{node_id}/refresh-tools")

    def _json(self, method: str, path: str, **kwargs: object) -> dict[str, Any]:
        response = self._client.request(method, path, **kwargs)
        self._raise_for_error(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RiftXAPIError(
                status_code=response.status_code,
                code="invalid_response",
                message="RiftX API returned a non-object JSON response",
                details={"path": path},
            )
        return payload

    def _raise_for_error(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        code = "http_error"
        message = f"RiftX API returned HTTP {response.status_code}"
        details: object = {}
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            error = payload["error"]
            code = str(error.get("code", code))
            message = str(error.get("message", message))
            details = error.get("details", details)
        raise RiftXAPIError(
            status_code=response.status_code,
            code=code,
            message=message,
            details=details,
        )


def parse_sse_lines(lines: Iterator[str]) -> Iterator[SSEEvent]:
    """Parse an SSE text stream without coupling the CLI to FastAPI internals."""

    event_id: str | None = None
    event_name: str | None = None
    data_lines: list[str] = []

    for line in lines:
        if line == "":
            if event_id is not None or event_name is not None or data_lines:
                yield SSEEvent(
                    id=event_id,
                    event=event_name,
                    data=_decode_sse_data(data_lines),
                )
            event_id = None
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    if event_id is not None or event_name is not None or data_lines:
        yield SSEEvent(
            id=event_id,
            event=event_name,
            data=_decode_sse_data(data_lines),
        )


def _decode_sse_data(data_lines: list[str]) -> object | None:
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw
