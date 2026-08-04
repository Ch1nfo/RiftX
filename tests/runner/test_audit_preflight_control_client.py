from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from riftx.domain import RUNNER_COMMAND_OWNERSHIP_CAPABILITY, RunnerPrincipal
from riftx.domain.audit import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    AuditPreflightEffectOwner,
    AuditPreflightExitReceipt,
    AuditPreflightExitTerminalState,
    AuditPreflightJobStatus,
    AuditPreflightLeaseEnvelope,
    AuditPreflightObservedTerminalState,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightStopDisposition,
    AuditPreflightStopReceipt,
    AuditPreflightTarget,
    PreflightRequest,
)
from riftx.domain.audit_preflight_wire import (
    AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
    AuditPreflightCallbackAck,
    AuditPreflightDispatchEnvelope,
    AuditPreflightLeaseGrant,
    AuditPreflightStartGrant,
)
from riftx.runner.control_client import (
    RunnerControlClient,
    RunnerControlClientError,
    RunnerCredentialStore,
)

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
PRINCIPAL = RunnerPrincipal(instance_id="preflight-runner", epoch=3)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _dispatch() -> AuditPreflightDispatchEnvelope:
    request = PreflightRequest(
        client_request_id="123e4567-e89b-42d3-a456-426614174000",
        repository_path="/srv/source/repository",
        source_execution_target=AuditPreflightSourceExecutionTarget(
            source_ingest_backend="linux_container"
        ),
        target=AuditPreflightTarget(
            kind=SourceTargetKind.WORKING_TREE,
            revision="HEAD",
        ),
        mode=AuditMode.STANDARD,
    )
    owner = AuditPreflightEffectOwner.from_request(
        job_id="job-1",
        operator_principal_id="operator-1",
        authorization_scope_digest=_digest("authorization"),
        source_root_identity_digest=_digest("root"),
        request=request,
        backend_id="linux_container",
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    lease = AuditPreflightLeaseEnvelope(
        owner=owner,
        runner_principal=PRINCIPAL,
        lease_id="lease-1",
        lease_expires_at=NOW + timedelta(minutes=10),
        expected_state_version=2,
        output_contract_digest=AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
    )
    return AuditPreflightDispatchEnvelope(
        owner=owner,
        lease=lease,
        request=request,
        capsule_id="capsule-1",
        state_version=2,
    )


def _client(
    tmp_path: Path,
    handler: httpx.MockTransport,
    *,
    capabilities: tuple[str, ...] = (
        AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
        RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
    ),
) -> RunnerControlClient:
    store = RunnerCredentialStore(tmp_path / "credential.json")
    store.save(
        "local",
        "runner-token",
        PRINCIPAL,
        protocol_capabilities=capabilities,
    )
    return RunnerControlClient(
        server_url="http://control.invalid",
        node_id="local",
        credentials=store,
        client=httpx.AsyncClient(
            base_url="http://control.invalid",
            transport=handler,
        ),
    )


def _assert_runner_headers(request: httpx.Request) -> None:
    assert request.headers["authorization"] == "Bearer runner-token"
    assert request.headers["x-riftx-node-id"] == "local"
    assert request.headers["x-riftx-runner-instance-id"] == PRINCIPAL.instance_id
    assert request.headers["x-riftx-runner-epoch"] == str(PRINCIPAL.epoch)


@pytest.mark.asyncio
async def test_poll_uses_dedicated_preflight_wire_and_validates_owner(tmp_path: Path) -> None:
    dispatch = _dispatch()

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_runner_headers(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/runner/audit-preflight/next"
        assert request.url.params["wait_seconds"] == "7"
        return httpx.Response(
            200,
            json={"dispatch": dispatch.model_dump(mode="json")},
        )

    client = _client(tmp_path, httpx.MockTransport(handler))
    try:
        assert await client.poll_audit_preflight(wait_seconds=7) == dispatch
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_preflight_wire_requires_immutable_credential_capability(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"dispatch": None})

    client = _client(
        tmp_path,
        httpx.MockTransport(handler),
        capabilities=(RUNNER_COMMAND_OWNERSHIP_CAPABILITY,),
    )
    try:
        with pytest.raises(RunnerControlClientError) as exc_info:
            await client.poll_audit_preflight()
        assert exc_info.value.code == "runner_protocol_capability_missing"
        assert calls == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_renew_and_start_echo_full_owner_lease_binding(tmp_path: Path) -> None:
    dispatch = _dispatch()
    renewed_lease = AuditPreflightLeaseEnvelope(
        owner=dispatch.owner,
        runner_principal=PRINCIPAL,
        lease_id=dispatch.lease.lease_id,
        lease_expires_at=NOW + timedelta(minutes=20),
        expected_state_version=3,
        output_contract_digest=dispatch.lease.output_contract_digest,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_runner_headers(request)
        body = request.read()
        payload = httpx.Response(200, content=body).json()
        calls.append(request.url.path)
        assert payload["owner_kind"] == "preflight_job"
        assert payload["owner"] == dispatch.owner.model_dump(mode="json")
        if request.url.path.endswith("/lease"):
            assert payload["schema_version"] == "riftx.audit-preflight-renew-request/v1"
            assert payload["lease"] == dispatch.lease.model_dump(mode="json")
            return httpx.Response(
                200,
                json=AuditPreflightLeaseGrant(
                    job_id="job-1",
                    status=AuditPreflightJobStatus.CLAIMED,
                    state_version=3,
                    lease_envelope_digest=renewed_lease.lease_envelope_digest,
                    lease_expires_at=renewed_lease.lease_expires_at,
                    lease_duration_seconds=600.0,
                ).model_dump(mode="json"),
            )
        assert request.url.path.endswith("/start")
        assert payload["schema_version"] == "riftx.audit-preflight-start-request/v1"
        assert payload["lease"] == renewed_lease.model_dump(mode="json")
        assert payload["capsule_prepare_proof_digest"] == _digest("prepare")
        return httpx.Response(
            200,
            json=AuditPreflightStartGrant(
                job_id="job-1",
                capsule_id="capsule-1",
                state_version=4,
                started_at=NOW + timedelta(seconds=5),
            ).model_dump(mode="json"),
        )

    client = _client(tmp_path, httpx.MockTransport(handler))
    try:
        lease_grant = await client.renew_audit_preflight(
            dispatch,
            lease=dispatch.lease,
            state_version=2,
        )
        assert lease_grant.lease_envelope_digest == renewed_lease.lease_envelope_digest
        start_grant = await client.start_audit_preflight(
            dispatch,
            lease=renewed_lease,
            state_version=3,
            capsule_prepare_proof_digest=_digest("prepare"),
        )
        assert start_grant.state_version == 4
        assert calls == [
            "/api/v1/runner/audit-preflight/job-1/lease",
            "/api/v1/runner/audit-preflight/job-1/start",
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_finish_and_stop_use_disjoint_bounded_callback_contracts(tmp_path: Path) -> None:
    dispatch = _dispatch()
    finish_receipt = AuditPreflightExitReceipt(
        job_id="job-1",
        effect_owner_digest=dispatch.owner.effect_owner_digest,
        lease_envelope_digest=dispatch.lease.lease_envelope_digest,
        capsule_id="capsule-1",
        runner_principal=PRINCIPAL,
        backend_id=dispatch.owner.backend_id,
        image_digest=dispatch.owner.image_digest,
        policy_digest=dispatch.owner.policy_digest,
        process_identity_digest=_digest("finish-process"),
        terminal_state=AuditPreflightExitTerminalState.FAILED,
        received_at=NOW + timedelta(seconds=5),
    )
    stop_receipt = AuditPreflightStopReceipt(
        job_id="job-1",
        effect_owner_digest=dispatch.owner.effect_owner_digest,
        lease_envelope_digest=dispatch.lease.lease_envelope_digest,
        capsule_id="capsule-1",
        runner_principal=PRINCIPAL,
        backend_id=dispatch.owner.backend_id,
        image_digest=dispatch.owner.image_digest,
        policy_digest=dispatch.owner.policy_digest,
        disposition=AuditPreflightStopDisposition.STOPPED,
        process_identity_digest=_digest("stop-process"),
        observed_terminal_state=AuditPreflightObservedTerminalState.CANCELLED,
        received_at=NOW + timedelta(seconds=6),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = httpx.Response(200, content=request.read()).json()
        if request.url.path.endswith("/finish"):
            assert payload["schema_version"] == "riftx.audit-preflight-finish-request/v1"
            assert payload["status"] == "failed"
            assert payload["result"] is None
            assert payload["safe_error_code"] == "audit_source_ingest_failed"
            assert payload["exit_receipt"]["schema_version"] == (
                "riftx.audit-preflight-exit-receipt/v1"
            )
            ack_status = AuditPreflightJobStatus.FAILED
        else:
            assert request.url.path.endswith("/stop")
            assert payload["schema_version"] == "riftx.audit-preflight-stop-request/v1"
            assert payload["status"] == "cancelled"
            assert payload["stop_receipt"]["schema_version"] == (
                "riftx.audit-preflight-stop-receipt/v1"
            )
            ack_status = AuditPreflightJobStatus.CANCELLED
        return httpx.Response(
            200,
            json=AuditPreflightCallbackAck(
                job_id="job-1",
                status=ack_status,
                state_version=3,
                finished_at=NOW + timedelta(seconds=7),
            ).model_dump(mode="json"),
        )

    client = _client(tmp_path, httpx.MockTransport(handler))
    try:
        finish_ack = await client.finish_audit_preflight(
            dispatch,
            lease=dispatch.lease,
            state_version=2,
            status=AuditPreflightJobStatus.FAILED,
            result=None,
            safe_error_code="audit_source_ingest_failed",
            exit_receipt=finish_receipt,
        )
        stop_ack = await client.stop_audit_preflight(
            dispatch,
            lease=dispatch.lease,
            state_version=2,
            status=AuditPreflightJobStatus.CANCELLED,
            safe_error_code=None,
            stop_receipt=stop_receipt,
        )
        assert finish_ack.status is AuditPreflightJobStatus.FAILED
        assert stop_ack.status is AuditPreflightJobStatus.CANCELLED
    finally:
        await client.close()
