"""Outbound HTTP client used by independently deployed RiftX Runners."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from riftx.application.services import NodeHeartbeat, NodeRegistration
from riftx.domain import ExecutionStatus, RunnerCommandKind


class RunnerControlClientError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}


class OutputOffsetMismatch(RunnerControlClientError):
    @property
    def expected_offset(self) -> int:
        value = self.details.get("expected_offset")
        if not isinstance(value, int):
            raise RuntimeError("offset mismatch response omitted expected_offset")
        return value


@dataclass(frozen=True, slots=True)
class LeasedRunnerCommand:
    id: str
    kind: RunnerCommandKind
    payload: dict[str, object]
    lease_id: str
    attempts: int


class RunnerCredentialStore:
    """Persists only the node-scoped credential returned after registration."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, node_id: str) -> str | None:
        try:
            payload = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if payload.get("node_id") != node_id:
            return None
        token = payload.get("runner_token")
        return token if isinstance(token, str) and token else None

    def save(self, node_id: str, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps({"node_id": node_id, "runner_token": token}))
        if os.name == "posix":
            temporary.chmod(0o600)
        temporary.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class RunnerControlClient:
    """Authenticates and speaks the reconnect-safe remote Runner protocol."""

    def __init__(
        self,
        *,
        server_url: str,
        node_id: str,
        credentials: RunnerCredentialStore,
        registration_token: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 40.0,
    ) -> None:
        self.node_id = node_id
        self._registration_token = registration_token
        self._credentials = credentials
        self._token = credentials.load(node_id)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=server_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def connect(self, registration: NodeRegistration) -> str:
        if self._token is not None:
            try:
                await self.heartbeat()
                return self._token
            except RunnerControlClientError as exc:
                if exc.status_code != 401:
                    raise
                self.invalidate_credentials()
        if not self._registration_token:
            raise RunnerControlClientError(
                401,
                "runner_registration_token_missing",
                "Runner registration requires a bootstrap token",
            )
        response = await self._client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": f"Bearer {self._registration_token}"},
            json={
                "node_id": registration.node_id,
                "name": registration.name,
                "platform": registration.platform,
                "architecture": registration.architecture,
                "runner_version": registration.runner_version,
                "capabilities": list(registration.capabilities),
                "labels": registration.labels or {},
            },
        )
        payload = _response_payload(response)
        token = payload.get("runner_token")
        if not isinstance(token, str) or not token:
            raise RunnerControlClientError(
                response.status_code,
                "runner_registration_invalid_response",
                "Control Plane did not return a Runner token",
            )
        self._token = token
        self._credentials.save(self.node_id, token)
        return token

    async def heartbeat(self, heartbeat: NodeHeartbeat | None = None) -> None:
        item = heartbeat or NodeHeartbeat()
        await self._request(
            "POST",
            f"/api/v1/nodes/{self.node_id}/heartbeat",
            json={
                "status": item.status.value,
                "capabilities": list(item.capabilities) if item.capabilities is not None else None,
                "labels": item.labels,
                "runner_version": item.runner_version,
            },
        )

    async def poll(self, *, wait_seconds: float = 30.0) -> LeasedRunnerCommand | None:
        payload = await self._request(
            "GET",
            "/api/v1/runner/commands/next",
            params={"wait_seconds": wait_seconds},
        )
        raw = payload.get("command")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise RunnerControlClientError(
                500,
                "runner_command_invalid_response",
                "Control Plane returned an invalid command",
            )
        return LeasedRunnerCommand(
            id=str(raw["id"]),
            kind=RunnerCommandKind(str(raw["kind"])),
            payload=dict(raw.get("payload") or {}),
            lease_id=str(raw["lease_id"]),
            attempts=int(raw["attempts"]),
        )

    async def finish(
        self,
        command: LeasedRunnerCommand,
        *,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        await self._request(
            "POST",
            f"/api/v1/runner/commands/{command.id}/finish",
            json={
                "lease_id": command.lease_id,
                "succeeded": succeeded,
                "result": result or {},
                "error": error,
            },
        )

    async def report_command_output(
        self,
        command: LeasedRunnerCommand,
        *,
        offset: int,
        data: bytes,
    ) -> int:
        try:
            payload = await self._request(
                "POST",
                f"/api/v1/runner/commands/{command.id}/output",
                json={
                    "lease_id": command.lease_id,
                    "offset": offset,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            )
        except RunnerControlClientError as exc:
            if exc.code == "runner_output_offset_mismatch":
                raise OutputOffsetMismatch(
                    exc.status_code,
                    exc.code,
                    str(exc),
                    details=exc.details,
                ) from exc
            raise
        return int(payload["next_offset"])

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        *,
        pid: int | None = None,
        process_group_id: int | None = None,
        exit_code: int | None = None,
        executable_path: str | None = None,
        tool_id: str | None = None,
        tool_version: str | None = None,
        platform_system: str = "",
        platform_release: str = "",
        platform_architecture: str = "",
        process_created_at: str | None = None,
    ) -> None:
        await self._request(
            "POST",
            f"/api/v1/runner/executions/{execution_id}/status",
            json={
                "status": status.value,
                "pid": pid,
                "process_group_id": process_group_id,
                "exit_code": exit_code,
                "executable_path": executable_path,
                "tool_id": tool_id,
                "tool_version": tool_version,
                "platform_system": platform_system,
                "platform_release": platform_release,
                "platform_architecture": platform_architecture,
                "process_created_at": process_created_at,
            },
        )

    async def report_output(
        self,
        execution_id: str,
        *,
        stream: str,
        offset: int,
        data: bytes,
    ) -> int:
        try:
            payload = await self._request(
                "POST",
                f"/api/v1/runner/executions/{execution_id}/output",
                json={
                    "stream": stream,
                    "offset": offset,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            )
        except RunnerControlClientError as exc:
            if exc.code == "runner_output_offset_mismatch":
                raise OutputOffsetMismatch(
                    exc.status_code,
                    exc.code,
                    str(exc),
                    details=exc.details,
                ) from exc
            raise
        return int(payload["next_offset"])

    def invalidate_credentials(self) -> None:
        self._token = None
        self._credentials.clear()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, object]:
        if self._token is None:
            raise RunnerControlClientError(
                401,
                "runner_not_connected",
                "Runner has not authenticated with the Control Plane",
            )
        headers = dict(kwargs.pop("headers", {}))
        headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "X-RiftX-Node-ID": self.node_id,
            }
        )
        response = await self._client.request(method, path, headers=headers, **kwargs)
        return _response_payload(response)


def _response_payload(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RunnerControlClientError(
            response.status_code,
            "runner_control_invalid_response",
            "Control Plane returned a non-JSON response",
        ) from exc
    if response.is_error:
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code", "runner_control_error"))
            message = str(error.get("message", response.reason_phrase))
            details = error.get("details")
        else:
            code = "runner_control_error"
            message = response.reason_phrase
            details = None
        raise RunnerControlClientError(
            response.status_code,
            code,
            message,
            details=details if isinstance(details, dict) else None,
        )
    if not isinstance(payload, dict):
        raise RunnerControlClientError(
            response.status_code,
            "runner_control_invalid_response",
            "Control Plane returned an invalid JSON response",
        )
    return payload
