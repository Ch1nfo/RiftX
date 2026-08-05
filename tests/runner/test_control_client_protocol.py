from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from riftx.application.services import NodeRegistration
from riftx.domain import (
    AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
    ExecutionStatus,
    RunKind,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnership,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    runner_payload_digest,
)
from riftx.runner.control_client import (
    LeasedRunnerCommand,
    OutputLimitExceeded,
    RunnerControlClient,
    RunnerControlClientError,
    RunnerCredentialStore,
    StoredRunnerCredential,
)

_PRINCIPAL = RunnerPrincipal(instance_id="runner-instance-a", epoch=7)


def _command_response(
    *,
    command_id: str = "command-1",
    principal: RunnerPrincipal = _PRINCIPAL,
) -> dict[str, object]:
    payload = {"execution_id": "execution-1"}
    binding = RunnerEffectBinding(
        id="binding-1",
        run_id="run-1",
        run_kind=RunKind.GENERAL,
        node_id="runner-a",
        target=principal,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=RunnerOperationFamily.EXECUTION,
        execution_id="execution-1",
        resource_kind=RunnerResourceKind.EXECUTION,
        resource_id="execution-1",
    )
    contract = RunnerOutputContract(
        max_output_bytes=1024,
        allowed_streams=("stderr", "stdout"),
        result_schema="riftx.runner-result/execution-start/v1",
    )
    ownership = RunnerCommandOwnership(
        command_id=command_id,
        effect_binding=binding,
        operation=RunnerCommandKind.EXECUTE,
        operation_family=RunnerOperationFamily.EXECUTION,
        payload_digest=runner_payload_digest(payload),
        output_contract=contract,
    )
    return {
        "id": command_id,
        "node_id": "runner-a",
        "kind": "execute",
        "payload": payload,
        "status": "leased",
        "lease_id": "lease-1",
        "attempts": 1,
        "target": principal.model_dump(mode="json"),
        "ownership": ownership.model_dump(mode="json"),
        "ownership_schema_version": ownership.schema_version,
        "effect_binding": binding.model_dump(mode="json"),
        "operation_family": ownership.operation_family.value,
        "output_contract": contract.model_dump(mode="json"),
        "envelope_digest": ownership.envelope_digest,
        "state_version": 1,
        "lease_expires_at": "2026-08-01T10:00:00+00:00",
        "lease_duration_seconds": 30,
    }


def _registration() -> NodeRegistration:
    return NodeRegistration(
        node_id="runner-a",
        name="Runner A",
        platform="linux",
        architecture="x86_64",
    )


def test_credential_store_round_trips_complete_principal_and_rejects_legacy_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credentials.json"
    store = RunnerCredentialStore(path)

    path.write_text(json.dumps({"node_id": "runner-a", "runner_token": "legacy-token"}))
    assert store.load("runner-a") is None

    store.save(
        "runner-a",
        "scoped-token",
        _PRINCIPAL,
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )

    assert store.load("runner-a") == StoredRunnerCredential(
        token="scoped-token",
        principal=_PRINCIPAL,
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )
    assert store.load("another-node") is None
    assert json.loads(path.read_text()) == {
        "node_id": "runner-a",
        "runner_token": "scoped-token",
        "principal": {"instance_id": "runner-instance-a", "epoch": 7},
        "protocol_capabilities": [RUNNER_COMMAND_OWNERSHIP_CAPABILITY],
    }
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_registration_persists_principal_and_sends_complete_auth_headers(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/nodes/register":
            return httpx.Response(
                200,
                json={
                    "node": {"id": "runner-a"},
                    "created": True,
                    "runner_token": "scoped-token",
                    "principal": _PRINCIPAL.model_dump(mode="json"),
                },
            )
        if request.url.path == "/api/v1/runner/commands/next":
            return httpx.Response(
                200,
                json={
                    "command": {
                        **_command_response(),
                    }
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    http = httpx.AsyncClient(
        base_url="http://control.test",
        transport=httpx.MockTransport(handler),
    )
    credential_path = tmp_path / "credentials.json"
    credential_path.write_text(json.dumps({"node_id": "runner-a", "runner_token": "legacy-token"}))
    store = RunnerCredentialStore(credential_path)
    client = RunnerControlClient(
        server_url="http://control.test",
        node_id="runner-a",
        credentials=store,
        registration_token="bootstrap-token",
        client=http,
    )

    assert await client.connect(_registration()) == "scoped-token"
    command = await client.poll(wait_seconds=0)

    assert command is not None
    assert command.target == _PRINCIPAL
    assert client.principal == _PRINCIPAL
    assert store.load("runner-a") == StoredRunnerCredential(
        token="scoped-token",
        principal=_PRINCIPAL,
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )
    assert requests[0].headers["Authorization"] == "Bearer bootstrap-token"
    assert requests[1].headers["Authorization"] == "Bearer scoped-token"
    assert requests[1].headers["X-RiftX-Node-ID"] == "runner-a"
    assert requests[1].headers["X-RiftX-Runner-Instance-ID"] == "runner-instance-a"
    assert requests[1].headers["X-RiftX-Runner-Epoch"] == "7"
    await http.aclose()


@pytest.mark.asyncio
async def test_valid_stored_credential_reconnects_without_registration(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1/nodes/runner-a/heartbeat"
        return httpx.Response(200, json={"id": "runner-a"})

    store = RunnerCredentialStore(tmp_path / "credentials.json")
    store.save(
        "runner-a",
        "stored-token",
        _PRINCIPAL,
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )
    http = httpx.AsyncClient(
        base_url="http://control.test",
        transport=httpx.MockTransport(handler),
    )
    client = RunnerControlClient(
        server_url="http://control.test",
        node_id="runner-a",
        credentials=store,
        client=http,
    )

    assert await client.connect(_registration()) == "stored-token"
    assert client.can_enable_protocol_capability(RUNNER_COMMAND_OWNERSHIP_CAPABILITY)
    assert not client.can_enable_protocol_capability(
        AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY
    )
    assert len(requests) == 1
    assert requests[0].headers["X-RiftX-Runner-Instance-ID"] == "runner-instance-a"
    assert requests[0].headers["X-RiftX-Runner-Epoch"] == "7"
    heartbeat = json.loads(requests[0].read())
    assert heartbeat["capabilities"] == [RUNNER_COMMAND_OWNERSHIP_CAPABILITY]
    assert heartbeat["runner_version"] == "unknown"
    await http.aclose()


@pytest.mark.asyncio
async def test_status_report_sends_explicit_physical_stop_confirmation(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "exited"})

    store = RunnerCredentialStore(tmp_path / "credentials.json")
    store.save(
        "runner-a",
        "stored-token",
        _PRINCIPAL,
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )
    http = httpx.AsyncClient(
        base_url="http://control.test",
        transport=httpx.MockTransport(handler),
    )
    client = RunnerControlClient(
        server_url="http://control.test",
        node_id="runner-a",
        credentials=store,
        client=http,
    )

    await client.report_status(
        "execution-a",
        ExecutionStatus.EXITED,
        runner_command_id="command-a",
        runner_effect_binding_id="binding-a",
        runner_envelope_digest="a" * 64,
        runner_binding_digest="b" * 64,
        exit_code=0,
        physical_stop_confirmed=True,
    )

    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "command_id": "command-a",
        "effect_binding_id": "binding-a",
        "envelope_digest": "a" * 64,
        "binding_digest": "b" * 64,
        "status": "exited",
        "pid": None,
        "process_group_id": None,
        "exit_code": 0,
        "executable_path": None,
        "tool_id": None,
        "tool_version": None,
        "platform_system": "",
        "platform_release": "",
        "platform_architecture": "",
        "process_created_at": None,
        "physical_stop_confirmed": True,
    }
    await http.aclose()


@pytest.mark.asyncio
async def test_execution_output_cap_error_is_typed_with_authoritative_limit(
    tmp_path: Path,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "runner_execution_output_too_large",
                    "message": "output exceeds immutable contract",
                    "details": {"max_output_bytes": 5},
                }
            },
        )

    store = RunnerCredentialStore(tmp_path / "credentials.json")
    store.save(
        "runner-a",
        "stored-token",
        _PRINCIPAL,
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )
    http = httpx.AsyncClient(
        base_url="http://control.test",
        transport=httpx.MockTransport(handler),
    )
    client = RunnerControlClient(
        server_url="http://control.test",
        node_id="runner-a",
        credentials=store,
        client=http,
    )

    with pytest.raises(OutputLimitExceeded) as captured:
        await client.report_output(
            "execution-a",
            runner_command_id="command-a",
            runner_effect_binding_id="binding-a",
            runner_envelope_digest="a" * 64,
            runner_binding_digest="b" * 64,
            stream="stdout",
            offset=0,
            data=b"abcdef",
        )

    assert captured.value.max_output_bytes == 5
    await http.aclose()


@pytest.mark.asyncio
async def test_registration_response_without_principal_fails_closed(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "node": {"id": "runner-a"},
                "created": True,
                "runner_token": "token-without-principal",
            },
        )

    path = tmp_path / "credentials.json"
    http = httpx.AsyncClient(
        base_url="http://control.test",
        transport=httpx.MockTransport(handler),
    )
    client = RunnerControlClient(
        server_url="http://control.test",
        node_id="runner-a",
        credentials=RunnerCredentialStore(path),
        registration_token="bootstrap-token",
        client=http,
    )

    with pytest.raises(RunnerControlClientError) as captured:
        await client.connect(_registration())

    assert captured.value.code == "runner_registration_invalid_response"
    assert client.principal is None
    assert not path.exists()
    await http.aclose()


@pytest.mark.asyncio
async def test_client_rejects_command_for_another_principal_before_effectful_callback(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"lease_duration_seconds": 30})

    store = RunnerCredentialStore(tmp_path / "credentials.json")
    store.save(
        "runner-a",
        "scoped-token",
        _PRINCIPAL,
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )
    http = httpx.AsyncClient(
        base_url="http://control.test",
        transport=httpx.MockTransport(handler),
    )
    client = RunnerControlClient(
        server_url="http://control.test",
        node_id="runner-a",
        credentials=store,
        client=http,
    )
    command = LeasedRunnerCommand(
        id="command-b",
        kind=RunnerCommandKind.EXECUTE,
        payload={},
        lease_id="lease-b",
        attempts=1,
        target=RunnerPrincipal(instance_id="runner-instance-b", epoch=8),
    )

    with pytest.raises(RunnerControlClientError) as captured:
        await client.renew(command)

    assert captured.value.code == "runner_command_principal_mismatch"
    assert requests == []
    await http.aclose()


@pytest.mark.asyncio
async def test_client_rejects_poll_response_targeted_to_another_principal(
    tmp_path: Path,
) -> None:
    other = RunnerPrincipal(instance_id="runner-instance-b", epoch=8)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "command": {
                    "id": "command-b",
                    "kind": "execute",
                    "payload": {},
                    "lease_id": "lease-b",
                    "attempts": 1,
                    "target": other.model_dump(mode="json"),
                    "lease_expires_at": "2026-08-01T10:00:00+00:00",
                    "lease_duration_seconds": 30,
                }
            },
        )

    store = RunnerCredentialStore(tmp_path / "credentials.json")
    store.save(
        "runner-a",
        "scoped-token",
        _PRINCIPAL,
        protocol_capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )
    http = httpx.AsyncClient(
        base_url="http://control.test",
        transport=httpx.MockTransport(handler),
    )
    client = RunnerControlClient(
        server_url="http://control.test",
        node_id="runner-a",
        credentials=store,
        client=http,
    )

    with pytest.raises(RunnerControlClientError) as captured:
        await client.poll(wait_seconds=0)

    assert captured.value.code == "runner_command_principal_mismatch"
    assert captured.value.status_code == 500
    await http.aclose()
