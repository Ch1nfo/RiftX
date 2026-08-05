from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, text

from riftx.api.errors import install_error_handlers
from riftx.api.routes.audit_preflight_runner import router
from riftx.application.services.audit_preflight_runner import (
    AuditPreflightRunnerService,
)
from riftx.domain.audit import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    AuditPreflightBudgetStatus,
    AuditPreflightCapabilityFact,
    AuditPreflightCapabilityMatrix,
    AuditPreflightCapabilityStatus,
    AuditPreflightEffectOwner,
    AuditPreflightExitReceipt,
    AuditPreflightExitTerminalState,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightLeaseEnvelope,
    AuditPreflightMinimumFeasibleBudget,
    AuditPreflightObservedTerminalState,
    AuditPreflightResult,
    AuditPreflightSecurityContext,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightStopDisposition,
    AuditPreflightStopReceipt,
    AuditPreflightTarget,
    PreflightRequest,
)
from riftx.domain.audit_preflight_wire import (
    AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
    AuditPreflightDispatchEnvelope,
)
from riftx.domain.runner import RunnerCredential, RunnerPrincipal
from riftx.persistence.audit_preflight import SQLAlchemyAuditPreflightRepository
from riftx.persistence.database import Database

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
PRINCIPAL = RunnerPrincipal(instance_id="preflight-runner", epoch=3)
TOKEN = "preflight-runner-token"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _request(client_request_id: str) -> PreflightRequest:
    return PreflightRequest(
        client_request_id=client_request_id,
        repository_path="/srv/source/repository",
        source_execution_target=AuditPreflightSourceExecutionTarget(
            source_ingest_backend="linux_container"
        ),
        target=AuditPreflightTarget(
            kind=SourceTargetKind.WORKING_TREE,
            revision="HEAD",
        ),
        security_context=AuditPreflightSecurityContext(),
        mode=AuditMode.STANDARD,
    )


def _pending_job(
    *,
    job_id: str = "preflight-job-1",
    client_request_id: str = "123e4567-e89b-42d3-a456-426614174000",
    created_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> AuditPreflightJob:
    request = _request(client_request_id)
    return AuditPreflightJob(
        job_id=job_id,
        client_request_id=client_request_id,
        operator_principal_id="operator-1",
        authorization_scope_digest=_digest("authorization"),
        request_digest=request.request_digest,
        restricted_request_json=request.canonical_json(),
        source_root_identity_digest=_digest("source-root"),
        backend_id="linux_container",
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        expires_at=expires_at or created_at + timedelta(hours=1),
        created_at=created_at,
        updated_at=created_at,
    )


def _result(
    dispatch: AuditPreflightDispatchEnvelope,
    *,
    blocking: bool,
) -> AuditPreflightResult:
    capabilities = (
        AuditPreflightCapabilityFact(
            capability_id="detector_inventory",
            status=(
                AuditPreflightCapabilityStatus.BLOCKING
                if blocking
                else AuditPreflightCapabilityStatus.UNAVAILABLE
            ),
            reason_code="audit_inventory_unavailable",
        ),
        AuditPreflightCapabilityFact(
            capability_id="source_ingest",
            status=AuditPreflightCapabilityStatus.AVAILABLE,
            component_version="v1",
            component_digest=_digest("component"),
            proof_digest=_digest("proof"),
        ),
    )
    completed_at = NOW + timedelta(seconds=10)
    return AuditPreflightResult(
        preflight_job_id=dispatch.owner.job_id,
        request_digest=dispatch.owner.request_digest,
        effect_owner_digest=dispatch.owner.effect_owner_digest,
        source_root_identity_digest=dispatch.owner.source_root_identity_digest,
        repository_identity_digest=_digest("repository"),
        content_identity_digest=_digest("content"),
        backend_id=dispatch.owner.backend_id,
        image_digest=dispatch.owner.image_digest,
        policy_digest=dispatch.owner.policy_digest,
        capsule_prepare_proof_digest=_digest("prepare"),
        target_kind=SourceTargetKind.WORKING_TREE,
        revision="HEAD",
        mode=AuditMode.STANDARD,
        include_untracked=False,
        head_revision=None if blocking else "1" * 40,
        resolved_revision=None if blocking else "1" * 40,
        dirty=False,
        staged=False,
        unstaged=False,
        untracked=False,
        file_count=1,
        total_bytes=10,
        max_file_bytes=10,
        capability_matrix=AuditPreflightCapabilityMatrix(entries=capabilities),
        blocking_errors=("audit_preflight_blocked",) if blocking else (),
        minimum_feasible_budget=AuditPreflightMinimumFeasibleBudget(
            status=(
                AuditPreflightBudgetStatus.BLOCKING
                if blocking
                else AuditPreflightBudgetStatus.UNAVAILABLE
            ),
            provenance_digest=_digest("budget"),
            reason_code=("audit_preflight_blocked" if blocking else "audit_inventory_unavailable"),
        ),
        completed_at=completed_at,
        expires_at=completed_at + timedelta(minutes=10),
    )


class FakeCredentialRepository:
    def __init__(self, credential: RunnerCredential) -> None:
        self.credential = credential

    async def get_by_token_hash(
        self,
        node_id: str,
        token_hash: str,
    ) -> RunnerCredential | None:
        if node_id == self.credential.node_id and token_hash == self.credential.token_hash:
            return self.credential
        return None


class SpyPreflightRepository:
    def __init__(self, delegate: SQLAlchemyAuditPreflightRepository) -> None:
        self.delegate = delegate
        self.owner_binding_calls = 0
        self.full_get_calls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)

    async def get_owner_binding(self, job_id: str):
        self.owner_binding_calls += 1
        return await self.delegate.get_owner_binding(job_id)

    async def get(self, job_id: str):
        self.full_get_calls += 1
        return await self.delegate.get(job_id)


def _credential(*, capabilities: tuple[str, ...]) -> RunnerCredential:
    return RunnerCredential(
        node_id="local",
        principal=PRINCIPAL,
        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
        token_prefix=TOKEN[:8],
        protocol_capabilities=capabilities,
        created_at=NOW,
        rotated_at=NOW,
    )


def _headers(
    *,
    instance_id: str = PRINCIPAL.instance_id,
    epoch: int = PRINCIPAL.epoch,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-RiftX-Node-ID": "local",
        "X-RiftX-Runner-Instance-ID": instance_id,
        "X-RiftX-Runner-Epoch": str(epoch),
    }


@asynccontextmanager
async def _api(
    tmp_path: Path,
    *,
    capabilities: tuple[str, ...] = (AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,),
    job: AuditPreflightJob | None = None,
) -> AsyncIterator[
    tuple[
        httpx.AsyncClient,
        Database,
        SQLAlchemyAuditPreflightRepository,
        SpyPreflightRepository,
        AuditPreflightRunnerService,
    ]
]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runner-preflight.db'}")
    await database.create_schema()
    repository = SQLAlchemyAuditPreflightRepository(database.session_factory)
    await repository.create(job or _pending_job())
    spy = SpyPreflightRepository(repository)
    service = AuditPreflightRunnerService(
        repository=spy,  # type: ignore[arg-type]
        credentials=FakeCredentialRepository(  # type: ignore[arg-type]
            _credential(capabilities=capabilities)
        ),
        lease_duration=timedelta(seconds=30),
        clock=lambda: NOW,
    )
    app = FastAPI()
    app.state.control_plane = SimpleNamespace(audit_preflight_runner_service=service)
    install_error_handlers(app)
    app.include_router(router, prefix="/api/v1")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=_headers(),
        ) as client:
            yield client, database, repository, spy, service
    finally:
        await database.dispose()


async def _poll(client: httpx.AsyncClient) -> AuditPreflightDispatchEnvelope:
    response = await client.get("/api/v1/runner/audit-preflight/next")
    assert response.status_code == 200
    dispatch = response.json()["dispatch"]
    assert dispatch is not None
    return AuditPreflightDispatchEnvelope.model_validate_json(
        json.dumps(dispatch, sort_keys=True, separators=(",", ":"))
    )


def _identity(
    dispatch: AuditPreflightDispatchEnvelope,
    *,
    lease: AuditPreflightLeaseEnvelope,
    state_version: int,
) -> dict[str, object]:
    return {
        "owner_kind": "preflight_job",
        "owner": dispatch.owner.model_dump(mode="json"),
        "lease": lease.model_dump(mode="json"),
        "state_version": state_version,
        "capsule_id": dispatch.capsule_id,
    }


@pytest.mark.asyncio
async def test_runner_preflight_lifecycle_renews_starts_finishes_and_replays(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, database, _repository, _spy, _service):
        dispatch = await _poll(client)
        renewed = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/lease",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=dispatch.state_version,
                ),
                "schema_version": "riftx.audit-preflight-renew-request/v1",
            },
        )
        assert renewed.status_code == 200
        grant = renewed.json()
        renewed_lease = AuditPreflightLeaseEnvelope(
            owner=dispatch.owner,
            runner_principal=PRINCIPAL,
            lease_id=dispatch.lease.lease_id,
            lease_expires_at=datetime.fromisoformat(grant["lease_expires_at"]),
            expected_state_version=grant["state_version"],
            output_contract_digest=AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
            lease_envelope_digest=grant["lease_envelope_digest"],
        )

        start_payload = {
            **_identity(
                dispatch,
                lease=renewed_lease,
                state_version=grant["state_version"],
            ),
            "schema_version": "riftx.audit-preflight-start-request/v1",
            "capsule_prepare_proof_digest": _digest("prepare"),
        }
        started = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/start",
            json=start_payload,
        )
        start_replay = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/start",
            json=start_payload,
        )
        assert started.status_code == start_replay.status_code == 200
        assert started.json() == start_replay.json()

        exit_receipt = AuditPreflightExitReceipt(
            job_id=dispatch.owner.job_id,
            effect_owner_digest=dispatch.owner.effect_owner_digest,
            lease_envelope_digest=renewed_lease.lease_envelope_digest,
            capsule_id=dispatch.capsule_id,
            runner_principal=PRINCIPAL,
            backend_id=dispatch.owner.backend_id,
            image_digest=dispatch.owner.image_digest,
            policy_digest=dispatch.owner.policy_digest,
            process_identity_digest=_digest("process"),
            terminal_state=AuditPreflightExitTerminalState.FAILED,
            received_at=NOW + timedelta(seconds=5),
        )
        finish_payload = {
            **_identity(
                dispatch,
                lease=renewed_lease,
                state_version=started.json()["state_version"],
            ),
            "schema_version": "riftx.audit-preflight-finish-request/v1",
            "status": "failed",
            "result": None,
            "safe_error_code": "audit_source_ingest_failed",
            "exit_receipt": exit_receipt.model_dump(mode="json"),
        }
        finished = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/finish",
            json=finish_payload,
        )
        replay = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/finish",
            json=finish_payload,
        )
        drift = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/finish",
            json={**finish_payload, "safe_error_code": "different_failure"},
        )

        assert finished.status_code == replay.status_code == 200
        assert finished.json() == replay.json()
        assert finished.json()["status"] == "failed"
        assert drift.status_code == 409
        assert drift.json()["error"]["code"] == "audit_preflight_state_conflict"
        async with database.session_factory() as session:
            assert await session.scalar(text("SELECT COUNT(*) FROM runner_commands")) == 0
            assert (
                await session.scalar(text("SELECT COUNT(*) FROM audit_preflight_exit_receipts"))
                == 1
            )


@pytest.mark.parametrize(
    ("terminal_status", "blocking", "safe_error_code"),
    [
        (AuditPreflightJobStatus.SUCCEEDED, False, None),
        (
            AuditPreflightJobStatus.REJECTED,
            True,
            "audit_preflight_blocked",
        ),
    ],
)
@pytest.mark.asyncio
async def test_finish_persists_success_or_rejection_result_and_exit_receipt(
    tmp_path: Path,
    terminal_status: AuditPreflightJobStatus,
    blocking: bool,
    safe_error_code: str | None,
) -> None:
    async with _api(tmp_path) as (client, _database, repository, _spy, _service):
        dispatch = await _poll(client)
        started = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/start",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=dispatch.state_version,
                ),
                "schema_version": "riftx.audit-preflight-start-request/v1",
                "capsule_prepare_proof_digest": _digest("prepare"),
            },
        )
        assert started.status_code == 200
        result = _result(dispatch, blocking=blocking)
        receipt = AuditPreflightExitReceipt(
            job_id=dispatch.owner.job_id,
            effect_owner_digest=dispatch.owner.effect_owner_digest,
            lease_envelope_digest=dispatch.lease.lease_envelope_digest,
            capsule_id=dispatch.capsule_id,
            runner_principal=PRINCIPAL,
            backend_id=dispatch.owner.backend_id,
            image_digest=dispatch.owner.image_digest,
            policy_digest=dispatch.owner.policy_digest,
            process_identity_digest=_digest("terminal-process"),
            result_digest=result.result_digest,
            terminal_state=AuditPreflightExitTerminalState(terminal_status.value),
            received_at=result.completed_at,
        )
        response = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/finish",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=started.json()["state_version"],
                ),
                "schema_version": "riftx.audit-preflight-finish-request/v1",
                "status": terminal_status.value,
                "result": result.model_dump(mode="json"),
                "safe_error_code": safe_error_code,
                "exit_receipt": receipt.model_dump(mode="json"),
            },
        )
        assert response.status_code == 200
        persisted = await repository.get(dispatch.owner.job_id)
        assert persisted is not None
        assert persisted.status is terminal_status
        assert persisted.result_digest == result.result_digest
        assert persisted.exit_receipt_digest == receipt.receipt_digest


@pytest.mark.asyncio
async def test_capability_and_principal_fail_before_claim_or_owner_lookup(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path, capabilities=()) as (
        client,
        _database,
        repository,
        spy,
        _service,
    ):
        missing = await client.get("/api/v1/runner/audit-preflight/next")
        mismatched = await client.get(
            "/api/v1/runner/audit-preflight/next",
            headers=_headers(instance_id="wrong-runner"),
        )

        assert missing.status_code == mismatched.status_code == 409
        assert missing.json()["error"]["code"] == "runner_protocol_capability_missing"
        assert mismatched.json() == missing.json()
        assert spy.owner_binding_calls == spy.full_get_calls == 0
        persisted = await repository.get("preflight-job-1")
        assert persisted is not None
        assert persisted.status is AuditPreflightJobStatus.PENDING


@pytest.mark.asyncio
async def test_claim_response_loss_replays_only_to_same_live_runner_owner(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, _database, repository, spy, service):
        first = await _poll(client)
        first_state = await repository.get(first.owner.job_id)
        replay = await _poll(client)
        replay_state = await repository.get(first.owner.job_id)

        assert replay == first
        assert replay_state == first_state
        assert replay.state_version == 2

        other_principal = RunnerPrincipal(instance_id="other-runner", epoch=4)
        other_service = AuditPreflightRunnerService(
            repository=spy,  # type: ignore[arg-type]
            credentials=FakeCredentialRepository(  # type: ignore[arg-type]
                RunnerCredential(
                    node_id="local",
                    principal=other_principal,
                    token_hash=_digest("other-token"),
                    token_prefix="other",
                    protocol_capabilities=(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,),
                    created_at=NOW,
                    rotated_at=NOW,
                )
            ),
            clock=lambda: NOW,
        )
        assert (
            await other_service.poll(
                node_id="local",
                principal=other_principal,
                protocol_capabilities=(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,),
            )
            is None
        )

        expired_service = AuditPreflightRunnerService(
            repository=spy,  # type: ignore[arg-type]
            credentials=FakeCredentialRepository(  # type: ignore[arg-type]
                _credential(capabilities=(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,))
            ),
            clock=lambda: NOW + timedelta(seconds=31),
        )
        assert (
            await expired_service.poll(
                node_id="local",
                principal=PRINCIPAL,
                protocol_capabilities=(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,),
            )
            is None
        )


@pytest.mark.asyncio
async def test_wrong_owner_and_cross_wire_reject_before_restricted_job_load(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, _database, repository, spy, _service):
        dispatch = await _poll(client)
        foreign_owner = AuditPreflightEffectOwner(
            **{
                **dispatch.owner.model_dump(
                    mode="python",
                    exclude={"effect_owner_digest"},
                ),
                "operator_principal_id": "foreign-operator",
            }
        )
        foreign_lease = AuditPreflightLeaseEnvelope(
            owner=foreign_owner,
            runner_principal=PRINCIPAL,
            lease_id=dispatch.lease.lease_id,
            lease_expires_at=dispatch.lease.lease_expires_at,
            expected_state_version=dispatch.state_version,
            output_contract_digest=AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
        )
        spy.owner_binding_calls = spy.full_get_calls = 0
        wrong_owner = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/start",
            json={
                "schema_version": "riftx.audit-preflight-start-request/v1",
                "owner_kind": "preflight_job",
                "owner": foreign_owner.model_dump(mode="json"),
                "lease": foreign_lease.model_dump(mode="json"),
                "state_version": dispatch.state_version,
                "capsule_id": dispatch.capsule_id,
                "capsule_prepare_proof_digest": _digest("prepare"),
            },
        )
        assert wrong_owner.status_code == 409
        assert wrong_owner.json()["error"]["code"] == "audit_preflight_owner_mismatch"
        assert spy.owner_binding_calls == 1
        assert spy.full_get_calls == 0

        spy.owner_binding_calls = spy.full_get_calls = 0
        wrong_path = await client.post(
            "/api/v1/runner/audit-preflight/different-job/start",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=dispatch.state_version,
                ),
                "schema_version": "riftx.audit-preflight-start-request/v1",
                "capsule_prepare_proof_digest": _digest("prepare"),
            },
        )
        assert wrong_path.status_code == 409
        assert wrong_path.json()["error"]["code"] == "audit_preflight_owner_mismatch"
        assert spy.owner_binding_calls == spy.full_get_calls == 0

        spy.owner_binding_calls = spy.full_get_calls = 0
        cross_wire = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/start",
            json={
                "lease_id": dispatch.lease.lease_id,
                "state_version": dispatch.state_version,
                "binding_digest": _digest("ordinary-runner-binding"),
            },
        )
        assert cross_wire.status_code == 422
        assert spy.owner_binding_calls == spy.full_get_calls == 0
        persisted = await repository.get(dispatch.owner.job_id)
        assert persisted is not None
        assert persisted.status is AuditPreflightJobStatus.CLAIMED


@pytest.mark.asyncio
async def test_cancel_fence_beats_finish_then_stop_receipt_converges_and_replays(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, _database, repository, _spy, _service):
        dispatch = await _poll(client)
        started = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/start",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=dispatch.state_version,
                ),
                "schema_version": "riftx.audit-preflight-start-request/v1",
                "capsule_prepare_proof_digest": _digest("prepare"),
            },
        )
        assert started.status_code == 200
        running = await repository.get(dispatch.owner.job_id)
        assert running is not None
        cancelling = AuditPreflightJob.model_validate(
            {
                **running.model_dump(mode="python"),
                "status": AuditPreflightJobStatus.CANCELLING,
                "state_version": running.state_version + 1,
                "updated_at": NOW + timedelta(seconds=4),
            }
        )
        await repository.compare_and_set(previous=running, updated=cancelling)

        exit_receipt = AuditPreflightExitReceipt(
            job_id=dispatch.owner.job_id,
            effect_owner_digest=dispatch.owner.effect_owner_digest,
            lease_envelope_digest=dispatch.lease.lease_envelope_digest,
            capsule_id=dispatch.capsule_id,
            runner_principal=PRINCIPAL,
            backend_id=dispatch.owner.backend_id,
            image_digest=dispatch.owner.image_digest,
            policy_digest=dispatch.owner.policy_digest,
            process_identity_digest=_digest("late-exit"),
            terminal_state=AuditPreflightExitTerminalState.FAILED,
            received_at=NOW + timedelta(seconds=3),
        )
        late_finish = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/finish",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=running.state_version,
                ),
                "schema_version": "riftx.audit-preflight-finish-request/v1",
                "status": "failed",
                "result": None,
                "safe_error_code": "audit_source_ingest_failed",
                "exit_receipt": exit_receipt.model_dump(mode="json"),
            },
        )
        assert late_finish.status_code == 409
        assert late_finish.json()["error"]["code"] == ("audit_preflight_cancel_requested")

        renewed = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/lease",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=cancelling.state_version,
                ),
                "schema_version": "riftx.audit-preflight-renew-request/v1",
            },
        )
        assert renewed.status_code == 200
        assert renewed.json()["status"] == "cancelling"
        cancellation_lease = AuditPreflightLeaseEnvelope(
            owner=dispatch.owner,
            runner_principal=PRINCIPAL,
            lease_id=dispatch.lease.lease_id,
            lease_expires_at=datetime.fromisoformat(renewed.json()["lease_expires_at"]),
            expected_state_version=renewed.json()["state_version"],
            output_contract_digest=AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
            lease_envelope_digest=renewed.json()["lease_envelope_digest"],
        )

        stop_receipt = AuditPreflightStopReceipt(
            job_id=dispatch.owner.job_id,
            effect_owner_digest=dispatch.owner.effect_owner_digest,
            lease_envelope_digest=cancellation_lease.lease_envelope_digest,
            capsule_id=dispatch.capsule_id,
            runner_principal=PRINCIPAL,
            backend_id=dispatch.owner.backend_id,
            image_digest=dispatch.owner.image_digest,
            policy_digest=dispatch.owner.policy_digest,
            disposition=AuditPreflightStopDisposition.STOPPED,
            process_identity_digest=_digest("stopped-process"),
            observed_terminal_state=AuditPreflightObservedTerminalState.CANCELLED,
            received_at=NOW + timedelta(seconds=5),
        )
        stop_payload = {
            **_identity(
                dispatch,
                lease=cancellation_lease,
                state_version=renewed.json()["state_version"],
            ),
            "schema_version": "riftx.audit-preflight-stop-request/v1",
            "status": "cancelled",
            "safe_error_code": None,
            "stop_receipt": stop_receipt.model_dump(mode="json"),
        }
        stopped = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/stop",
            json=stop_payload,
        )
        replay = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/stop",
            json=stop_payload,
        )
        assert stopped.status_code == replay.status_code == 200
        assert stopped.json() == replay.json()
        assert stopped.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_reconciler_seams_fence_expiry_and_project_saved_stop_receipt(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, _database, _repository, _spy, service):
        dispatch = await _poll(client)
        unknown = await service.mark_expired_outcome_unknown(
            dispatch.owner.job_id,
            observed_at=NOW + timedelta(seconds=31),
        )
        replayed_unknown = await service.mark_expired_outcome_unknown(
            dispatch.owner.job_id,
            observed_at=NOW + timedelta(seconds=40),
        )
        assert unknown.status is AuditPreflightJobStatus.OUTCOME_UNKNOWN
        assert replayed_unknown == unknown

        stop_receipt = AuditPreflightStopReceipt(
            job_id=dispatch.owner.job_id,
            effect_owner_digest=dispatch.owner.effect_owner_digest,
            lease_envelope_digest=dispatch.lease.lease_envelope_digest,
            capsule_id=None,
            runner_principal=PRINCIPAL,
            backend_id=dispatch.owner.backend_id,
            image_digest=dispatch.owner.image_digest,
            policy_digest=dispatch.owner.policy_digest,
            disposition=AuditPreflightStopDisposition.NEVER_CREATED,
            never_created_proof_digest=_digest("never-created"),
            observed_terminal_state=AuditPreflightObservedTerminalState.NOT_CREATED,
            received_at=NOW + timedelta(seconds=41),
        )
        converged = await service.converge_stop_receipt(
            dispatch.owner.job_id,
            state_version=unknown.state_version,
            status=AuditPreflightJobStatus.CANCELLED,
            safe_error_code=None,
            stop_receipt=stop_receipt,
        )
        replay = await service.converge_stop_receipt(
            dispatch.owner.job_id,
            state_version=unknown.state_version,
            status=AuditPreflightJobStatus.CANCELLED,
            safe_error_code=None,
            stop_receipt=stop_receipt,
        )
        assert converged.status is AuditPreflightJobStatus.CANCELLED
        assert converged.state_version == unknown.state_version + 2
        assert replay == converged


@pytest.mark.asyncio
async def test_reconcile_batch_expires_pending_without_redispatch(
    tmp_path: Path,
) -> None:
    expired = _pending_job(
        created_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1),
    )
    async with _api(tmp_path, job=expired) as (
        _client,
        _database,
        repository,
        _spy,
        service,
    ):
        assert await service.reconcile_batch() == 1
        persisted = await repository.get(expired.job_id)
        assert persisted is not None
        assert persisted.status is AuditPreflightJobStatus.CANCELLED
        assert persisted.never_created_proof_digest is not None
        assert persisted.attempt == 0
        assert await service.reconcile_batch() == 0


@pytest.mark.asyncio
async def test_reconcile_batch_never_materializes_restricted_request(
    tmp_path: Path,
) -> None:
    expired = _pending_job(
        created_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1),
    )
    async with _api(tmp_path, job=expired) as (
        _client,
        database,
        _repository,
        spy,
        service,
    ):
        async with database.session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE audit_preflight_job_requests "
                    "SET canonical_json = '{not-json' WHERE job_id = :job_id"
                ),
                {"job_id": expired.job_id},
            )

        statements: list[str] = []

        def capture_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement.lower())

        event.listen(
            database.engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        )
        try:
            assert await service.reconcile_batch() == 1
        finally:
            event.remove(
                database.engine.sync_engine,
                "before_cursor_execute",
                capture_statement,
            )

        assert spy.full_get_calls == 0
        assert not any("audit_preflight_job_requests" in statement for statement in statements)
        async with database.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, state_version, never_created_proof_digest "
                        "FROM audit_preflight_jobs WHERE id = :job_id"
                    ),
                    {"job_id": expired.job_id},
                )
            ).one()
        assert row.status == AuditPreflightJobStatus.CANCELLED.value
        assert row.state_version == 2
        assert row.never_created_proof_digest is not None


@pytest.mark.asyncio
async def test_reconciler_terminalizes_expired_pending_with_db_no_effect_proof(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (
        _client,
        database,
        _repository,
        _spy,
        service,
    ):
        expired_at = NOW + timedelta(hours=1)
        cancelled = await service.expire_pending_never_created(
            "preflight-job-1",
            observed_at=expired_at,
        )
        replay = await service.expire_pending_never_created(
            "preflight-job-1",
            observed_at=expired_at + timedelta(minutes=1),
        )

        assert cancelled.status is AuditPreflightJobStatus.CANCELLED
        assert cancelled.state_version == 2
        assert cancelled.never_created_proof_digest is not None
        assert replay == cancelled
        async with database.session_factory() as session:
            assert await session.scalar(text("SELECT COUNT(*) FROM runner_commands")) == 0


@pytest.mark.asyncio
async def test_finish_timeout_recovery_accepts_only_immediate_fenced_lease_version(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, _database, _repository, _spy, service):
        dispatch = await _poll(client)
        started = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/start",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=dispatch.state_version,
                ),
                "schema_version": "riftx.audit-preflight-start-request/v1",
                "capsule_prepare_proof_digest": _digest("prepare"),
            },
        )
        assert started.status_code == 200
        renewed = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/lease",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=started.json()["state_version"],
                ),
                "schema_version": "riftx.audit-preflight-renew-request/v1",
            },
        )
        assert renewed.status_code == 200
        renewed_lease = AuditPreflightLeaseEnvelope(
            owner=dispatch.owner,
            runner_principal=PRINCIPAL,
            lease_id=dispatch.lease.lease_id,
            lease_expires_at=datetime.fromisoformat(renewed.json()["lease_expires_at"]),
            expected_state_version=renewed.json()["state_version"],
            output_contract_digest=AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
            lease_envelope_digest=renewed.json()["lease_envelope_digest"],
        )
        unknown = await service.mark_expired_outcome_unknown(
            dispatch.owner.job_id,
            observed_at=renewed_lease.lease_expires_at + timedelta(seconds=1),
        )
        assert unknown.state_version == renewed.json()["state_version"] + 1

        exit_receipt = AuditPreflightExitReceipt(
            job_id=dispatch.owner.job_id,
            effect_owner_digest=dispatch.owner.effect_owner_digest,
            lease_envelope_digest=renewed_lease.lease_envelope_digest,
            capsule_id=dispatch.capsule_id,
            runner_principal=PRINCIPAL,
            backend_id=dispatch.owner.backend_id,
            image_digest=dispatch.owner.image_digest,
            policy_digest=dispatch.owner.policy_digest,
            process_identity_digest=_digest("recovered-process"),
            terminal_state=AuditPreflightExitTerminalState.FAILED,
            received_at=NOW + timedelta(seconds=20),
        )
        finish_payload = {
            **_identity(
                dispatch,
                lease=renewed_lease,
                state_version=renewed.json()["state_version"],
            ),
            "schema_version": "riftx.audit-preflight-finish-request/v1",
            "status": "failed",
            "result": None,
            "safe_error_code": "audit_source_ingest_failed",
            "exit_receipt": exit_receipt.model_dump(mode="json"),
        }
        stale = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/finish",
            json={
                **finish_payload,
                "state_version": renewed.json()["state_version"] - 1,
            },
        )
        recovered = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/finish",
            json=finish_payload,
        )
        replay = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/finish",
            json=finish_payload,
        )

        assert stale.status_code == 409
        assert recovered.status_code == replay.status_code == 200
        assert recovered.json() == replay.json()
        assert recovered.json()["state_version"] == (renewed.json()["state_version"] + 2)


@pytest.mark.asyncio
async def test_start_timeout_old_callback_state_fails_closed_then_reconciler_projects(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, _database, _repository, _spy, service):
        dispatch = await _poll(client)
        started = await service.start(
            dispatch.owner.job_id,
            node_id="local",
            principal=PRINCIPAL,
            protocol_capabilities=(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,),
            owner=dispatch.owner,
            lease=dispatch.lease,
            state_version=dispatch.state_version,
            capsule_id=dispatch.capsule_id,
            capsule_prepare_proof_digest=_digest("prepare"),
        )
        unknown = await service.mark_expired_outcome_unknown(
            dispatch.owner.job_id,
            observed_at=dispatch.lease.lease_expires_at + timedelta(seconds=1),
        )
        assert unknown.state_version == started.state_version + 1

        exit_receipt = AuditPreflightExitReceipt(
            job_id=dispatch.owner.job_id,
            effect_owner_digest=dispatch.owner.effect_owner_digest,
            lease_envelope_digest=dispatch.lease.lease_envelope_digest,
            capsule_id=dispatch.capsule_id,
            runner_principal=PRINCIPAL,
            backend_id=dispatch.owner.backend_id,
            image_digest=dispatch.owner.image_digest,
            policy_digest=dispatch.owner.policy_digest,
            process_identity_digest=_digest("start-timeout-process"),
            terminal_state=AuditPreflightExitTerminalState.FAILED,
            received_at=NOW + timedelta(seconds=20),
        )
        stale = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/finish",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=dispatch.state_version,
                ),
                "schema_version": "riftx.audit-preflight-finish-request/v1",
                "status": "failed",
                "result": None,
                "safe_error_code": "audit_source_ingest_failed",
                "exit_receipt": exit_receipt.model_dump(mode="json"),
            },
        )
        assert stale.status_code == 409

        projected = await service.converge_finish_receipt(
            dispatch.owner.job_id,
            state_version=unknown.state_version,
            status=AuditPreflightJobStatus.FAILED,
            result=None,
            safe_error_code="audit_source_ingest_failed",
            exit_receipt=exit_receipt,
        )
        assert projected.status is AuditPreflightJobStatus.FAILED


@pytest.mark.asyncio
async def test_expired_cancelling_becomes_unknown_then_recovered_stop_cancels(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, _database, repository, _spy, service):
        dispatch = await _poll(client)
        started = await service.start(
            dispatch.owner.job_id,
            node_id="local",
            principal=PRINCIPAL,
            protocol_capabilities=(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,),
            owner=dispatch.owner,
            lease=dispatch.lease,
            state_version=dispatch.state_version,
            capsule_id=dispatch.capsule_id,
            capsule_prepare_proof_digest=_digest("prepare"),
        )
        running = await repository.get(dispatch.owner.job_id)
        assert running is not None
        cancelling = AuditPreflightJob.model_validate(
            {
                **running.model_dump(mode="python"),
                "status": AuditPreflightJobStatus.CANCELLING,
                "state_version": started.state_version + 1,
                "updated_at": NOW + timedelta(seconds=4),
            }
        )
        await repository.compare_and_set(previous=running, updated=cancelling)
        renewed = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/lease",
            json={
                **_identity(
                    dispatch,
                    lease=dispatch.lease,
                    state_version=cancelling.state_version,
                ),
                "schema_version": "riftx.audit-preflight-renew-request/v1",
            },
        )
        assert renewed.status_code == 200
        cancelling_lease = AuditPreflightLeaseEnvelope(
            owner=dispatch.owner,
            runner_principal=PRINCIPAL,
            lease_id=dispatch.lease.lease_id,
            lease_expires_at=datetime.fromisoformat(renewed.json()["lease_expires_at"]),
            expected_state_version=renewed.json()["state_version"],
            output_contract_digest=AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
            lease_envelope_digest=renewed.json()["lease_envelope_digest"],
        )
        observed_at = cancelling_lease.lease_expires_at + timedelta(seconds=1)
        unknown = await service.mark_expired_outcome_unknown(
            dispatch.owner.job_id,
            observed_at=observed_at,
        )
        assert unknown.status is AuditPreflightJobStatus.OUTCOME_UNKNOWN

        stop_receipt = AuditPreflightStopReceipt(
            job_id=dispatch.owner.job_id,
            effect_owner_digest=dispatch.owner.effect_owner_digest,
            lease_envelope_digest=cancelling_lease.lease_envelope_digest,
            capsule_id=dispatch.capsule_id,
            runner_principal=PRINCIPAL,
            backend_id=dispatch.owner.backend_id,
            image_digest=dispatch.owner.image_digest,
            policy_digest=dispatch.owner.policy_digest,
            disposition=AuditPreflightStopDisposition.STOPPED,
            process_identity_digest=_digest("cancel-recovery-process"),
            observed_terminal_state=AuditPreflightObservedTerminalState.CANCELLED,
            received_at=observed_at + timedelta(seconds=1),
        )
        stop_payload = {
            **_identity(
                dispatch,
                lease=cancelling_lease,
                state_version=renewed.json()["state_version"],
            ),
            "schema_version": "riftx.audit-preflight-stop-request/v1",
            "status": "cancelled",
            "safe_error_code": None,
            "stop_receipt": stop_receipt.model_dump(mode="json"),
        }
        stopped = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/stop",
            json=stop_payload,
        )
        replay = await client.post(
            f"/api/v1/runner/audit-preflight/{dispatch.owner.job_id}/stop",
            json=stop_payload,
        )
        assert stopped.status_code == replay.status_code == 200
        assert stopped.json() == replay.json()
        assert stopped.json()["state_version"] == (renewed.json()["state_version"] + 3)
