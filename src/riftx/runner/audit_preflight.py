"""Dedicated Audit Preflight execution loop for the standalone Runner.

The implementation intentionally owns no Run, Workflow, Event, Snapshot, CAS,
Artifact, or ordinary Runner command identity.  Every capsule effect is fenced by
the independent journal in :mod:`riftx.runner.preflight`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from riftx.audit.paths import (
    DEFAULT_SOURCE_PATH_POLICY_VERSION,
    SourcePathAuthorizationError,
    open_authorized_source_repository,
)
from riftx.audit.source_ingest import (
    SOURCE_INGEST_BACKEND_COMPONENT_VERSION,
    DockerSourceIngestBackend,
    PreparedSourceIngestCapsule,
    SourceIngestBackendAvailability,
    SourceIngestBackendError,
    SourceIngestCapsuleRecord,
    SourceIngestExecutionResult,
)
from riftx.audit.source_ingest_contract import (
    SourceIngestWorkerOutcome,
    SourceIngestWorkerRequest,
)
from riftx.config import AuditConfig
from riftx.domain.audit_preflight import (
    AuditPreflightBudgetStatus,
    AuditPreflightCapabilityFact,
    AuditPreflightCapabilityMatrix,
    AuditPreflightCapabilityStatus,
    AuditPreflightExitReceipt,
    AuditPreflightExitTerminalState,
    AuditPreflightJobStatus,
    AuditPreflightLanguageEstimate,
    AuditPreflightLeaseEnvelope,
    AuditPreflightMinimumFeasibleBudget,
    AuditPreflightObservedTerminalState,
    AuditPreflightResult,
    AuditPreflightStopDisposition,
    AuditPreflightStopReceipt,
)
from riftx.domain.audit_preflight_wire import (
    AuditPreflightCallbackAck,
    AuditPreflightDispatchEnvelope,
    AuditPreflightLeaseGrant,
    AuditPreflightStartGrant,
)
from riftx.domain.base import utc_now

from .control_client import RunnerControlClientError
from .preflight import (
    AuditPreflightCapsuleReference,
    AuditPreflightCapsuleStartEvidence,
    AuditPreflightCapsuleStopEvidence,
    AuditPreflightRecoveryAction,
    AuditPreflightRunnerJournal,
    AuditPreflightRunnerJournalConflict,
    AuditPreflightRunnerJournalRecord,
    AuditPreflightTerminalObservation,
    DurableAuditPreflightCapsuleController,
)

logger = logging.getLogger(__name__)
SOURCE_INGEST_EXECUTION_PROOF_VERSION = "riftx.audit-source-ingest-execution-proof/v1"


class AuditPreflightControlClient(Protocol):
    async def poll_audit_preflight(
        self,
        *,
        wait_seconds: float = 0,
    ) -> AuditPreflightDispatchEnvelope | None: ...

    async def renew_audit_preflight(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
    ) -> AuditPreflightLeaseGrant: ...

    async def start_audit_preflight(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        capsule_prepare_proof_digest: str,
    ) -> AuditPreflightStartGrant: ...

    async def finish_audit_preflight(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        status: AuditPreflightJobStatus,
        result: AuditPreflightResult | None,
        safe_error_code: str | None,
        exit_receipt: AuditPreflightExitReceipt,
    ) -> AuditPreflightCallbackAck: ...

    async def stop_audit_preflight(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        *,
        lease: AuditPreflightLeaseEnvelope,
        state_version: int,
        status: AuditPreflightJobStatus,
        safe_error_code: str | None,
        stop_receipt: AuditPreflightStopReceipt,
    ) -> AuditPreflightCallbackAck: ...


class AuditPreflightCapsuleOutcomeUnknown(RuntimeError):
    """The backend cannot yet prove terminal or never-created state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AuditPreflightCapsuleRecovery:
    capsule: AuditPreflightCapsuleReference | None = None
    start_evidence: AuditPreflightCapsuleStartEvidence | None = None
    stop_evidence: AuditPreflightCapsuleStopEvidence | None = None
    terminal_available: bool = False
    requires_stop: bool = False
    cleanup_complete: bool = False


class AuditPreflightExecutionBackend(Protocol):
    async def prepare(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
    ) -> AuditPreflightCapsuleReference: ...

    async def start(
        self,
        capsule: AuditPreflightCapsuleReference,
    ) -> AuditPreflightCapsuleStartEvidence: ...

    async def wait(
        self,
        capsule: AuditPreflightCapsuleReference,
    ) -> SourceIngestExecutionResult: ...

    async def stop(
        self,
        *,
        capsule_id: str,
        capsule: AuditPreflightCapsuleReference | None,
    ) -> AuditPreflightCapsuleStopEvidence: ...

    async def cleanup(
        self,
        *,
        capsule_id: str,
        capsule: AuditPreflightCapsuleReference | None,
    ) -> None: ...

    def can_start(self, capsule_id: str) -> bool: ...

    async def recover(
        self,
        record: AuditPreflightRunnerJournalRecord,
    ) -> AuditPreflightCapsuleRecovery: ...

    async def reconcile_orphans(self, known_capsule_ids: set[str]) -> tuple[str, ...]: ...


class DockerAuditPreflightCapsuleBackend:
    """Adapter from the generic Runner journal to the production SourceIngest backend."""

    def __init__(self, *, audit: AuditConfig, state_root: Path) -> None:
        self.audit = audit
        self.backend = DockerSourceIngestBackend(audit=audit, state_root=state_root)
        self._prepared: dict[str, PreparedSourceIngestCapsule] = {}

    async def probe_availability(self) -> SourceIngestBackendAvailability:
        return await self.backend.probe_availability()

    async def reconcile_mount_probe(self) -> str | None:
        return await self.backend.reconcile_mount_probe()

    async def prepare(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
    ) -> AuditPreflightCapsuleReference:
        self._require_dispatch_policy(dispatch)
        request = dispatch.request
        try:
            source = open_authorized_source_repository(
                request.repository_path,
                allowed_roots=self.audit.source_roots,
                policy_version=DEFAULT_SOURCE_PATH_POLICY_VERSION,
            )
        except SourcePathAuthorizationError as exc:
            raise SourceIngestBackendError(exc.failure.value) from exc
        if source.source_root_identity_digest != dispatch.owner.source_root_identity_digest:
            source.close()
            raise SourceIngestBackendError("audit_source_root_identity_changed")
        try:
            source_mount = self.backend.observe_source_mount(source)
        except SourceIngestBackendError:
            source.close()
            raise
        worker_request = SourceIngestWorkerRequest(
            capsule_id=dispatch.capsule_id,
            request_digest=request.request_digest,
            source_root_identity_digest=source.source_root_identity_digest,
            repository_descriptor_identity_digest=(source.repository_descriptor_identity_digest),
            expected_source_mount_identity_digest=source_mount.identity_digest,
            target_kind=request.target.kind,
            revision=request.target.revision,
            base_revision=request.target.base_revision,
            mode=request.mode,
            include_untracked=request.target.include_untracked,
            include_paths=request.include_paths,
            exclude_paths=request.exclude_paths,
            max_files=self.audit.max_files,
            max_repository_bytes=self.audit.max_repository_bytes,
            max_file_bytes=self.audit.max_file_bytes,
            max_git_output_bytes=min(
                self.audit.source_ingest.max_output_bytes,
                16 * 1024 * 1024,
            ),
            command_timeout_seconds=min(
                self.audit.source_ingest.max_wall_seconds,
                300,
            ),
        )
        prepared = await self.backend.prepare(
            source=source,
            capsule_id=dispatch.capsule_id,
            request=worker_request,
        )
        self._prepared[dispatch.capsule_id] = prepared
        return AuditPreflightCapsuleReference(
            capsule_id=dispatch.capsule_id,
            locator=prepared.container_id,
            prepare_proof_digest=prepared.prepare_proof_digest,
        )

    async def start(
        self,
        capsule: AuditPreflightCapsuleReference,
    ) -> AuditPreflightCapsuleStartEvidence:
        prepared = self._prepared.get(capsule.capsule_id)
        if prepared is None or prepared.container_id != capsule.locator:
            # A prepared container restored after Runner restart no longer has
            # descriptor ownership in this process. It must be stopped, never
            # restarted from pathname/container metadata alone.
            raise AuditPreflightCapsuleOutcomeUnknown(
                "audit_source_ingest_prepared_restart_requires_stop"
            )
        evidence = await prepared.start()
        return AuditPreflightCapsuleStartEvidence(
            capsule_id=capsule.capsule_id,
            process_identity_digest=evidence.process_identity_digest,
            observed_state=evidence.observed_state,
            observed_at=utc_now(),
        )

    async def wait(
        self,
        capsule: AuditPreflightCapsuleReference,
    ) -> SourceIngestExecutionResult:
        prepared = self._prepared.get(capsule.capsule_id)
        if prepared is not None:
            return await prepared.wait()
        return await self.backend.wait_capsule(capsule.capsule_id)

    async def stop(
        self,
        *,
        capsule_id: str,
        capsule: AuditPreflightCapsuleReference | None,
    ) -> AuditPreflightCapsuleStopEvidence:
        prepared = self._prepared.get(capsule_id)
        evidence = (
            await prepared.stop()
            if prepared is not None
            else await self.backend.stop_capsule(capsule_id)
        )
        if not evidence.stopped or evidence.process_identity_digest is None:
            raise AuditPreflightCapsuleOutcomeUnknown("audit_source_ingest_stop_outcome_unknown")
        return AuditPreflightCapsuleStopEvidence(
            disposition=AuditPreflightStopDisposition.STOPPED,
            capsule_id=capsule_id,
            process_identity_digest=evidence.process_identity_digest,
            observed_terminal_state=_observed_terminal_state(evidence.observed_state),
            observed_at=utc_now(),
        )

    async def cleanup(
        self,
        *,
        capsule_id: str,
        capsule: AuditPreflightCapsuleReference | None,
    ) -> None:
        prepared = self._prepared.pop(capsule_id, None)
        if prepared is not None:
            await prepared.cleanup(terminal_proof_persisted=True)
            return
        await self.backend.cleanup_capsule(
            capsule_id,
            terminal_proof_persisted=True,
        )

    def can_start(self, capsule_id: str) -> bool:
        prepared = self._prepared.get(capsule_id)
        if prepared is None:
            return False
        record = self.backend.get_capsule_record(capsule_id)
        return (
            record is not None
            and record.lifecycle_state == "prepared"
            and record.container_id == prepared.container_id
            and record.prepare_proof_digest == prepared.prepare_proof_digest
        )

    async def recover(
        self,
        record: AuditPreflightRunnerJournalRecord,
    ) -> AuditPreflightCapsuleRecovery:
        backend_record = self.backend.get_capsule_record(record.capsule_id)
        if backend_record is None:
            return AuditPreflightCapsuleRecovery()
        self._require_recovery_binding(record, backend_record)
        if (
            backend_record.lifecycle_state in {"create_intent", "outcome_unknown"}
            and backend_record.container_id is None
        ):
            await self.backend.recover_create_intent(record.capsule_id)
            recovered_record = self.backend.get_capsule_record(record.capsule_id)
            if recovered_record is None:
                raise AuditPreflightRunnerJournalConflict(record.job_id)
            self._require_recovery_binding(record, recovered_record)
            backend_record = recovered_record
        capsule = _capsule_reference(backend_record)
        start_evidence = _recovered_start_evidence(backend_record)
        stop_evidence = _recovered_stop_evidence(backend_record)
        state = backend_record.lifecycle_state
        return AuditPreflightCapsuleRecovery(
            capsule=capsule,
            start_evidence=start_evidence,
            stop_evidence=stop_evidence,
            terminal_available=state == "terminal",
            requires_stop=state
            in {
                "create_intent",
                "created",
                "prepared",
                "start_requested",
                "running",
                "outcome_unknown",
            },
            cleanup_complete=state == "cleanup_complete",
        )

    async def reconcile_orphans(self, known_capsule_ids: set[str]) -> tuple[str, ...]:
        stopped: list[str] = []
        for record in self.backend.list_capsule_records():
            if record.capsule_id in known_capsule_ids or record.lifecycle_state in {
                "cleanup_complete",
                "stop_observed",
            }:
                continue
            try:
                evidence = await self.backend.stop_capsule(record.capsule_id)
            except SourceIngestBackendError:
                continue
            if evidence.stopped:
                stopped.append(record.capsule_id)
        return tuple(stopped)

    def _require_dispatch_policy(self, dispatch: AuditPreflightDispatchEnvelope) -> None:
        image_digest = self.audit.source_ingest.image_digest
        if (
            dispatch.owner.backend_id != self.audit.source_ingest.backend_id
            or image_digest is None
            or dispatch.owner.image_digest != image_digest
            or dispatch.owner.policy_digest != self.backend.policy_digest
            or dispatch.owner.source_node_id != "local"
        ):
            raise SourceIngestBackendError("audit_source_ingest_policy_binding_mismatch")

    def _require_recovery_binding(
        self,
        record: AuditPreflightRunnerJournalRecord,
        backend_record: SourceIngestCapsuleRecord,
    ) -> None:
        dispatch = record.dispatch
        if (
            backend_record.capsule_id != record.capsule_id
            or backend_record.request_digest != dispatch.request.request_digest
            or backend_record.source_root_identity_digest
            != dispatch.owner.source_root_identity_digest
            or backend_record.backend_id != dispatch.owner.backend_id
            or backend_record.image_digest != dispatch.owner.image_digest
            or backend_record.policy_digest != dispatch.owner.policy_digest
            or (
                record.capsule is not None
                and (
                    backend_record.container_id != record.capsule.locator
                    or backend_record.prepare_proof_digest != record.capsule.prepare_proof_digest
                )
            )
        ):
            raise AuditPreflightRunnerJournalConflict(record.job_id)


@dataclass(slots=True)
class _CancellationState:
    event: asyncio.Event
    reason_code: str | None = None

    def request(self, reason_code: str) -> None:
        if self.reason_code is None:
            self.reason_code = reason_code
        self.event.set()


class AuditPreflightRunner:
    """Poll, execute, renew, recover, and clean dedicated Preflight Jobs."""

    def __init__(
        self,
        *,
        client: AuditPreflightControlClient,
        journal: AuditPreflightRunnerJournal,
        backend: AuditPreflightExecutionBackend,
        poll_wait_seconds: float = 10.0,
        reconnect_seconds: float = 0.5,
        max_concurrent_jobs: int = 1,
    ) -> None:
        if not 0 <= poll_wait_seconds <= 30:
            raise ValueError("Audit Preflight poll wait must be between zero and 30 seconds")
        if reconnect_seconds <= 0:
            raise ValueError("Audit Preflight reconnect delay must be positive")
        if max_concurrent_jobs < 1:
            raise ValueError("Audit Preflight concurrency must be positive")
        self._client = client
        self._journal = journal
        self._backend = backend
        self._controller = DurableAuditPreflightCapsuleController(
            journal=journal,
            backend=backend,
        )
        self._poll_wait_seconds = poll_wait_seconds
        self._reconnect_seconds = reconnect_seconds
        self._max_concurrent_jobs = max_concurrent_jobs
        self._poll_task: asyncio.Task[None] | None = None
        self._job_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancellations: dict[str, _CancellationState] = {}
        self._callback_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Audit Preflight Runner is closed")
        if self._poll_task is not None and not self._poll_task.done():
            return
        await self._schedule_recoverable()
        await self.reconcile_orphans()
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="riftx-audit-preflight-poll",
        )

    async def close(self) -> None:
        self._closed = True
        poll_task = self._poll_task
        if poll_task is not None:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
        for cancellation in self._cancellations.values():
            cancellation.request("audit_preflight_runner_shutdown")
        tasks = list(self._job_tasks.values())
        await asyncio.gather(*tasks, return_exceptions=True)

    async def reconcile_orphans(self) -> tuple[str, ...]:
        known = {record.capsule_id for record in await self._journal.list_records()}
        stopped = await self._backend.reconcile_orphans(known)
        for capsule_id in stopped:
            logger.warning(
                "Stopped orphan Audit Preflight capsule %s; retained proof for reconciliation",
                capsule_id,
            )
        return stopped

    async def submit(self, dispatch: AuditPreflightDispatchEnvelope) -> None:
        record, _ = await self._journal.admit(dispatch)
        self._schedule(record)

    async def _poll_loop(self) -> None:
        while not self._closed:
            try:
                await self._schedule_recoverable()
                if len(self._job_tasks) >= self._max_concurrent_jobs:
                    await asyncio.sleep(0.05)
                    continue
                dispatch = await self._client.poll_audit_preflight(
                    wait_seconds=self._poll_wait_seconds,
                )
                if dispatch is not None:
                    await self.submit(dispatch)
                await self.reconcile_orphans()
            except asyncio.CancelledError:
                raise
            except RunnerControlClientError as exc:
                logger.warning("Audit Preflight control connection failed: %s", exc)
                await asyncio.sleep(self._reconnect_seconds)
            except Exception:
                logger.exception("Audit Preflight polling/recovery failed")
                await asyncio.sleep(self._reconnect_seconds)

    async def _schedule_recoverable(self) -> None:
        for record in await self._journal.list_recoverable():
            if len(self._job_tasks) >= self._max_concurrent_jobs:
                return
            self._schedule(record)

    def _schedule(self, record: AuditPreflightRunnerJournalRecord) -> None:
        existing = self._job_tasks.get(record.job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_job(record.job_id, record.dispatch_digest),
            name=f"riftx-audit-preflight-{record.job_id}",
        )
        self._job_tasks[record.job_id] = task

        def finished(completed: asyncio.Task[None], *, job_id: str = record.job_id) -> None:
            if self._job_tasks.get(job_id) is completed:
                self._job_tasks.pop(job_id, None)
            self._cancellations.pop(job_id, None)
            if not completed.cancelled():
                try:
                    completed.exception()
                except Exception:
                    pass

        task.add_done_callback(finished)

    async def _run_job(self, job_id: str, dispatch_digest: str) -> None:
        cancellation = _CancellationState(asyncio.Event())
        self._cancellations[job_id] = cancellation
        renewal = asyncio.create_task(
            self._renew_loop(job_id, dispatch_digest, cancellation),
            name=f"riftx-audit-preflight-renew-{job_id}",
        )
        try:
            await self._drive(job_id, dispatch_digest, cancellation)
        except asyncio.CancelledError:
            cancellation.request("audit_preflight_runner_shutdown")
            await self._best_effort_stop(job_id, dispatch_digest, cancellation)
            raise
        except Exception:
            logger.exception("Audit Preflight Job %s processing failed", job_id)
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

    async def _drive(
        self,
        job_id: str,
        dispatch_digest: str,
        cancellation: _CancellationState,
    ) -> None:
        for _ in range(64):
            record = await self._require_record(job_id)
            if (
                cancellation.event.is_set()
                and not record.callback_acknowledged
                and record.stop_intent_at is None
            ):
                await self._journal.begin_stop(
                    job_id,
                    dispatch_digest,
                    reason_code=(cancellation.reason_code or "audit_preflight_lease_lost"),
                )
                continue
            action = record.recovery_action
            if action is AuditPreflightRecoveryAction.NONE:
                return
            if action is AuditPreflightRecoveryAction.PREPARE:
                await self._controller.prepare(job_id, dispatch_digest)
                continue
            if action in {
                AuditPreflightRecoveryAction.PROBE_PREPARE,
                AuditPreflightRecoveryAction.PROBE_START,
            }:
                changed = await self._recover(record)
                if not changed:
                    return
                continue
            if action is AuditPreflightRecoveryAction.START:
                if not self._backend.can_start(record.capsule_id):
                    await self._journal.begin_stop(
                        job_id,
                        dispatch_digest,
                        reason_code="audit_source_ingest_prepared_restart_requires_stop",
                    )
                else:
                    await self._controller.start(job_id, dispatch_digest)
                continue
            if action is AuditPreflightRecoveryAction.REPORT_START:
                await self._report_start(record)
                continue
            if action is AuditPreflightRecoveryAction.WAIT_OR_PROBE:
                await self._wait_or_stop(record, cancellation)
                continue
            if action is AuditPreflightRecoveryAction.REPORT_FINISH:
                try:
                    await self._report_finish(record)
                except RunnerControlClientError as exc:
                    if exc.status_code in {409, 410}:
                        reason_code = (
                            "audit_preflight_cancel_requested"
                            if exc.code == "audit_preflight_cancel_requested"
                            else "audit_preflight_finish_fenced"
                        )
                        await self._journal.begin_stop(
                            job_id,
                            dispatch_digest,
                            reason_code=reason_code,
                        )
                        continue
                    raise
                continue
            if action is AuditPreflightRecoveryAction.STOP:
                try:
                    await self._controller.stop(
                        job_id,
                        dispatch_digest,
                        reason_code=(record.stop_reason_code or "audit_preflight_stop_required"),
                    )
                except AuditPreflightCapsuleOutcomeUnknown:
                    return
                continue
            if action is AuditPreflightRecoveryAction.REPORT_STOP:
                try:
                    await self._report_stop(record)
                except RunnerControlClientError as exc:
                    if (
                        exc.status_code in {409, 410}
                        and exc.code == "audit_preflight_cancel_requested"
                    ):
                        await self._journal.begin_stop(
                            job_id,
                            dispatch_digest,
                            reason_code="audit_preflight_cancel_requested",
                        )
                        continue
                    raise
                continue
            if action is AuditPreflightRecoveryAction.CLEANUP:
                await self._controller.cleanup(job_id, dispatch_digest)
                continue
        raise RuntimeError("Audit Preflight recovery exceeded its bounded transition count")

    async def _recover(self, record: AuditPreflightRunnerJournalRecord) -> bool:
        recovery = await self._backend.recover(record)
        changed = False
        if recovery.cleanup_complete and record.callback_acknowledged:
            await self._journal.begin_cleanup(record.job_id, record.dispatch_digest)
            await self._journal.record_cleaned(record.job_id, record.dispatch_digest)
            return True
        if recovery.capsule is not None and record.capsule is None:
            await self._journal.record_prepared(
                record.job_id,
                record.dispatch_digest,
                recovery.capsule,
            )
            changed = True
        current = await self._require_record(record.job_id)
        if recovery.start_evidence is not None and current.start_evidence is None:
            if current.start_intent_at is None:
                await self._journal.begin_stop(
                    record.job_id,
                    record.dispatch_digest,
                    reason_code="audit_preflight_orphan_start_detected",
                )
                return True
            await self._journal.record_started(
                record.job_id,
                record.dispatch_digest,
                recovery.start_evidence,
            )
            changed = True
        current = await self._require_record(record.job_id)
        if recovery.stop_evidence is not None and current.stop_evidence is None:
            if current.stop_intent_at is None:
                await self._journal.begin_stop(
                    record.job_id,
                    record.dispatch_digest,
                    reason_code="audit_preflight_recovered_stop",
                )
            await self._journal.record_stop_evidence(
                record.job_id,
                record.dispatch_digest,
                recovery.stop_evidence,
            )
            return True
        if recovery.requires_stop:
            current = await self._require_record(record.job_id)
            if current.stop_intent_at is None and not self._backend.can_start(current.capsule_id):
                await self._journal.begin_stop(
                    record.job_id,
                    record.dispatch_digest,
                    reason_code="audit_preflight_recovery_stop_required",
                )
                return True
        return changed or recovery.terminal_available

    async def _report_start(self, record: AuditPreflightRunnerJournalRecord) -> None:
        capsule = record.capsule
        if capsule is None:
            raise AuditPreflightRunnerJournalConflict(record.job_id)
        async with self._callback_lock(record.job_id):
            current = await self._require_record(record.job_id)
            grant = await self._client.start_audit_preflight(
                current.dispatch,
                lease=current.lease_envelope,
                state_version=current.state_version,
                capsule_prepare_proof_digest=capsule.prepare_proof_digest,
            )
            await self._journal.record_start_grant(
                current.job_id,
                current.dispatch_digest,
                grant,
            )

    async def _wait_or_stop(
        self,
        record: AuditPreflightRunnerJournalRecord,
        cancellation: _CancellationState,
    ) -> None:
        capsule = record.capsule
        if capsule is None:
            raise AuditPreflightRunnerJournalConflict(record.job_id)
        wait_task = asyncio.create_task(
            self._backend.wait(capsule),
            name=f"riftx-audit-preflight-wait-{record.job_id}",
        )
        cancel_task = asyncio.create_task(cancellation.event.wait())
        try:
            done, _ = await asyncio.wait(
                {wait_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancellation.event.is_set():
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
                await self._journal.begin_stop(
                    record.job_id,
                    record.dispatch_digest,
                    reason_code=(cancellation.reason_code or "audit_preflight_lease_lost"),
                )
                return
            execution = await wait_task
        except (SourceIngestBackendError, AuditPreflightCapsuleOutcomeUnknown) as exc:
            code = getattr(exc, "code", "audit_source_ingest_failed")
            await self._journal.begin_stop(
                record.job_id,
                record.dispatch_digest,
                reason_code=code,
            )
            return
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        current = await self._require_record(record.job_id)
        start_evidence = current.start_evidence
        current_capsule = current.capsule
        if (
            start_evidence is None
            or current_capsule is None
            or not hmac.compare_digest(
                execution.prepare_proof_digest,
                current_capsule.prepare_proof_digest,
            )
            or not hmac.compare_digest(
                execution.process_identity_digest,
                start_evidence.process_identity_digest,
            )
        ):
            await self._journal.begin_stop(
                current.job_id,
                current.dispatch_digest,
                reason_code="audit_source_ingest_execution_binding_mismatch",
            )
            return
        try:
            observation = _project_terminal_observation(
                current,
                execution,
                completed_at=utc_now(),
            )
        except (AuditPreflightCapsuleOutcomeUnknown, ValueError) as exc:
            await self._journal.begin_stop(
                current.job_id,
                current.dispatch_digest,
                reason_code=getattr(
                    exc,
                    "code",
                    "audit_source_ingest_result_invalid",
                ),
            )
            return
        await self._journal.record_terminal_observation(
            current.job_id,
            current.dispatch_digest,
            observation,
        )

    async def _report_finish(self, record: AuditPreflightRunnerJournalRecord) -> None:
        observation = record.terminal_observation
        if observation is None:
            raise AuditPreflightRunnerJournalConflict(record.job_id)
        async with self._callback_lock(record.job_id):
            current = await self._require_record(record.job_id)
            ack = await self._client.finish_audit_preflight(
                current.dispatch,
                lease=current.lease_envelope,
                state_version=current.state_version,
                status=observation.status,
                result=observation.result,
                safe_error_code=observation.safe_error_code,
                exit_receipt=observation.exit_receipt,
            )
            await self._journal.record_finish_ack(
                current.job_id,
                current.dispatch_digest,
                ack,
            )

    async def _report_stop(self, record: AuditPreflightRunnerJournalRecord) -> None:
        evidence = record.stop_evidence
        if evidence is None:
            raise AuditPreflightRunnerJournalConflict(record.job_id)
        reason_code = record.stop_reason_code or "audit_preflight_stop_required"
        cancelled = reason_code == "audit_preflight_cancel_requested"
        status = AuditPreflightJobStatus.CANCELLED if cancelled else AuditPreflightJobStatus.FAILED
        stop_receipt = AuditPreflightStopReceipt(
            job_id=record.job_id,
            effect_owner_digest=record.effect_owner_digest,
            lease_envelope_digest=record.current_lease_envelope_digest,
            capsule_id=evidence.capsule_id,
            source_node_id=record.dispatch.owner.source_node_id,
            runner_principal=record.lease_envelope.runner_principal,
            backend_id=record.dispatch.owner.backend_id,
            image_digest=record.dispatch.owner.image_digest,
            policy_digest=record.dispatch.owner.policy_digest,
            disposition=evidence.disposition,
            process_identity_digest=evidence.process_identity_digest,
            never_created_proof_digest=evidence.never_created_proof_digest,
            observed_terminal_state=evidence.observed_terminal_state,
            received_at=evidence.observed_at,
        )
        async with self._callback_lock(record.job_id):
            current = await self._require_record(record.job_id)
            ack = await self._client.stop_audit_preflight(
                current.dispatch,
                lease=current.lease_envelope,
                state_version=current.state_version,
                status=status,
                safe_error_code=None if cancelled else reason_code,
                stop_receipt=stop_receipt,
            )
            await self._journal.record_stop_ack(
                current.job_id,
                current.dispatch_digest,
                ack,
            )

    async def _renew_loop(
        self,
        job_id: str,
        dispatch_digest: str,
        cancellation: _CancellationState,
    ) -> None:
        while not self._closed and not cancellation.event.is_set():
            record = await self._require_record(job_id)
            if (
                record.callback_acknowledged
                or record.terminal_observation is not None
                or record.stop_evidence is not None
            ):
                return
            remaining = (record.lease_expires_at - utc_now()).total_seconds()
            if remaining <= 0:
                cancellation.request("audit_preflight_lease_lost")
                return
            delay = max(0.05, min(remaining / 3, 10.0))
            try:
                await asyncio.wait_for(cancellation.event.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                async with self._callback_lock(job_id):
                    current = await self._require_record(job_id)
                    grant = await self._client.renew_audit_preflight(
                        current.dispatch,
                        lease=current.lease_envelope,
                        state_version=current.state_version,
                    )
                    await self._journal.record_lease_grant(
                        job_id,
                        dispatch_digest,
                        grant,
                    )
                if grant.status is AuditPreflightJobStatus.CANCELLING:
                    await self._journal.begin_stop(
                        job_id,
                        dispatch_digest,
                        reason_code="audit_preflight_cancel_requested",
                    )
                    cancellation.request("audit_preflight_cancel_requested")
                    return
            except RunnerControlClientError as exc:
                if exc.status_code < 500:
                    cancellation.request("audit_preflight_lease_lost")
                    return
                await asyncio.sleep(min(self._reconnect_seconds, max(remaining, 0.05)))
            except AuditPreflightRunnerJournalConflict:
                return

    async def _best_effort_stop(
        self,
        job_id: str,
        dispatch_digest: str,
        cancellation: _CancellationState,
    ) -> None:
        try:
            record = await self._require_record(job_id)
            if record.callback_acknowledged:
                return
            if record.stop_intent_at is None:
                await self._journal.begin_stop(
                    job_id,
                    dispatch_digest,
                    reason_code=(cancellation.reason_code or "audit_preflight_runner_shutdown"),
                )
            await self._controller.stop(
                job_id,
                dispatch_digest,
                reason_code=(cancellation.reason_code or "audit_preflight_runner_shutdown"),
            )
        except Exception:
            logger.exception("Unable to stop Audit Preflight Job %s during shutdown", job_id)

    async def _require_record(self, job_id: str) -> AuditPreflightRunnerJournalRecord:
        record = await self._journal.get(job_id)
        if record is None:
            raise AuditPreflightRunnerJournalConflict(job_id)
        return record

    def _callback_lock(self, job_id: str) -> asyncio.Lock:
        return self._callback_locks.setdefault(job_id, asyncio.Lock())


def _project_terminal_observation(
    record: AuditPreflightRunnerJournalRecord,
    execution: SourceIngestExecutionResult,
    *,
    completed_at: datetime,
) -> AuditPreflightTerminalObservation:
    worker = execution.worker_result
    if completed_at >= record.dispatch.owner.expires_at:
        status: Literal[
            AuditPreflightJobStatus.SUCCEEDED,
            AuditPreflightJobStatus.REJECTED,
            AuditPreflightJobStatus.FAILED,
        ] = AuditPreflightJobStatus.FAILED
        result: AuditPreflightResult | None = None
        safe_error_code: str | None = "audit_preflight_result_expired"
    elif worker.outcome is SourceIngestWorkerOutcome.FAILED:
        status = AuditPreflightJobStatus.FAILED
        result = None
        safe_error_code = worker.safe_error_code or "audit_source_ingest_failed"
    else:
        status = (
            AuditPreflightJobStatus.REJECTED
            if worker.outcome is SourceIngestWorkerOutcome.REJECTED
            else AuditPreflightJobStatus.SUCCEEDED
        )
        safe_error_code = worker.safe_error_code
        result = _project_result(record, execution, completed_at=completed_at)
    exit_receipt = AuditPreflightExitReceipt(
        job_id=record.job_id,
        effect_owner_digest=record.effect_owner_digest,
        lease_envelope_digest=record.current_lease_envelope_digest,
        capsule_id=record.capsule_id,
        source_node_id=record.dispatch.owner.source_node_id,
        runner_principal=record.lease_envelope.runner_principal,
        backend_id=record.dispatch.owner.backend_id,
        image_digest=record.dispatch.owner.image_digest,
        policy_digest=record.dispatch.owner.policy_digest,
        process_identity_digest=execution.process_identity_digest,
        result_digest=result.result_digest if result is not None else None,
        terminal_state=AuditPreflightExitTerminalState(status.value),
        received_at=completed_at,
    )
    return AuditPreflightTerminalObservation(
        status=status,
        result=result,
        safe_error_code=safe_error_code,
        exit_receipt=exit_receipt,
        observed_at=completed_at,
    )


def _project_result(
    record: AuditPreflightRunnerJournalRecord,
    execution: SourceIngestExecutionResult,
    *,
    completed_at: datetime,
) -> AuditPreflightResult:
    worker = execution.worker_result
    if (
        worker.repository_identity_digest is None
        or worker.content_identity_digest is None
        or worker.git_component_digest is None
        or worker.git_proof_digest is None
        or worker.source_mount_identity_digest is None
        or worker.source_mount_proof_digest is None
    ):
        raise AuditPreflightCapsuleOutcomeUnknown("audit_source_ingest_result_proof_missing")
    blocking = bool(worker.blocking_errors)
    source_ingest_execution_proof = _domain_digest(
        SOURCE_INGEST_EXECUTION_PROOF_VERSION,
        {
            "backend_id": record.dispatch.owner.backend_id,
            "capsule_id": record.capsule_id,
            "container_id_digest": hashlib.sha256(
                execution.container_id.encode("ascii")
            ).hexdigest(),
            "image_digest": record.dispatch.owner.image_digest,
            "policy_digest": record.dispatch.owner.policy_digest,
            "prepare_proof_digest": execution.prepare_proof_digest,
            "process_identity_digest": execution.process_identity_digest,
            "request_digest": record.request_digest,
            "schema_version": SOURCE_INGEST_EXECUTION_PROOF_VERSION,
            "source_mount_identity_digest": worker.source_mount_identity_digest,
            "source_mount_proof_digest": worker.source_mount_proof_digest,
        },
    )
    capability_matrix = AuditPreflightCapabilityMatrix(
        entries=(
            AuditPreflightCapabilityFact(
                capability_id="detector_inventory",
                status=AuditPreflightCapabilityStatus.UNAVAILABLE,
                reason_code="audit_inventory_unavailable",
            ),
            AuditPreflightCapabilityFact(
                capability_id="git_metadata",
                status=AuditPreflightCapabilityStatus.AVAILABLE,
                component_version=worker.git_version,
                component_digest=worker.git_component_digest,
                proof_digest=worker.git_proof_digest,
            ),
            AuditPreflightCapabilityFact(
                capability_id="source_ingest",
                status=AuditPreflightCapabilityStatus.AVAILABLE,
                component_version=SOURCE_INGEST_BACKEND_COMPONENT_VERSION,
                component_digest=_domain_digest(
                    SOURCE_INGEST_BACKEND_COMPONENT_VERSION,
                    {
                        "backend_id": record.dispatch.owner.backend_id,
                        "image_digest": record.dispatch.owner.image_digest,
                        "policy_digest": record.dispatch.owner.policy_digest,
                    },
                ),
                proof_digest=source_ingest_execution_proof,
            ),
        )
    )
    budget_reason = worker.blocking_errors[0] if blocking else "audit_inventory_unavailable"
    budget = AuditPreflightMinimumFeasibleBudget(
        status=(
            AuditPreflightBudgetStatus.BLOCKING
            if blocking
            else AuditPreflightBudgetStatus.UNAVAILABLE
        ),
        provenance_digest=_domain_digest(
            "riftx.audit-preflight-budget-provenance/v1",
            {
                "blocking_errors": list(worker.blocking_errors),
                "file_count": worker.file_count,
                "language_estimates": [
                    item.model_dump(mode="json") for item in worker.language_estimates
                ],
                "total_bytes": worker.total_bytes,
            },
        ),
        reason_code=budget_reason,
    )
    request = record.dispatch.request
    return AuditPreflightResult(
        preflight_job_id=record.job_id,
        request_digest=record.request_digest,
        effect_owner_digest=record.effect_owner_digest,
        source_node_id=record.dispatch.owner.source_node_id,
        source_root_identity_digest=record.dispatch.owner.source_root_identity_digest,
        repository_identity_digest=worker.repository_identity_digest,
        content_identity_digest=worker.content_identity_digest,
        backend_id=record.dispatch.owner.backend_id,
        image_digest=record.dispatch.owner.image_digest,
        policy_digest=record.dispatch.owner.policy_digest,
        capsule_prepare_proof_digest=execution.prepare_proof_digest,
        target_kind=request.target.kind,
        revision=request.target.revision,
        base_revision=request.target.base_revision,
        mode=request.mode,
        include_untracked=request.target.include_untracked,
        head_revision=worker.head_revision,
        resolved_revision=worker.resolved_revision,
        resolved_base_revision=worker.resolved_base_revision,
        merge_base_revision=worker.merge_base_revision,
        dirty=worker.dirty,
        staged=worker.staged,
        unstaged=worker.unstaged,
        untracked=worker.untracked,
        file_count=worker.file_count,
        total_bytes=worker.total_bytes,
        max_file_bytes=worker.max_file_bytes,
        language_estimates=tuple(
            AuditPreflightLanguageEstimate(
                language_id=item.language_id,
                file_count=item.file_count,
                total_bytes=item.total_bytes,
            )
            for item in worker.language_estimates
        ),
        capability_matrix=capability_matrix,
        capability_warnings=worker.capability_warnings,
        blocking_errors=worker.blocking_errors,
        minimum_feasible_budget=budget,
        completed_at=completed_at,
        expires_at=record.dispatch.owner.expires_at,
    )


def _capsule_reference(
    record: SourceIngestCapsuleRecord,
) -> AuditPreflightCapsuleReference | None:
    if record.container_id is None or record.prepare_proof_digest is None:
        return None
    return AuditPreflightCapsuleReference(
        capsule_id=record.capsule_id,
        locator=record.container_id,
        prepare_proof_digest=record.prepare_proof_digest,
    )


def _recovered_start_evidence(
    record: SourceIngestCapsuleRecord,
) -> AuditPreflightCapsuleStartEvidence | None:
    if record.process_identity_digest is None or record.lifecycle_state not in {
        "running",
        "terminal",
        "stop_requested",
        "stop_observed",
    }:
        return None
    return AuditPreflightCapsuleStartEvidence(
        capsule_id=record.capsule_id,
        process_identity_digest=record.process_identity_digest,
        observed_state=record.observed_state or record.lifecycle_state,
        observed_at=utc_now(),
    )


def _recovered_stop_evidence(
    record: SourceIngestCapsuleRecord,
) -> AuditPreflightCapsuleStopEvidence | None:
    if record.lifecycle_state != "stop_observed" or record.process_identity_digest is None:
        return None
    return AuditPreflightCapsuleStopEvidence(
        disposition=AuditPreflightStopDisposition.STOPPED,
        capsule_id=record.capsule_id,
        process_identity_digest=record.process_identity_digest,
        observed_terminal_state=_observed_terminal_state(record.observed_state or "cancelled"),
        observed_at=utc_now(),
    )


def _observed_terminal_state(value: str) -> AuditPreflightObservedTerminalState:
    if value == "exited":
        return AuditPreflightObservedTerminalState.EXITED
    if value in {"dead", "failed"}:
        return AuditPreflightObservedTerminalState.FAILED
    return AuditPreflightObservedTerminalState.CANCELLED


def _domain_digest(domain: str, value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded).hexdigest()


__all__ = [
    "AuditPreflightCapsuleOutcomeUnknown",
    "AuditPreflightCapsuleRecovery",
    "AuditPreflightControlClient",
    "AuditPreflightExecutionBackend",
    "AuditPreflightRunner",
    "DockerAuditPreflightCapsuleBackend",
]
