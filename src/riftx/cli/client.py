"""Synchronous HTTP/SSE client shared by CLI command and interactive modes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

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

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._json("POST", f"/api/v1/runs/{run_id}/cancel")

    def compact_run(self, run_id: str, *, max_history_items: int = 100) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/v1/runs/{run_id}/compact",
            json={"max_history_items": max_history_items},
        )

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

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/executions/{execution_id}")

    def list_executions(
        self,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/api/v1/runs/{run_id}/executions",
            params={"limit": limit, "offset": offset},
        )

    def cancel_execution(self, execution_id: str) -> dict[str, Any]:
        return self._json("POST", f"/api/v1/executions/{execution_id}/cancel")

    def register_artifact(
        self,
        run_id: str,
        source_path: str,
        *,
        name: str | None = None,
        mime_type: str | None = None,
        description: str = "",
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, object] = {
            "source_path": source_path,
            "description": description,
        }
        if name is not None:
            payload["name"] = name
        if mime_type is not None:
            payload["mime_type"] = mime_type
        if execution_id is not None:
            payload["execution_id"] = execution_id
        return self._json("POST", f"/api/v1/runs/{run_id}/artifacts", json=payload)

    def list_artifacts(
        self,
        run_id: str,
        *,
        execution_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if execution_id is not None:
            params["execution_id"] = execution_id
        return self._json(
            "GET",
            f"/api/v1/runs/{run_id}/artifacts",
            params=params,
        )

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/artifacts/{artifact_id}")

    def generate_reports(
        self,
        run_id: str,
        *,
        formats: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/v1/runs/{run_id}/reports",
            json={"formats": formats or ["markdown", "html", "json"]},
        )

    def list_reports(
        self,
        run_id: str,
        *,
        format: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if format is not None:
            params["format"] = format
        return self._json("GET", f"/api/v1/runs/{run_id}/reports", params=params)

    def get_report(self, report_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/reports/{report_id}")

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

    def list_nodes(self, *, status: str | None = None) -> dict[str, Any]:
        params = {"status": status} if status else None
        return self._json("GET", "/api/v1/nodes", params=params)

    def get_node(self, node_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/nodes/{node_id}")

    def register_node(
        self,
        *,
        node_id: str,
        name: str,
        platform: str,
        architecture: str,
        runner_version: str = "unknown",
        capabilities: list[str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/api/v1/nodes/register",
            json={
                "node_id": node_id,
                "name": name,
                "platform": platform,
                "architecture": architecture,
                "runner_version": runner_version,
                "capabilities": capabilities or [],
                "labels": labels or {},
            },
        )

    def disconnect_node(self, node_id: str) -> dict[str, Any]:
        return self._json("POST", f"/api/v1/nodes/{node_id}/disconnect")

    def list_tools(self, node_id: str = "local") -> dict[str, Any]:
        return self._json("GET", f"/api/v1/nodes/{node_id}/tools")

    def refresh_tools(self, node_id: str = "local") -> dict[str, Any]:
        return self._json("POST", f"/api/v1/nodes/{node_id}/refresh-tools")

    def list_approvals(
        self,
        run_id: str,
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        params = {"status": status} if status else None
        return self._json(
            "GET",
            f"/api/v1/runs/{run_id}/approvals",
            params=params,
        )

    def approve(
        self,
        approval_id: str,
        *,
        approve_for_run: bool = False,
        decided_by: str = "local-user",
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/v1/approvals/{approval_id}/approve",
            json={
                "decided_by": decided_by,
                "approve_for_run": approve_for_run,
            },
        )

    def reject(
        self,
        approval_id: str,
        *,
        reason: str | None = None,
        decided_by: str = "local-user",
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/v1/approvals/{approval_id}/reject",
            json={"decided_by": decided_by, "reason": reason},
        )

    def create_terminal(
        self,
        run_id: str,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        cols: int = 120,
        rows: int = 40,
        owner: str = "agent",
    ) -> dict[str, Any]:
        payload: dict[str, object] = {
            "argv": argv or [],
            "cols": cols,
            "rows": rows,
            "owner": owner,
        }
        if cwd is not None:
            payload["cwd"] = cwd
        return self._json(
            "POST",
            f"/api/v1/runs/{run_id}/terminals",
            json=payload,
        )

    def get_terminal(self, session_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/terminals/{session_id}")

    def close_terminal(self, session_id: str) -> dict[str, Any]:
        return self._json("DELETE", f"/api/v1/terminals/{session_id}")

    def terminal_websocket_url(self, session_id: str, *, cursor: int = 0) -> str:
        parsed = urlsplit(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        path = f"{base_path}/api/v1/terminals/{session_id}/ws"
        query = urlencode({"cursor": max(cursor, 0)})
        return urlunsplit((scheme, parsed.netloc, path, query, ""))

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
