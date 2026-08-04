from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from riftx.domain.audit import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    AuditPreflightEffectOwner,
    AuditPreflightJobStatus,
    AuditPreflightLeaseEnvelope,
    AuditPreflightObservedTerminalState,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightStopDisposition,
    AuditPreflightTarget,
    PreflightRequest,
)
from riftx.domain.audit_preflight_wire import (
    AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
    AuditPreflightCallbackAck,
    AuditPreflightDispatchEnvelope,
)
from riftx.domain.runner import RunnerPrincipal
from riftx.runner.preflight import (
    AuditPreflightCapsuleReference,
    AuditPreflightCapsuleStartEvidence,
    AuditPreflightCapsuleStopEvidence,
    AuditPreflightRecoveryAction,
    AuditPreflightRunnerJournal,
    AuditPreflightRunnerJournalConflict,
    AuditPreflightRunnerRecoveryRequired,
    DurableAuditPreflightCapsuleController,
    audit_preflight_dispatch_digest,
)

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _dispatch(
    *,
    job_id: str = "job-1",
    repository_path: str = "/srv/source/repo",
) -> AuditPreflightDispatchEnvelope:
    request = PreflightRequest(
        client_request_id="123e4567-e89b-42d3-a456-426614174000",
        repository_path=repository_path,
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
        job_id=job_id,
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
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=1),
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


class _FakeBackend:
    def __init__(
        self,
        journal: AuditPreflightRunnerJournal,
        *,
        job_id: str,
        dispatch_digest: str,
    ) -> None:
        self.journal = journal
        self.job_id = job_id
        self.dispatch_digest = dispatch_digest
        self.prepare_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.cleanup_calls = 0
        self.fail_prepare = False
        self.fail_start = False

    async def prepare(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
    ) -> AuditPreflightCapsuleReference:
        self.prepare_calls += 1
        record = await self.journal.get(self.job_id)
        assert record is not None
        assert record.prepare_intent_at is not None
        assert record.capsule is None
        if self.fail_prepare:
            raise RuntimeError("simulated crash after prepare intent")
        return AuditPreflightCapsuleReference(
            capsule_id=dispatch.capsule_id,
            locator="docker-container-1",
            prepare_proof_digest=_digest("prepare-proof"),
        )

    async def start(
        self,
        capsule: AuditPreflightCapsuleReference,
    ) -> AuditPreflightCapsuleStartEvidence:
        self.start_calls += 1
        record = await self.journal.get(self.job_id)
        assert record is not None
        assert record.start_intent_at is not None
        assert record.capsule == capsule
        assert record.start_evidence is None
        if self.fail_start:
            raise RuntimeError("simulated crash after start intent")
        return AuditPreflightCapsuleStartEvidence(
            capsule_id=capsule.capsule_id,
            process_identity_digest=_digest("process"),
            observed_state="running",
            observed_at=NOW + timedelta(seconds=2),
        )

    async def stop(
        self,
        *,
        capsule_id: str,
        capsule: AuditPreflightCapsuleReference | None,
    ) -> AuditPreflightCapsuleStopEvidence:
        self.stop_calls += 1
        record = await self.journal.get(self.job_id)
        assert record is not None
        assert record.stop_intent_at is not None
        assert record.capsule == capsule
        # A prepared container is an actual effect even if start never happened.
        assert capsule is not None
        return AuditPreflightCapsuleStopEvidence(
            disposition=AuditPreflightStopDisposition.STOPPED,
            capsule_id=capsule_id,
            process_identity_digest=_digest("created-container-process"),
            observed_terminal_state=AuditPreflightObservedTerminalState.CANCELLED,
            observed_at=NOW + timedelta(seconds=3),
        )

    async def cleanup(
        self,
        *,
        capsule_id: str,
        capsule: AuditPreflightCapsuleReference | None,
    ) -> None:
        self.cleanup_calls += 1
        record = await self.journal.get(self.job_id)
        assert record is not None
        assert record.stop_ack is not None
        assert record.cleanup_intent_at is not None
        assert record.cleaned_at is None
        assert capsule_id == record.capsule_id
        assert capsule == record.capsule


@pytest.mark.asyncio
async def test_dispatch_replay_is_exact_and_digest_drift_fails_closed(tmp_path: Path) -> None:
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    dispatch = _dispatch()

    admitted, created = await journal.admit(dispatch, admitted_at=NOW)
    replayed, replay_created = await journal.admit(dispatch, admitted_at=NOW)

    assert created is True
    assert replay_created is False
    assert replayed == admitted
    assert admitted.recovery_action is AuditPreflightRecoveryAction.PREPARE

    drifted = _dispatch(repository_path="/srv/source/other")
    with pytest.raises(AuditPreflightRunnerJournalConflict):
        await journal.admit(drifted, admitted_at=NOW)


@pytest.mark.asyncio
async def test_prepare_and_start_intents_are_durable_before_backend_io(tmp_path: Path) -> None:
    path = tmp_path / "preflight.json"
    journal = AuditPreflightRunnerJournal(path)
    dispatch = _dispatch()
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch, admitted_at=NOW)
    backend = _FakeBackend(journal, job_id="job-1", dispatch_digest=digest)
    controller = DurableAuditPreflightCapsuleController(journal=journal, backend=backend)

    prepared = await controller.prepare(
        "job-1",
        digest,
        recorded_at=NOW + timedelta(seconds=1),
    )
    assert prepared.capsule is not None
    assert prepared.recovery_action is AuditPreflightRecoveryAction.START

    started = await controller.start(
        "job-1",
        digest,
        recorded_at=NOW + timedelta(seconds=2),
    )
    assert started.start_evidence is not None
    assert started.recovery_action is AuditPreflightRecoveryAction.REPORT_START

    reopened = AuditPreflightRunnerJournal(path)
    recovered = await reopened.get("job-1")
    assert recovered == started
    assert backend.prepare_calls == 1
    assert backend.start_calls == 1


@pytest.mark.asyncio
async def test_uncertain_prepare_or_start_requires_probe_instead_of_replay(tmp_path: Path) -> None:
    path = tmp_path / "preflight.json"
    journal = AuditPreflightRunnerJournal(path)
    dispatch = _dispatch()
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch, admitted_at=NOW)
    backend = _FakeBackend(journal, job_id="job-1", dispatch_digest=digest)
    backend.fail_prepare = True
    controller = DurableAuditPreflightCapsuleController(journal=journal, backend=backend)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await controller.prepare(
            "job-1",
            digest,
            recorded_at=NOW + timedelta(seconds=1),
        )

    reopened = AuditPreflightRunnerJournal(path)
    record = await reopened.get("job-1")
    assert record is not None
    assert record.recovery_action is AuditPreflightRecoveryAction.PROBE_PREPARE
    restarted_backend = _FakeBackend(reopened, job_id="job-1", dispatch_digest=digest)
    restarted = DurableAuditPreflightCapsuleController(
        journal=reopened,
        backend=restarted_backend,
    )
    with pytest.raises(AuditPreflightRunnerRecoveryRequired) as exc_info:
        await restarted.prepare("job-1", digest)
    assert exc_info.value.action is AuditPreflightRecoveryAction.PROBE_PREPARE
    assert restarted_backend.prepare_calls == 0


@pytest.mark.asyncio
async def test_uncertain_start_requires_probe_instead_of_second_start(tmp_path: Path) -> None:
    path = tmp_path / "preflight.json"
    journal = AuditPreflightRunnerJournal(path)
    dispatch = _dispatch()
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch, admitted_at=NOW)
    backend = _FakeBackend(journal, job_id="job-1", dispatch_digest=digest)
    controller = DurableAuditPreflightCapsuleController(journal=journal, backend=backend)
    await controller.prepare(
        "job-1",
        digest,
        recorded_at=NOW + timedelta(seconds=1),
    )
    backend.fail_start = True

    with pytest.raises(RuntimeError, match="simulated crash"):
        await controller.start(
            "job-1",
            digest,
            recorded_at=NOW + timedelta(seconds=2),
        )

    reopened = AuditPreflightRunnerJournal(path)
    record = await reopened.get("job-1")
    assert record is not None
    assert record.recovery_action is AuditPreflightRecoveryAction.PROBE_START
    restarted_backend = _FakeBackend(reopened, job_id="job-1", dispatch_digest=digest)
    restarted = DurableAuditPreflightCapsuleController(
        journal=reopened,
        backend=restarted_backend,
    )
    with pytest.raises(AuditPreflightRunnerRecoveryRequired) as exc_info:
        await restarted.start("job-1", digest)
    assert exc_info.value.action is AuditPreflightRecoveryAction.PROBE_START
    assert restarted_backend.start_calls == 0


@pytest.mark.asyncio
async def test_created_but_not_started_capsule_stops_with_real_evidence_and_recovers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "preflight.json"
    journal = AuditPreflightRunnerJournal(path)
    dispatch = _dispatch()
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch, admitted_at=NOW)
    backend = _FakeBackend(journal, job_id="job-1", dispatch_digest=digest)
    controller = DurableAuditPreflightCapsuleController(journal=journal, backend=backend)
    await controller.prepare(
        "job-1",
        digest,
        recorded_at=NOW + timedelta(seconds=1),
    )

    stopped = await controller.stop(
        "job-1",
        digest,
        reason_code="audit_preflight_cancel_requested",
        recorded_at=NOW + timedelta(seconds=3),
    )

    assert stopped.start_intent_at is None
    assert stopped.stop_evidence is not None
    assert stopped.stop_evidence.disposition is AuditPreflightStopDisposition.STOPPED
    assert stopped.stop_evidence.never_created_proof_digest is None
    assert stopped.recovery_action is AuditPreflightRecoveryAction.REPORT_STOP
    assert backend.stop_calls == 1

    recovered = await AuditPreflightRunnerJournal(path).get("job-1")
    assert recovered == stopped


@pytest.mark.asyncio
async def test_cleanup_is_blocked_until_stop_ack_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "preflight.json"
    journal = AuditPreflightRunnerJournal(path)
    dispatch = _dispatch()
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch, admitted_at=NOW)
    backend = _FakeBackend(journal, job_id="job-1", dispatch_digest=digest)
    controller = DurableAuditPreflightCapsuleController(journal=journal, backend=backend)
    await controller.prepare(
        "job-1",
        digest,
        recorded_at=NOW + timedelta(seconds=1),
    )
    await controller.stop(
        "job-1",
        digest,
        reason_code="audit_preflight_cancel_requested",
        recorded_at=NOW + timedelta(seconds=3),
    )

    with pytest.raises(AuditPreflightRunnerJournalConflict):
        await controller.cleanup(
            "job-1",
            digest,
            recorded_at=NOW + timedelta(seconds=4),
        )
    assert backend.cleanup_calls == 0

    ack = AuditPreflightCallbackAck(
        job_id="job-1",
        status=AuditPreflightJobStatus.CANCELLED,
        state_version=3,
        finished_at=NOW + timedelta(seconds=4),
    )
    await journal.record_stop_ack(
        "job-1",
        digest,
        ack,
        recorded_at=NOW + timedelta(seconds=4),
    )

    reopened = AuditPreflightRunnerJournal(path)
    restarted_backend = _FakeBackend(reopened, job_id="job-1", dispatch_digest=digest)
    restarted = DurableAuditPreflightCapsuleController(
        journal=reopened,
        backend=restarted_backend,
    )
    cleaned = await restarted.cleanup(
        "job-1",
        digest,
        recorded_at=NOW + timedelta(seconds=5),
    )

    assert cleaned.cleaned_at == NOW + timedelta(seconds=5)
    assert cleaned.recovery_action is AuditPreflightRecoveryAction.NONE
    assert restarted_backend.cleanup_calls == 1
    assert await reopened.list_recoverable() == ()
