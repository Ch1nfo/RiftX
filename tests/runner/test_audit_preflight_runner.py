from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import riftx.runner.audit_preflight as audit_preflight_module
from riftx.audit.source_ingest import (
    PreparedSourceIngestCapsule,
    SourceIngestCapsuleRecord,
    SourceIngestExecutionResult,
    SourceIngestStopEvidence,
)
from riftx.audit.source_ingest_contract import (
    SourceIngestLanguageEstimate,
    SourceIngestWorkerOutcome,
    SourceIngestWorkerResult,
)
from riftx.config import AuditConfig, AuditSourceIngestConfig
from riftx.domain.audit import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    AuditPreflightBudgetStatus,
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
    AuditPreflightLeaseGrant,
    AuditPreflightStartGrant,
)
from riftx.domain.base import utc_now
from riftx.domain.runner import RunnerPrincipal
from riftx.runner.audit_preflight import (
    AuditPreflightCapsuleRecovery,
    AuditPreflightRunner,
    DockerAuditPreflightCapsuleBackend,
)
from riftx.runner.control_client import RunnerControlClientError
from riftx.runner.preflight import (
    AuditPreflightCapsuleReference,
    AuditPreflightCapsuleStartEvidence,
    AuditPreflightCapsuleStopEvidence,
    AuditPreflightRecoveryAction,
    AuditPreflightRunnerJournal,
    AuditPreflightRunnerJournalRecord,
    DurableAuditPreflightCapsuleController,
    audit_preflight_dispatch_digest,
)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _dispatch(
    *,
    job_id: str = "job-1",
    capsule_id: str = "capsule-1",
    lease_seconds: float = 120,
) -> AuditPreflightDispatchEnvelope:
    now = datetime.now(UTC)
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
        job_id=job_id,
        operator_principal_id="operator-1",
        authorization_scope_digest=_digest("authorization"),
        source_root_identity_digest=_digest("source-root"),
        request=request,
        backend_id="linux_container",
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    lease = AuditPreflightLeaseEnvelope(
        owner=owner,
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=1),
        lease_id="lease-1",
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        expected_state_version=2,
        output_contract_digest=AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
    )
    return AuditPreflightDispatchEnvelope(
        owner=owner,
        lease=lease,
        request=request,
        capsule_id=capsule_id,
        state_version=2,
    )


def _worker_result(
    dispatch: AuditPreflightDispatchEnvelope,
    outcome: SourceIngestWorkerOutcome,
) -> SourceIngestWorkerResult:
    common: dict[str, object] = {
        "outcome": outcome,
        "request_digest": dispatch.request.request_digest,
        "source_root_identity_digest": dispatch.owner.source_root_identity_digest,
        "repository_descriptor_identity_digest": _digest("repository-descriptor"),
    }
    if outcome is SourceIngestWorkerOutcome.FAILED:
        return SourceIngestWorkerResult(
            **common,
            safe_error_code="audit_source_ingest_failed",
        )
    blocking = outcome is SourceIngestWorkerOutcome.REJECTED
    return SourceIngestWorkerResult(
        **common,
        safe_error_code="audit_preflight_blocked" if blocking else None,
        source_mount_identity_digest=_digest("source-mount"),
        source_mount_proof_digest=_digest("source-mount-proof"),
        repository_identity_digest=_digest("repository"),
        content_identity_digest=_digest("content"),
        git_version="2.44.0",
        git_component_digest=_digest("git-component"),
        git_proof_digest=_digest("git-proof"),
        head_revision="a" * 40,
        resolved_revision="b" * 40,
        dirty=False,
        file_count=1,
        total_bytes=16,
        max_file_bytes=16,
        language_estimates=(
            SourceIngestLanguageEstimate(
                language_id="python",
                file_count=1,
                total_bytes=16,
            ),
        ),
        blocking_errors=("audit_preflight_blocked",) if blocking else (),
    )


class _FakeExecutionBackend:
    def __init__(
        self,
        *,
        dispatch: AuditPreflightDispatchEnvelope,
        journal: AuditPreflightRunnerJournal,
        outcome: SourceIngestWorkerOutcome = SourceIngestWorkerOutcome.SUCCEEDED,
        block_wait: bool = False,
    ) -> None:
        self.dispatch = dispatch
        self.journal = journal
        self.outcome = outcome
        self.block_wait = block_wait
        self.wait_gate = asyncio.Event()
        self.wait_started = asyncio.Event()
        self.prepare_calls = 0
        self.start_calls = 0
        self.wait_calls = 0
        self.stop_calls = 0
        self.cleanup_calls = 0
        self.orphan_stop_calls = 0
        self.ordinary_enqueue_calls = 0
        self.can_start_value = True
        self.recovery = AuditPreflightCapsuleRecovery()
        self.orphan_capsules: tuple[str, ...] = ()
        self.last_known_capsules: set[str] | None = None
        self.execution_prepare_proof_digest: str | None = None
        self.execution_process_identity_digest: str | None = None
        self.call_order: list[str] = []

    @property
    def capsule(self) -> AuditPreflightCapsuleReference:
        return AuditPreflightCapsuleReference(
            capsule_id=self.dispatch.capsule_id,
            locator=f"container-{self.dispatch.capsule_id}",
            prepare_proof_digest=_digest(f"prepare-{self.dispatch.capsule_id}"),
        )

    @property
    def start_evidence(self) -> AuditPreflightCapsuleStartEvidence:
        return AuditPreflightCapsuleStartEvidence(
            capsule_id=self.dispatch.capsule_id,
            process_identity_digest=_digest(f"process-{self.dispatch.capsule_id}"),
            observed_state="running",
            observed_at=utc_now(),
        )

    async def prepare(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
    ) -> AuditPreflightCapsuleReference:
        self.prepare_calls += 1
        self.call_order.append("prepare")
        record = await self._record()
        assert record.prepare_intent_at is not None
        assert record.capsule is None
        assert dispatch == self.dispatch
        return self.capsule

    async def start(
        self,
        capsule: AuditPreflightCapsuleReference,
    ) -> AuditPreflightCapsuleStartEvidence:
        self.start_calls += 1
        self.call_order.append("physical_start")
        record = await self._record()
        assert record.start_intent_at is not None
        assert record.start_evidence is None
        assert capsule == self.capsule
        return self.start_evidence

    async def wait(
        self,
        capsule: AuditPreflightCapsuleReference,
    ) -> SourceIngestExecutionResult:
        self.wait_calls += 1
        self.call_order.append("wait")
        record = await self._record()
        assert record.start_evidence is not None
        assert record.start_grant is not None
        assert capsule == self.capsule
        self.wait_started.set()
        if self.block_wait:
            await self.wait_gate.wait()
        return SourceIngestExecutionResult(
            worker_result=_worker_result(self.dispatch, self.outcome),
            prepare_proof_digest=(
                self.execution_prepare_proof_digest or self.capsule.prepare_proof_digest
            ),
            process_identity_digest=(
                self.execution_process_identity_digest
                or self.start_evidence.process_identity_digest
            ),
            container_id=self.capsule.locator,
            exit_code=0 if self.outcome is not SourceIngestWorkerOutcome.FAILED else 1,
        )

    async def stop(
        self,
        *,
        capsule_id: str,
        capsule: AuditPreflightCapsuleReference | None,
    ) -> AuditPreflightCapsuleStopEvidence:
        self.stop_calls += 1
        self.call_order.append("physical_stop")
        record = await self._record()
        assert record.stop_intent_at is not None
        assert capsule_id == self.dispatch.capsule_id
        assert capsule == self.capsule
        return AuditPreflightCapsuleStopEvidence(
            disposition=AuditPreflightStopDisposition.STOPPED,
            capsule_id=capsule_id,
            process_identity_digest=_digest(f"stopped-{capsule_id}"),
            observed_terminal_state=AuditPreflightObservedTerminalState.CANCELLED,
            observed_at=utc_now(),
        )

    async def cleanup(
        self,
        *,
        capsule_id: str,
        capsule: AuditPreflightCapsuleReference | None,
    ) -> None:
        self.cleanup_calls += 1
        self.call_order.append("cleanup")
        record = await self._record()
        assert record.callback_acknowledged
        assert record.cleanup_intent_at is not None
        assert record.cleaned_at is None
        assert capsule_id == self.dispatch.capsule_id
        assert capsule == record.capsule

    def can_start(self, capsule_id: str) -> bool:
        assert capsule_id == self.dispatch.capsule_id
        return self.can_start_value

    async def recover(
        self,
        record: AuditPreflightRunnerJournalRecord,
    ) -> AuditPreflightCapsuleRecovery:
        assert record.job_id == self.dispatch.owner.job_id
        return self.recovery

    async def reconcile_orphans(self, known_capsule_ids: set[str]) -> tuple[str, ...]:
        self.last_known_capsules = known_capsule_ids
        self.orphan_stop_calls += len(self.orphan_capsules)
        return self.orphan_capsules

    async def enqueue(self, _: object) -> None:
        self.ordinary_enqueue_calls += 1
        raise AssertionError("Audit Preflight must not use ordinary Runner enqueue")

    async def _record(self) -> AuditPreflightRunnerJournalRecord:
        record = await self.journal.get(self.dispatch.owner.job_id)
        assert record is not None
        return record


class _FakeControlClient:
    def __init__(
        self,
        *,
        journal: AuditPreflightRunnerJournal,
        backend: _FakeExecutionBackend,
        renew_mode: str = "renew",
        finish_failures: int = 0,
        finish_error: tuple[int, str] | None = None,
        stop_failures: int = 0,
        stop_error: tuple[int, str] | None = None,
    ) -> None:
        self.journal = journal
        self.backend = backend
        self.renew_mode = renew_mode
        self.finish_failures = finish_failures
        self.finish_error = finish_error
        self.stop_failures = stop_failures
        self.stop_error = stop_error
        self.poll_calls = 0
        self.renew_calls = 0
        self.start_calls = 0
        self.finish_calls = 0
        self.stop_calls = 0
        self.ordinary_enqueue_calls = 0
        self.finish_statuses: list[AuditPreflightJobStatus] = []
        self.stop_statuses: list[AuditPreflightJobStatus] = []
        self.stop_safe_error_codes: list[str | None] = []

    async def poll_audit_preflight(
        self,
        *,
        wait_seconds: float = 0,
    ) -> AuditPreflightDispatchEnvelope | None:
        self.poll_calls += 1
        assert wait_seconds >= 0
        return None

    async def renew_audit_preflight(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
    ) -> AuditPreflightLeaseGrant:
        self.renew_calls += 1
        if self.renew_mode == "lease_lost":
            raise RunnerControlClientError(
                409,
                "audit_preflight_lease_lost",
                "lease lost",
            )
        status = (
            AuditPreflightJobStatus.CANCELLING
            if self.renew_mode == "cancel"
            else AuditPreflightJobStatus.RUNNING
        )
        expires_at = min(
            dispatch.owner.expires_at,
            max(lease.lease_expires_at + timedelta(seconds=30), utc_now()),
        )
        renewed = AuditPreflightLeaseEnvelope(
            owner=dispatch.owner,
            runner_principal=lease.runner_principal,
            lease_id=lease.lease_id,
            lease_expires_at=expires_at,
            expected_state_version=state_version + 1,
            output_contract_digest=lease.output_contract_digest,
        )
        return AuditPreflightLeaseGrant(
            job_id=dispatch.owner.job_id,
            status=status,
            state_version=state_version + 1,
            lease_envelope_digest=renewed.lease_envelope_digest,
            lease_expires_at=expires_at,
            lease_duration_seconds=30.0,
        )

    async def start_audit_preflight(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        capsule_prepare_proof_digest: str,
    ) -> AuditPreflightStartGrant:
        self.start_calls += 1
        self.backend.call_order.append("start_ack")
        record = await self._record(dispatch)
        assert record.start_evidence is not None
        assert record.start_grant is None
        assert lease == record.lease_envelope
        assert capsule_prepare_proof_digest == self.backend.capsule.prepare_proof_digest
        return AuditPreflightStartGrant(
            job_id=dispatch.owner.job_id,
            capsule_id=dispatch.capsule_id,
            state_version=state_version + 1,
            started_at=utc_now(),
        )

    async def finish_audit_preflight(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        status: AuditPreflightJobStatus,
        result: object | None,
        safe_error_code: str | None,
        exit_receipt: object,
    ) -> AuditPreflightCallbackAck:
        self.finish_calls += 1
        self.finish_statuses.append(status)
        self.backend.call_order.append("finish_callback")
        record = await self._record(dispatch)
        assert record.terminal_observation is not None
        assert record.finish_ack is None
        assert self.backend.cleanup_calls == 0
        assert lease == record.lease_envelope
        assert result == record.terminal_observation.result
        assert safe_error_code == record.terminal_observation.safe_error_code
        assert exit_receipt == record.terminal_observation.exit_receipt
        if self.finish_calls == 1 and self.finish_error is not None:
            status_code, code = self.finish_error
            raise RunnerControlClientError(status_code, code, "finish fenced")
        if self.finish_calls <= self.finish_failures:
            raise RunnerControlClientError(503, "control_unavailable", "retry")
        return AuditPreflightCallbackAck(
            job_id=dispatch.owner.job_id,
            status=status,
            state_version=state_version + 1,
            finished_at=utc_now(),
        )

    async def stop_audit_preflight(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        status: AuditPreflightJobStatus,
        safe_error_code: str | None,
        stop_receipt: object,
    ) -> AuditPreflightCallbackAck:
        self.stop_calls += 1
        self.stop_statuses.append(status)
        self.stop_safe_error_codes.append(safe_error_code)
        self.backend.call_order.append("stop_callback")
        record = await self._record(dispatch)
        assert record.stop_evidence is not None
        assert record.stop_ack is None
        assert self.backend.cleanup_calls == 0
        assert lease == record.lease_envelope
        assert stop_receipt is not None
        if self.stop_calls == 1 and self.stop_error is not None:
            status_code, code = self.stop_error
            raise RunnerControlClientError(status_code, code, "stop fenced")
        if self.stop_calls <= self.stop_failures:
            raise RunnerControlClientError(503, "control_unavailable", "retry")
        return AuditPreflightCallbackAck(
            job_id=dispatch.owner.job_id,
            status=status,
            state_version=state_version + 1,
            finished_at=utc_now(),
        )

    async def enqueue(self, _: object) -> None:
        self.ordinary_enqueue_calls += 1
        raise AssertionError("Audit Preflight must not use ordinary Runner enqueue")

    async def _record(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
    ) -> AuditPreflightRunnerJournalRecord:
        record = await self.journal.get(dispatch.owner.job_id)
        assert record is not None
        return record


async def _wait_for_record(
    journal: AuditPreflightRunnerJournal,
    job_id: str,
    predicate: Callable[[AuditPreflightRunnerJournalRecord], bool],
    *,
    deadline_seconds: float = 3,
) -> AuditPreflightRunnerJournalRecord:
    async with asyncio.timeout(deadline_seconds):
        while True:
            record = await journal.get(job_id)
            if record is not None and predicate(record):
                return record
            await asyncio.sleep(0.01)


async def _join_runner_jobs(
    runner: AuditPreflightRunner,
    *,
    deadline_seconds: float = 3,
) -> None:
    tasks = tuple(
        runner._job_tasks.values()  # noqa: SLF001 - white-box crash/retry assertion
    )
    if tasks:
        async with asyncio.timeout(deadline_seconds):
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status", "has_result", "budget_status"),
    [
        (
            SourceIngestWorkerOutcome.SUCCEEDED,
            AuditPreflightJobStatus.SUCCEEDED,
            True,
            AuditPreflightBudgetStatus.UNAVAILABLE,
        ),
        (
            SourceIngestWorkerOutcome.REJECTED,
            AuditPreflightJobStatus.REJECTED,
            True,
            AuditPreflightBudgetStatus.BLOCKING,
        ),
        (
            SourceIngestWorkerOutcome.FAILED,
            AuditPreflightJobStatus.FAILED,
            False,
            None,
        ),
    ],
)
async def test_execution_projects_terminal_result_and_cleans_only_after_ack(
    tmp_path: Path,
    outcome: SourceIngestWorkerOutcome,
    expected_status: AuditPreflightJobStatus,
    has_result: bool,
    budget_status: AuditPreflightBudgetStatus | None,
) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    backend = _FakeExecutionBackend(
        dispatch=dispatch,
        journal=journal,
        outcome=outcome,
    )
    client = _FakeControlClient(journal=journal, backend=backend)
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    record = await _wait_for_record(journal, "job-1", lambda item: item.cleaned_at is not None)
    await runner.close()

    assert record.finish_ack is not None
    assert record.finish_ack.status is expected_status
    assert record.terminal_observation is not None
    assert (record.terminal_observation.result is not None) is has_result
    if budget_status is not None:
        assert record.terminal_observation.result is not None
        assert record.terminal_observation.result.minimum_feasible_budget.status is budget_status
        entries = record.terminal_observation.result.capability_matrix.entries
        assert tuple(entry.capability_id for entry in entries) == (
            "detector_inventory",
            "git_metadata",
            "source_ingest",
        )
        assert entries[2].proof_digest != backend.capsule.prepare_proof_digest
    assert backend.call_order == [
        "prepare",
        "physical_start",
        "start_ack",
        "wait",
        "finish_callback",
        "cleanup",
    ]
    assert backend.cleanup_calls == 1
    assert backend.ordinary_enqueue_calls == 0
    assert client.ordinary_enqueue_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("renew_mode", "expected_status", "expected_error"),
    [
        ("cancel", AuditPreflightJobStatus.CANCELLED, None),
        (
            "lease_lost",
            AuditPreflightJobStatus.FAILED,
            "audit_preflight_lease_lost",
        ),
    ],
)
async def test_cancel_or_lease_loss_stops_then_acknowledges_and_cleans(
    tmp_path: Path,
    renew_mode: str,
    expected_status: AuditPreflightJobStatus,
    expected_error: str | None,
) -> None:
    dispatch = _dispatch(lease_seconds=0.2)
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    backend = _FakeExecutionBackend(
        dispatch=dispatch,
        journal=journal,
        block_wait=True,
    )
    client = _FakeControlClient(
        journal=journal,
        backend=backend,
        renew_mode=renew_mode,
    )
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    record = await _wait_for_record(journal, "job-1", lambda item: item.cleaned_at is not None)
    await runner.close()

    assert record.stop_ack is not None
    assert record.stop_ack.status is expected_status
    assert client.stop_statuses == [expected_status]
    assert client.stop_safe_error_codes == [expected_error]
    assert backend.stop_calls == 1
    assert backend.cleanup_calls == 1
    assert backend.call_order.index("physical_stop") < backend.call_order.index("stop_callback")
    assert backend.call_order.index("stop_callback") < backend.call_order.index("cleanup")


@pytest.mark.asyncio
async def test_transient_finish_failure_retains_proof_and_replays_callback_only(
    tmp_path: Path,
) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    backend = _FakeExecutionBackend(dispatch=dispatch, journal=journal)
    failing_client = _FakeControlClient(
        journal=journal,
        backend=backend,
        finish_failures=1,
    )
    first_runner = AuditPreflightRunner(
        client=failing_client,
        journal=journal,
        backend=backend,
    )

    await first_runner.submit(dispatch)
    await _join_runner_jobs(first_runner)
    retained = await _wait_for_record(
        journal,
        "job-1",
        lambda item: item.terminal_observation is not None,
    )
    assert retained.finish_ack is None
    assert retained.cleanup_intent_at is None
    assert backend.cleanup_calls == 0
    await first_runner.close()

    retry_client = _FakeControlClient(journal=journal, backend=backend)
    restarted = AuditPreflightRunner(
        client=retry_client,
        journal=journal,
        backend=backend,
    )
    await restarted.submit(dispatch)
    recovered = await _wait_for_record(
        journal,
        "job-1",
        lambda item: item.cleaned_at is not None,
    )
    await restarted.close()

    assert recovered.finish_ack is not None
    assert backend.prepare_calls == 1
    assert backend.start_calls == 1
    assert backend.wait_calls == 1
    assert backend.cleanup_calls == 1
    assert failing_client.finish_calls == 1
    assert retry_client.finish_calls == 1


@pytest.mark.asyncio
async def test_finish_cancel_race_switches_to_cancelled_stop_path(tmp_path: Path) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    backend = _FakeExecutionBackend(dispatch=dispatch, journal=journal)
    client = _FakeControlClient(
        journal=journal,
        backend=backend,
        finish_error=(409, "audit_preflight_cancel_requested"),
    )
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    record = await _wait_for_record(journal, "job-1", lambda item: item.cleaned_at is not None)
    await runner.close()

    assert record.terminal_observation is not None
    assert record.finish_ack is None
    assert record.stop_reason_code == "audit_preflight_cancel_requested"
    assert record.stop_ack is not None
    assert record.stop_ack.status is AuditPreflightJobStatus.CANCELLED
    assert client.finish_calls == 1
    assert client.stop_statuses == [AuditPreflightJobStatus.CANCELLED]
    assert client.stop_safe_error_codes == [None]
    assert backend.stop_calls == 1
    assert backend.cleanup_calls == 1


@pytest.mark.asyncio
async def test_result_expiry_persists_failed_terminal_instead_of_retry_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    backend = _FakeExecutionBackend(
        dispatch=dispatch,
        journal=journal,
        block_wait=True,
    )
    client = _FakeControlClient(journal=journal, backend=backend)
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    await asyncio.wait_for(backend.wait_started.wait(), timeout=2)
    monkeypatch.setattr(
        audit_preflight_module,
        "utc_now",
        lambda: dispatch.owner.expires_at,
    )
    backend.wait_gate.set()
    record = await _wait_for_record(journal, "job-1", lambda item: item.cleaned_at is not None)
    await runner.close()

    assert record.terminal_observation is not None
    assert record.terminal_observation.status is AuditPreflightJobStatus.FAILED
    assert record.terminal_observation.result is None
    assert record.terminal_observation.safe_error_code == "audit_preflight_result_expired"
    assert record.finish_ack is not None
    assert record.finish_ack.status is AuditPreflightJobStatus.FAILED
    assert backend.wait_calls == 1
    assert backend.stop_calls == 0
    assert backend.cleanup_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["prepare_proof", "process_identity"])
async def test_execution_identity_drift_stops_without_publishing_result(
    tmp_path: Path,
    drift: str,
) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    backend = _FakeExecutionBackend(dispatch=dispatch, journal=journal)
    if drift == "prepare_proof":
        backend.execution_prepare_proof_digest = _digest("drifted-prepare")
    else:
        backend.execution_process_identity_digest = _digest("drifted-process")
    client = _FakeControlClient(journal=journal, backend=backend)
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    record = await _wait_for_record(journal, "job-1", lambda item: item.cleaned_at is not None)
    await runner.close()

    assert record.terminal_observation is None
    assert record.finish_ack is None
    assert record.stop_reason_code == "audit_source_ingest_execution_binding_mismatch"
    assert record.stop_ack is not None
    assert record.stop_ack.status is AuditPreflightJobStatus.FAILED
    assert client.finish_calls == 0
    assert client.stop_safe_error_codes == ["audit_source_ingest_execution_binding_mismatch"]
    assert backend.stop_calls == 1
    assert backend.cleanup_calls == 1


@pytest.mark.asyncio
async def test_operator_cancel_fence_upgrades_local_failure_but_retains_observation(
    tmp_path: Path,
) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    backend = _FakeExecutionBackend(dispatch=dispatch, journal=journal)
    backend.execution_prepare_proof_digest = _digest("drifted-prepare")
    client = _FakeControlClient(
        journal=journal,
        backend=backend,
        stop_error=(409, "audit_preflight_cancel_requested"),
    )
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    record = await _wait_for_record(journal, "job-1", lambda item: item.cleaned_at is not None)
    await runner.close()

    assert record.stop_reason_code == "audit_preflight_cancel_requested"
    assert record.stop_failure_code == "audit_source_ingest_execution_binding_mismatch"
    assert record.stop_ack is not None
    assert record.stop_ack.status is AuditPreflightJobStatus.CANCELLED
    assert client.stop_statuses == [
        AuditPreflightJobStatus.FAILED,
        AuditPreflightJobStatus.CANCELLED,
    ]
    assert client.stop_safe_error_codes == [
        "audit_source_ingest_execution_binding_mismatch",
        None,
    ]
    assert backend.stop_calls == 1
    assert backend.cleanup_calls == 1


@pytest.mark.asyncio
async def test_cancel_before_prepare_proves_never_created_without_backend_io(
    tmp_path: Path,
) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch)
    await journal.begin_stop(
        "job-1",
        digest,
        reason_code="audit_preflight_cancel_requested",
    )
    backend = _FakeExecutionBackend(dispatch=dispatch, journal=journal)
    client = _FakeControlClient(journal=journal, backend=backend)
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    record = await _wait_for_record(journal, "job-1", lambda item: item.cleaned_at is not None)
    await runner.close()

    assert record.prepare_intent_at is None
    assert record.capsule is None
    assert record.stop_evidence is not None
    assert record.stop_evidence.disposition is AuditPreflightStopDisposition.NEVER_CREATED
    assert record.stop_evidence.capsule_id is None
    assert record.stop_evidence.process_identity_digest is None
    assert record.stop_evidence.never_created_proof_digest is not None
    assert (
        record.stop_evidence.observed_terminal_state
        is AuditPreflightObservedTerminalState.NOT_CREATED
    )
    assert record.stop_ack is not None
    assert record.stop_ack.status is AuditPreflightJobStatus.CANCELLED
    assert backend.prepare_calls == 0
    assert backend.start_calls == 0
    assert backend.wait_calls == 0
    assert backend.stop_calls == 0
    assert backend.cleanup_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain_action", ["prepare", "prepared", "start"])
async def test_restart_of_unstarted_capsule_stops_without_second_start(
    tmp_path: Path,
    uncertain_action: str,
) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch)
    await journal.begin_prepare("job-1", digest)
    backend = _FakeExecutionBackend(dispatch=dispatch, journal=journal)
    if uncertain_action != "prepare":
        await journal.record_prepared("job-1", digest, backend.capsule)
    if uncertain_action == "start":
        await journal.begin_start("job-1", digest)
    if uncertain_action in {"prepare", "start"}:
        backend.recovery = AuditPreflightCapsuleRecovery(
            capsule=backend.capsule,
            requires_stop=True,
        )
    backend.can_start_value = False
    client = _FakeControlClient(journal=journal, backend=backend)
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    record = await _wait_for_record(journal, "job-1", lambda item: item.cleaned_at is not None)
    await runner.close()

    assert record.stop_ack is not None
    assert backend.start_calls == 0
    assert backend.stop_calls == 1
    assert backend.cleanup_calls == 1


@pytest.mark.asyncio
async def test_prepare_intent_without_backend_record_stays_fail_closed_for_probe(
    tmp_path: Path,
) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch)
    await journal.begin_prepare("job-1", digest)
    backend = _FakeExecutionBackend(dispatch=dispatch, journal=journal)
    client = _FakeControlClient(journal=journal, backend=backend)
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    await _join_runner_jobs(runner)
    record = await journal.get("job-1")
    await runner.close()

    assert record is not None
    assert record.recovery_action is AuditPreflightRecoveryAction.PROBE_PREPARE
    assert record.capsule is None
    assert record.stop_evidence is None
    assert backend.prepare_calls == 0
    assert backend.start_calls == 0
    assert backend.stop_calls == 0
    assert backend.cleanup_calls == 0
    assert client.start_calls == 0
    assert client.finish_calls == 0
    assert client.stop_calls == 0


def test_same_process_ambiguous_start_is_not_considered_startable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch()
    container_id = "c" * 64
    prepare_proof = _digest("prepare-proof")

    class RecoveringSourceBackend:
        def __init__(self, *, audit: AuditConfig, state_root: Path) -> None:
            assert state_root == tmp_path
            self.policy_digest = dispatch.owner.policy_digest
            self.record = SourceIngestCapsuleRecord(
                schema_version="riftx.audit-source-ingest-capsule-record/v1",
                capsule_id=dispatch.capsule_id,
                container_name="riftx-preflight-fixture",
                container_id=container_id,
                request_digest=dispatch.request.request_digest,
                source_root_identity_digest=dispatch.owner.source_root_identity_digest,
                repository_descriptor_identity_digest=_digest("repository-descriptor"),
                source_mount_identity_digest=_digest("source-mount"),
                backend_id=dispatch.owner.backend_id,
                image_digest=dispatch.owner.image_digest,
                policy_digest=dispatch.owner.policy_digest,
                capsule_user_id=1000,
                lifecycle_state="prepared",
                prepare_proof_digest=prepare_proof,
                observed_state="created",
            )

        def get_capsule_record(self, _capsule_id: str) -> SourceIngestCapsuleRecord:
            return self.record

    monkeypatch.setattr(
        audit_preflight_module,
        "DockerSourceIngestBackend",
        RecoveringSourceBackend,
    )
    adapter = DockerAuditPreflightCapsuleBackend(
        audit=AuditConfig(
            enabled=True,
            source_ingest=AuditSourceIngestConfig(
                image_digest=dispatch.owner.image_digest,
            ),
        ),
        state_root=tmp_path,
    )
    prepared = object.__new__(PreparedSourceIngestCapsule)
    prepared.container_id = container_id
    prepared.prepare_proof_digest = prepare_proof
    adapter._prepared[dispatch.capsule_id] = prepared

    assert adapter.can_start(dispatch.capsule_id) is True

    adapter.backend.record = replace(
        adapter.backend.record,
        lifecycle_state="start_requested",
    )

    assert adapter.can_start(dispatch.capsule_id) is False


@pytest.mark.asyncio
async def test_timeout_created_capsule_recovers_locator_only_and_requires_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch()
    process_identity = _digest("recovered-created-process")

    class RecoveringSourceBackend:
        def __init__(self, *, audit: AuditConfig, state_root: Path) -> None:
            assert state_root == tmp_path
            self.policy_digest = dispatch.owner.policy_digest
            self.recover_calls = 0
            self.start_calls = 0
            self.stop_calls = 0
            self.record = SourceIngestCapsuleRecord(
                schema_version="riftx.audit-source-ingest-capsule-record/v1",
                capsule_id=dispatch.capsule_id,
                container_name="riftx-audit-preflight-capsule-1",
                container_id=None,
                request_digest=dispatch.request.request_digest,
                source_root_identity_digest=(dispatch.owner.source_root_identity_digest),
                repository_descriptor_identity_digest=_digest("descriptor"),
                source_mount_identity_digest=_digest("source-mount"),
                backend_id=dispatch.owner.backend_id,
                image_digest=dispatch.owner.image_digest,
                policy_digest=dispatch.owner.policy_digest,
                capsule_user_id=1000,
                lifecycle_state="create_intent",
            )
            assert audit.source_ingest.image_digest == self.record.image_digest

        def get_capsule_record(self, capsule_id: str) -> SourceIngestCapsuleRecord:
            assert capsule_id == dispatch.capsule_id
            return self.record

        async def recover_create_intent(self, capsule_id: str) -> object:
            assert capsule_id == dispatch.capsule_id
            self.recover_calls += 1
            self.record = replace(
                self.record,
                container_id="c" * 64,
                lifecycle_state="created",
                process_identity_digest=process_identity,
                observed_state="recovered_create_created",
            )
            return object()

        async def start_capsule(self, _capsule_id: str) -> object:
            self.start_calls += 1
            raise AssertionError("recovered create intent must never be resumed")

        async def stop_capsule(self, capsule_id: str) -> SourceIngestStopEvidence:
            assert capsule_id == dispatch.capsule_id
            assert self.record.container_id == "c" * 64
            self.stop_calls += 1
            return SourceIngestStopEvidence(
                stopped=True,
                process_identity_digest=process_identity,
                observed_state="created_not_started",
            )

    monkeypatch.setattr(
        audit_preflight_module,
        "DockerSourceIngestBackend",
        RecoveringSourceBackend,
    )
    audit = AuditConfig(
        enabled=True,
        source_ingest=AuditSourceIngestConfig(
            image_digest=dispatch.owner.image_digest,
        ),
    )
    adapter = DockerAuditPreflightCapsuleBackend(audit=audit, state_root=tmp_path)
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch)
    await journal.begin_prepare(dispatch.owner.job_id, digest)
    record = await journal.get(dispatch.owner.job_id)
    assert record is not None

    recovery = await adapter.recover(record)

    assert recovery.requires_stop is True
    assert recovery.capsule is None
    assert recovery.start_evidence is None
    assert recovery.stop_evidence is None
    assert adapter.can_start(dispatch.capsule_id) is False
    assert adapter.backend.recover_calls == 1
    assert adapter.backend.start_calls == 0
    recovered_record = adapter.backend.get_capsule_record(dispatch.capsule_id)
    assert recovered_record.container_id == "c" * 64
    assert recovered_record.lifecycle_state == "created"

    controller = DurableAuditPreflightCapsuleController(
        journal=journal,
        backend=adapter,
    )
    stopped = await controller.stop(
        dispatch.owner.job_id,
        digest,
        reason_code="audit_preflight_recovery_stop_required",
    )

    assert stopped.stop_evidence is not None
    assert stopped.stop_evidence.disposition is AuditPreflightStopDisposition.STOPPED
    assert stopped.stop_evidence.never_created_proof_digest is None
    assert adapter.backend.stop_calls == 1
    assert adapter.backend.start_calls == 0


@pytest.mark.asyncio
async def test_restart_recovers_physical_start_then_waits_without_second_start(
    tmp_path: Path,
) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    digest = audit_preflight_dispatch_digest(dispatch)
    await journal.admit(dispatch)
    backend = _FakeExecutionBackend(dispatch=dispatch, journal=journal)
    await journal.begin_prepare("job-1", digest)
    await journal.record_prepared("job-1", digest, backend.capsule)
    await journal.begin_start("job-1", digest)
    backend.can_start_value = False
    backend.recovery = AuditPreflightCapsuleRecovery(
        capsule=backend.capsule,
        start_evidence=backend.start_evidence,
        terminal_available=True,
    )
    client = _FakeControlClient(journal=journal, backend=backend)
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    await runner.submit(dispatch)
    record = await _wait_for_record(journal, "job-1", lambda item: item.cleaned_at is not None)
    await runner.close()

    assert record.finish_ack is not None
    assert backend.start_calls == 0
    assert backend.wait_calls == 1
    assert backend.cleanup_calls == 1


@pytest.mark.asyncio
async def test_orphan_reconciliation_physically_stops_without_cleanup(tmp_path: Path) -> None:
    dispatch = _dispatch()
    journal = AuditPreflightRunnerJournal(tmp_path / "preflight.json")
    await journal.admit(dispatch)
    backend = _FakeExecutionBackend(dispatch=dispatch, journal=journal)
    backend.orphan_capsules = ("orphan-capsule",)
    client = _FakeControlClient(journal=journal, backend=backend)
    runner = AuditPreflightRunner(client=client, journal=journal, backend=backend)

    stopped = await runner.reconcile_orphans()

    assert stopped == ("orphan-capsule",)
    assert backend.last_known_capsules == {dispatch.capsule_id}
    assert backend.orphan_stop_calls == 1
    assert backend.cleanup_calls == 0
    assert backend.ordinary_enqueue_calls == 0
    assert client.ordinary_enqueue_calls == 0
