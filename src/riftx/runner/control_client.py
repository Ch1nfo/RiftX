"""Outbound HTTP client used by independently deployed RiftX Runners."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from riftx.application.services import NodeHeartbeat, NodeRegistration
from riftx.domain import (
    RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
    RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION,
    ExecutionStatus,
    RunnerCommandKind,
    RunnerCommandOwnership,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    runner_payload_digest,
)


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


class OutputLimitExceeded(RunnerControlClientError):
    """The immutable output contract rejected bytes beyond its exact cap."""

    @property
    def max_output_bytes(self) -> int:
        value = self.details.get("max_output_bytes")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("output limit response omitted max_output_bytes")
        return value


def _required_response_int(
    payload: Mapping[str, object],
    field_name: str,
    *,
    minimum: int = 0,
) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RunnerControlClientError(
            500,
            "runner_command_invalid_response",
            f"Control Plane returned an invalid {field_name}",
        )
    return value


@dataclass(slots=True)
class LeasedRunnerCommand:
    id: str
    kind: RunnerCommandKind
    payload: dict[str, object]
    lease_id: str
    attempts: int
    target: RunnerPrincipal | None = None
    ownership: RunnerCommandOwnership | None = None
    ownership_schema_version: str = RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION
    effect_binding_id: str = ""
    binding_digest: str = ""
    envelope_digest: str = ""
    state_version: int = 0
    operation_family: RunnerOperationFamily | None = None
    output_contract: RunnerOutputContract | None = None
    lease_expires_at: datetime | None = None
    lease_duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class StoredRunnerCredential:
    """Complete server-issued identity required for authenticated callbacks."""

    token: str
    principal: RunnerPrincipal
    protocol_capabilities: tuple[str, ...] = ()


class RunnerCredentialStore:
    """Persists one complete node-scoped credential returned after registration."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, node_id: str) -> StoredRunnerCredential | None:
        try:
            payload = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("node_id") != node_id:
            return None
        token = payload.get("runner_token")
        if not isinstance(token, str) or not token:
            return None
        try:
            principal = RunnerPrincipal.model_validate(payload.get("principal"))
        except ValidationError:
            # Legacy token-only files are deliberately rejected. Inventing a
            # principal here would let cloned execution state impersonate a
            # server-issued owner generation.
            return None
        raw_capabilities = payload.get("protocol_capabilities", [])
        if not isinstance(raw_capabilities, list) or any(
            not isinstance(item, str) or not item for item in raw_capabilities
        ):
            return None
        return StoredRunnerCredential(
            token=token,
            principal=principal,
            protocol_capabilities=tuple(sorted(set(raw_capabilities))),
        )

    def save(
        self,
        node_id: str,
        token: str,
        principal: RunnerPrincipal,
        *,
        protocol_capabilities: tuple[str, ...] = (),
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "node_id": node_id,
                    "runner_token": token,
                    "principal": principal.model_dump(mode="json"),
                    "protocol_capabilities": list(
                        sorted(set(protocol_capabilities))
                    ),
                }
            )
        )
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
        self._credential = credentials.load(node_id)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=server_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
        )
        self._command_locks: dict[str, asyncio.Lock] = {}

    async def connect(self, registration: NodeRegistration) -> str:
        capabilities = tuple(
            sorted(
                {
                    *registration.capabilities,
                    RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
                }
            )
        )
        registration = NodeRegistration(
            node_id=registration.node_id,
            name=registration.name,
            platform=registration.platform,
            architecture=registration.architecture,
            runner_version=registration.runner_version,
            capabilities=capabilities,
            labels=registration.labels,
        )
        if (
            self._credential is not None
            and RUNNER_COMMAND_OWNERSHIP_CAPABILITY
            not in self._credential.protocol_capabilities
        ):
            self.invalidate_credentials()
        if self._credential is not None:
            try:
                await self.heartbeat(
                    NodeHeartbeat(
                        capabilities=registration.capabilities,
                        labels=registration.labels,
                        runner_version=registration.runner_version,
                    )
                )
                return self._credential.token
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
        principal = _parse_runner_principal(
            payload.get("principal"),
            status_code=response.status_code,
            code="runner_registration_invalid_response",
            message="Control Plane did not return a valid Runner principal",
        )
        credential = StoredRunnerCredential(
            token=token,
            principal=principal,
            protocol_capabilities=capabilities,
        )
        self._credentials.save(
            self.node_id,
            token,
            principal,
            protocol_capabilities=capabilities,
        )
        self._credential = credential
        return token

    def can_enable_protocol_capability(self, capability: str) -> bool:
        """Whether startup can authenticate an immutable protocol capability."""

        credential = self._credential
        return (
            credential is not None
            and capability in credential.protocol_capabilities
        ) or self._registration_token is not None

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

    async def poll(
        self,
        *,
        wait_seconds: float = 30.0,
        safety_only: bool = False,
    ) -> LeasedRunnerCommand | None:
        payload = await self._request(
            "GET",
            "/api/v1/runner/commands/next",
            params={
                "wait_seconds": wait_seconds,
                "safety_only": str(safety_only).lower(),
            },
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
        raw_lease_duration = raw.get("lease_duration_seconds")
        lease_duration = (
            float(raw_lease_duration)
            if isinstance(raw_lease_duration, int | float)
            and not isinstance(raw_lease_duration, bool)
            and raw_lease_duration > 0
            else None
        )
        target = self._require_polled_target(raw.get("target"))
        if raw.get("ownership_schema_version") != RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION:
            raise RunnerControlClientError(
                500,
                "runner_command_invalid_response",
                "Control Plane returned an unsupported ownership envelope",
            )
        try:
            kind = RunnerCommandKind(str(raw["kind"]))
            command_payload = dict(raw.get("payload") or {})
            ownership = RunnerCommandOwnership.model_validate(raw.get("ownership"))
            effect_binding = RunnerEffectBinding.model_validate(raw.get("effect_binding"))
            output_contract = RunnerOutputContract.model_validate(raw.get("output_contract"))
            operation_family = RunnerOperationFamily(str(raw["operation_family"]))
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RunnerControlClientError(
                500,
                "runner_command_invalid_response",
                "Control Plane returned invalid Runner ownership metadata",
            ) from exc
        if (
            ownership.schema_version != raw.get("ownership_schema_version")
            or ownership.command_id != raw.get("id")
            or ownership.operation is not kind
            or ownership.operation_family is not operation_family
            or ownership.effect_binding != effect_binding
            or ownership.output_contract != output_contract
            or ownership.envelope_digest != raw.get("envelope_digest")
            or ownership.effect_binding.target != target
            or ownership.effect_binding.node_id != self.node_id
            or runner_payload_digest(command_payload) != ownership.payload_digest
        ):
            raise RunnerControlClientError(
                500,
                "runner_command_invalid_response",
                "Control Plane returned inconsistent Runner ownership metadata",
            )
        return LeasedRunnerCommand(
            id=str(raw["id"]),
            kind=kind,
            payload=command_payload,
            lease_id=str(raw["lease_id"]),
            attempts=int(raw["attempts"]),
            target=target,
            ownership=ownership,
            ownership_schema_version=RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION,
            effect_binding_id=effect_binding.id,
            binding_digest=effect_binding.binding_digest,
            envelope_digest=str(raw["envelope_digest"]),
            state_version=int(raw["state_version"]),
            operation_family=operation_family,
            output_contract=output_contract,
            lease_expires_at=datetime.fromisoformat(str(raw["lease_expires_at"])),
            lease_duration_seconds=lease_duration,
        )

    async def renew(self, command: LeasedRunnerCommand) -> LeasedRunnerCommand:
        async with self._command_lock(command.id):
            expected_principal = self._require_callback_principal(command)
            payload = await self._request(
                "POST",
                f"/api/v1/runner/commands/{command.id}/lease",
                expected_principal=expected_principal,
                json=_command_callback_identity(command),
            )
            value = payload.get("lease_duration_seconds")
            if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
                raise RunnerControlClientError(
                    500,
                    "runner_command_invalid_response",
                    "Control Plane omitted the renewed command lease duration",
                )
            if (
                payload.get("envelope_digest") != command.envelope_digest
                or payload.get("binding_digest") != command.binding_digest
            ):
                raise RunnerControlClientError(
                    500,
                    "runner_command_invalid_response",
                    "Control Plane changed immutable command ownership during renewal",
                )
            command.state_version = _required_response_int(
                payload,
                "state_version",
                minimum=1,
            )
            command.lease_expires_at = datetime.fromisoformat(
                str(payload["lease_expires_at"])
            )
            command.lease_duration_seconds = float(value)
            return command

    async def finish(
        self,
        command: LeasedRunnerCommand,
        *,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        async with self._command_lock(command.id):
            expected_principal = self._require_callback_principal(command)
            await self._request(
                "POST",
                f"/api/v1/runner/commands/{command.id}/finish-owned",
                expected_principal=expected_principal,
                json={
                    **_command_callback_identity(command),
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
            async with self._command_lock(command.id):
                expected_principal = self._require_callback_principal(command)
                payload = await self._request(
                    "POST",
                    f"/api/v1/runner/commands/{command.id}/output",
                    expected_principal=expected_principal,
                    json={
                        **_command_callback_identity(command),
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
        return _required_response_int(payload, "next_offset")

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        *,
        runner_command_id: str,
        runner_effect_binding_id: str,
        runner_envelope_digest: str,
        runner_binding_digest: str,
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
        physical_stop_confirmed: bool = False,
    ) -> None:
        await self._request(
            "POST",
            f"/api/v1/runner/executions/{execution_id}/status",
            json={
                **_execution_callback_identity(
                    command_id=runner_command_id,
                    effect_binding_id=runner_effect_binding_id,
                    envelope_digest=runner_envelope_digest,
                    binding_digest=runner_binding_digest,
                ),
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
                "physical_stop_confirmed": physical_stop_confirmed,
            },
        )

    async def report_output(
        self,
        execution_id: str,
        *,
        runner_command_id: str,
        runner_effect_binding_id: str,
        runner_envelope_digest: str,
        runner_binding_digest: str,
        stream: str,
        offset: int,
        data: bytes,
    ) -> int:
        try:
            payload = await self._request(
                "POST",
                f"/api/v1/runner/executions/{execution_id}/output",
                json={
                    **_execution_callback_identity(
                        command_id=runner_command_id,
                        effect_binding_id=runner_effect_binding_id,
                        envelope_digest=runner_envelope_digest,
                        binding_digest=runner_binding_digest,
                    ),
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
            if exc.code == "runner_execution_output_too_large":
                raise OutputLimitExceeded(
                    exc.status_code,
                    exc.code,
                    str(exc),
                    details=exc.details,
                ) from exc
            raise
        return _required_response_int(payload, "next_offset")

    def invalidate_credentials(self) -> None:
        self._credential = None
        self._credentials.clear()

    @property
    def principal(self) -> RunnerPrincipal | None:
        credential = self._credential
        return credential.principal if credential is not None else None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _command_lock(self, command_id: str) -> asyncio.Lock:
        return self._command_locks.setdefault(command_id, asyncio.Lock())

    def _require_protocol_capability(self, capability: str) -> None:
        credential = self._credential
        if credential is None:
            raise RunnerControlClientError(
                401,
                "runner_not_connected",
                "Runner has not authenticated with the Control Plane",
            )
        if capability not in credential.protocol_capabilities:
            raise RunnerControlClientError(
                409,
                "runner_protocol_capability_missing",
                "Runner credential does not carry the required protocol capability",
                details={"required_capability": capability},
            )

    def _require_callback_principal(
        self,
        command: LeasedRunnerCommand,
    ) -> RunnerPrincipal:
        target = _required_command_target(command)
        credential = self._credential
        if credential is None or credential.principal != target:
            raise RunnerControlClientError(
                409,
                "runner_command_principal_mismatch",
                "Runner command belongs to a different owner generation",
            )
        return target

    async def _request(
        self,
        method: str,
        path: str,
        *,
        expected_principal: RunnerPrincipal | None = None,
        **kwargs: Any,
    ) -> dict[str, object]:
        credential = self._credential
        if credential is None:
            raise RunnerControlClientError(
                401,
                "runner_not_connected",
                "Runner has not authenticated with the Control Plane",
            )
        if expected_principal is not None and credential.principal != expected_principal:
            raise RunnerControlClientError(
                409,
                "runner_command_principal_mismatch",
                "Runner command belongs to a different owner generation",
            )
        headers = dict(kwargs.pop("headers", {}))
        headers.update(
            {
                "Authorization": f"Bearer {credential.token}",
                "X-RiftX-Node-ID": self.node_id,
                "X-RiftX-Runner-Instance-ID": credential.principal.instance_id,
                "X-RiftX-Runner-Epoch": str(credential.principal.epoch),
            }
        )
        response = await self._client.request(method, path, headers=headers, **kwargs)
        return _response_payload(response)

    def _require_polled_target(self, raw: object) -> RunnerPrincipal:
        target = _parse_runner_principal(
            raw,
            status_code=500,
            code="runner_command_invalid_response",
            message="Control Plane returned a command without a valid target principal",
        )
        credential = self._credential
        if credential is None or target != credential.principal:
            raise RunnerControlClientError(
                500,
                "runner_command_principal_mismatch",
                "Control Plane returned a command for a different owner generation",
            )
        return target


def _required_command_target(command: LeasedRunnerCommand) -> RunnerPrincipal:
    if command.target is None:
        raise RunnerControlClientError(
            409,
            "runner_command_principal_missing",
            "Runner command omitted its owner generation",
        )
    return command.target


def _command_callback_identity(command: LeasedRunnerCommand) -> dict[str, object]:
    if (
        len(command.envelope_digest) != 64
        or len(command.binding_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in command.envelope_digest + command.binding_digest
        )
    ):
        raise RunnerControlClientError(
            409,
            "runner_command_ownership_missing",
            "Runner command omitted immutable ownership digests",
        )
    return {
        "lease_id": command.lease_id,
        "state_version": command.state_version,
        "envelope_digest": command.envelope_digest,
        "binding_digest": command.binding_digest,
    }


def _execution_callback_identity(
    *,
    command_id: str,
    effect_binding_id: str,
    envelope_digest: str,
    binding_digest: str,
) -> dict[str, str]:
    if not command_id or not effect_binding_id:
        raise RunnerControlClientError(
            409,
            "runner_execution_callback_binding_missing",
            "Runner execution omitted its launch command binding",
        )
    if (
        len(envelope_digest) != 64
        or len(binding_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in envelope_digest + binding_digest
        )
    ):
        raise RunnerControlClientError(
            409,
            "runner_execution_callback_binding_missing",
            "Runner execution omitted immutable launch ownership digests",
        )
    return {
        "command_id": command_id,
        "effect_binding_id": effect_binding_id,
        "envelope_digest": envelope_digest,
        "binding_digest": binding_digest,
    }


def _parse_runner_principal(
    raw: object,
    *,
    status_code: int,
    code: str,
    message: str,
) -> RunnerPrincipal:
    try:
        return RunnerPrincipal.model_validate(raw)
    except ValidationError as exc:
        raise RunnerControlClientError(status_code, code, message) from exc


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
