"""Crash-durable Runner ownership for Audit Preflight capsules.

This module is deliberately independent from the ordinary Runner command journal.
An Audit Preflight Job has no Run identity and therefore cannot reuse
``RunnerCommandOwnership`` or any Run-scoped command record.

The journal stores only Runner-local recovery material.  In particular, a
prepare/start/stop intent is fsync-durable before the corresponding backend call,
and capsule cleanup is forbidden until a terminal callback acknowledgement is
itself durable.  A created-but-not-started capsule therefore remains recoverable
and can never be inferred to be ``never_created`` merely from a process restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, field_validator, model_validator

from riftx.domain.audit_preflight import (
    AuditPreflightExitReceipt,
    AuditPreflightJobStatus,
    AuditPreflightLeaseEnvelope,
    AuditPreflightObservedTerminalState,
    AuditPreflightResult,
    AuditPreflightStopDisposition,
    AuditPreflightStrictModel,
    PreflightDigest,
    PreflightId,
    PreflightSafeCode,
)
from riftx.domain.audit_preflight_wire import (
    AuditPreflightCallbackAck,
    AuditPreflightDispatchEnvelope,
    AuditPreflightLeaseGrant,
    AuditPreflightStartGrant,
)
from riftx.domain.base import utc_now

from ._durable_file import atomic_write_json, locked_file

AUDIT_PREFLIGHT_RUNNER_JOURNAL_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-runner-journal/v1"
] = "riftx.audit-preflight-runner-journal/v1"
AUDIT_PREFLIGHT_RUNNER_RECORD_SCHEMA_VERSION: Literal["riftx.audit-preflight-runner-record/v1"] = (
    "riftx.audit-preflight-runner-record/v1"
)
AUDIT_PREFLIGHT_RUNNER_DISPATCH_DIGEST_DOMAIN = "riftx.audit-preflight-runner-dispatch/v1"
AUDIT_PREFLIGHT_RUNNER_NEVER_CREATED_PROOF_DOMAIN = (
    "riftx.audit-preflight-runner-never-created-proof/v1"
)

_MAX_CAPSULE_LOCATOR_BYTES = 4_096
_TERMINAL_JOB_STATUSES = frozenset(
    {
        AuditPreflightJobStatus.SUCCEEDED,
        AuditPreflightJobStatus.REJECTED,
        AuditPreflightJobStatus.FAILED,
        AuditPreflightJobStatus.CANCELLED,
    }
)


class AuditPreflightRunnerJournalError(RuntimeError):
    """Base class for path-free Preflight journal failures."""


class AuditPreflightRunnerJournalConflict(AuditPreflightRunnerJournalError):
    """An immutable dispatch or append-only recovery fact drifted."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"Audit Preflight Job {job_id!r} conflicts with its durable Runner journal"
        )
        self.job_id = job_id


class AuditPreflightRunnerRecoveryRequired(AuditPreflightRunnerJournalError):
    """A prior uncertain backend call must be probed instead of replayed."""

    def __init__(self, job_id: str, action: AuditPreflightRecoveryAction) -> None:
        super().__init__(
            f"Audit Preflight Job {job_id!r} requires Runner recovery action {action.value!r}"
        )
        self.job_id = job_id
        self.action = action


class AuditPreflightRecoveryAction(StrEnum):
    """The next safe action after opening a durable Runner journal."""

    PREPARE = "prepare"
    PROBE_PREPARE = "probe_prepare"
    START = "start"
    PROBE_START = "probe_start"
    REPORT_START = "report_start"
    WAIT_OR_PROBE = "wait_or_probe"
    REPORT_FINISH = "report_finish"
    STOP = "stop"
    REPORT_STOP = "report_stop"
    CLEANUP = "cleanup"
    NONE = "none"


class AuditPreflightCapsuleReference(AuditPreflightStrictModel):
    """Opaque Runner-local locator persisted before capsule start."""

    capsule_id: PreflightId
    locator: str = Field(min_length=1, max_length=_MAX_CAPSULE_LOCATOR_BYTES, repr=False)
    prepare_proof_digest: PreflightDigest

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: str) -> str:
        if value != value.strip() or unicodedata.normalize("NFC", value) != value:
            raise ValueError("Audit Preflight capsule locator is not canonical")
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("Audit Preflight capsule locator contains control characters")
        if len(value.encode("utf-8")) > _MAX_CAPSULE_LOCATOR_BYTES:
            raise ValueError("Audit Preflight capsule locator exceeds its byte limit")
        return value


class AuditPreflightCapsuleStartEvidence(AuditPreflightStrictModel):
    """Affirmative local evidence that the capsule effect was started."""

    capsule_id: PreflightId
    process_identity_digest: PreflightDigest
    observed_state: PreflightSafeCode
    observed_at: AwareDatetime


class AuditPreflightCapsuleStopEvidence(AuditPreflightStrictModel):
    """Affirmative backend evidence; absence/timeouts are intentionally unmodelled."""

    disposition: AuditPreflightStopDisposition
    capsule_id: PreflightId | None = None
    process_identity_digest: PreflightDigest | None = None
    never_created_proof_digest: PreflightDigest | None = None
    observed_terminal_state: AuditPreflightObservedTerminalState
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_evidence(self) -> AuditPreflightCapsuleStopEvidence:
        if self.disposition is AuditPreflightStopDisposition.STOPPED:
            if self.capsule_id is None or self.process_identity_digest is None:
                raise ValueError("stopped capsule evidence requires capsule/process identity")
            if self.never_created_proof_digest is not None:
                raise ValueError("stopped capsule evidence cannot claim never-created proof")
            if self.observed_terminal_state is AuditPreflightObservedTerminalState.NOT_CREATED:
                raise ValueError("stopped capsule evidence must observe an actual effect")
        else:
            if self.capsule_id is not None or self.process_identity_digest is not None:
                raise ValueError("never-created evidence cannot claim capsule/process identity")
            if self.never_created_proof_digest is None:
                raise ValueError("never-created evidence requires affirmative proof")
            if self.observed_terminal_state is not AuditPreflightObservedTerminalState.NOT_CREATED:
                raise ValueError("never-created evidence must observe not_created")
        return self


class AuditPreflightTerminalObservation(AuditPreflightStrictModel):
    """Exact finish material retained until the Control Plane acknowledges it."""

    status: Literal[
        AuditPreflightJobStatus.SUCCEEDED,
        AuditPreflightJobStatus.REJECTED,
        AuditPreflightJobStatus.FAILED,
    ]
    result: AuditPreflightResult | None = Field(default=None, repr=False)
    safe_error_code: PreflightSafeCode | None = None
    exit_receipt: AuditPreflightExitReceipt
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_observation(self) -> AuditPreflightTerminalObservation:
        if self.status is AuditPreflightJobStatus.SUCCEEDED:
            if self.result is None or self.safe_error_code is not None:
                raise ValueError("successful terminal observation requires only a result")
        elif self.status is AuditPreflightJobStatus.REJECTED:
            if self.result is None or self.safe_error_code is None:
                raise ValueError("rejected terminal observation requires result and safe error")
        elif self.result is not None or self.safe_error_code is None:
            raise ValueError("failed terminal observation requires only a safe error")
        if self.exit_receipt.terminal_state.value != self.status.value:
            raise ValueError("terminal observation and exit receipt status differ")
        result_digest = self.result.result_digest if self.result is not None else None
        if self.exit_receipt.result_digest != result_digest:
            raise ValueError("terminal observation result and exit receipt differ")
        return self


class AuditPreflightRunnerJournalRecord(AuditPreflightStrictModel):
    """One append-only, digest-bound local recovery record."""

    schema_version: Literal["riftx.audit-preflight-runner-record/v1"] = (
        AUDIT_PREFLIGHT_RUNNER_RECORD_SCHEMA_VERSION
    )
    job_id: PreflightId
    dispatch_digest: PreflightDigest
    dispatch_json: str = Field(min_length=2, repr=False)
    effect_owner_digest: PreflightDigest
    request_digest: PreflightDigest
    initial_lease_envelope_digest: PreflightDigest
    current_lease_envelope_digest: PreflightDigest
    lease_expected_state_version: int = Field(strict=True, ge=1)
    state_version: int = Field(strict=True, ge=1)
    lease_expires_at: AwareDatetime
    capsule_id: PreflightId
    admitted_at: AwareDatetime
    updated_at: AwareDatetime
    prepare_intent_at: AwareDatetime | None = None
    capsule: AuditPreflightCapsuleReference | None = Field(default=None, repr=False)
    start_intent_at: AwareDatetime | None = None
    start_evidence: AuditPreflightCapsuleStartEvidence | None = None
    start_grant: AuditPreflightStartGrant | None = None
    terminal_observation: AuditPreflightTerminalObservation | None = Field(
        default=None,
        repr=False,
    )
    finish_ack: AuditPreflightCallbackAck | None = None
    stop_intent_at: AwareDatetime | None = None
    stop_reason_code: PreflightSafeCode | None = None
    stop_failure_code: PreflightSafeCode | None = None
    stop_evidence: AuditPreflightCapsuleStopEvidence | None = None
    stop_ack: AuditPreflightCallbackAck | None = None
    cleanup_intent_at: AwareDatetime | None = None
    cleaned_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_record(self) -> AuditPreflightRunnerJournalRecord:
        dispatch = _parse_dispatch(self.dispatch_json)
        if _dispatch_digest(dispatch) != self.dispatch_digest:
            raise ValueError("Audit Preflight durable dispatch digest does not match")
        if (
            dispatch.owner.job_id != self.job_id
            or dispatch.owner.effect_owner_digest != self.effect_owner_digest
            or dispatch.request.request_digest != self.request_digest
            or dispatch.lease.lease_envelope_digest != self.initial_lease_envelope_digest
            or dispatch.capsule_id != self.capsule_id
        ):
            raise ValueError("Audit Preflight durable dispatch binding does not match")
        if self.updated_at < self.admitted_at:
            raise ValueError("Audit Preflight journal time moved backwards")
        if self.lease_expires_at > dispatch.owner.expires_at:
            raise ValueError("Audit Preflight journal lease exceeds owner lifetime")
        current_lease = self.lease_envelope
        if current_lease.lease_envelope_digest != self.current_lease_envelope_digest:
            raise ValueError("Audit Preflight journal current lease digest does not match")

        if self.capsule is not None:
            if self.prepare_intent_at is None or self.capsule.capsule_id != self.capsule_id:
                raise ValueError("Audit Preflight capsule lacks its durable prepare intent")
        if self.start_intent_at is not None and self.capsule is None:
            raise ValueError("Audit Preflight start intent lacks a durable capsule locator")
        if self.start_evidence is not None:
            if self.start_intent_at is None or self.start_evidence.capsule_id != self.capsule_id:
                raise ValueError("Audit Preflight start evidence lacks its durable intent")
            if self.start_evidence.observed_at < self.start_intent_at:
                raise ValueError("Audit Preflight start evidence precedes its durable intent")
        if self.start_grant is not None:
            if self.start_evidence is None:
                raise ValueError("Audit Preflight start ACK precedes physical start evidence")
            if (
                self.start_grant.job_id != self.job_id
                or self.start_grant.capsule_id != self.capsule_id
            ):
                raise ValueError("Audit Preflight start ACK binding does not match")

        if self.terminal_observation is not None:
            if self.start_evidence is None:
                raise ValueError("Audit Preflight terminal observation lacks start evidence")
            if self.terminal_observation.exit_receipt.job_id != self.job_id:
                raise ValueError("Audit Preflight terminal observation job does not match")
            if (
                self.terminal_observation.exit_receipt.lease_envelope_digest
                != self.current_lease_envelope_digest
            ):
                raise ValueError("Audit Preflight terminal observation lease does not match")
            exit_receipt = self.terminal_observation.exit_receipt
            if (
                exit_receipt.effect_owner_digest != self.effect_owner_digest
                or exit_receipt.capsule_id != self.capsule_id
            ):
                raise ValueError("Audit Preflight terminal observation owner does not match")
            result = self.terminal_observation.result
            if result is not None and (
                result.preflight_job_id != self.job_id
                or result.request_digest != self.request_digest
                or result.effect_owner_digest != self.effect_owner_digest
                or self.capsule is None
                or result.capsule_prepare_proof_digest != self.capsule.prepare_proof_digest
            ):
                raise ValueError("Audit Preflight terminal result binding does not match")
        if self.finish_ack is not None:
            if self.terminal_observation is None:
                raise ValueError("Audit Preflight finish ACK lacks terminal observation")
            if self.finish_ack.job_id != self.job_id or self.finish_ack.status not in {
                AuditPreflightJobStatus.SUCCEEDED,
                AuditPreflightJobStatus.REJECTED,
                AuditPreflightJobStatus.FAILED,
            }:
                raise ValueError("Audit Preflight finish ACK binding does not match")
            if (
                self.finish_ack.status is not self.terminal_observation.status
                or self.finish_ack.finished_at is None
            ):
                raise ValueError("Audit Preflight finish ACK terminal fact does not match")

        if (self.stop_intent_at is None) != (self.stop_reason_code is None):
            raise ValueError("Audit Preflight stop intent and reason must appear together")
        if self.stop_intent_at is None:
            if self.stop_failure_code is not None:
                raise ValueError("Audit Preflight stop failure lacks its durable intent")
        elif self.stop_reason_code == "audit_preflight_cancel_requested":
            if self.stop_failure_code == "audit_preflight_cancel_requested":
                raise ValueError("Audit Preflight cancel reason cannot be a local failure fact")
        elif self.stop_failure_code != self.stop_reason_code:
            raise ValueError("Audit Preflight local stop reason must retain its failure fact")
        if self.stop_evidence is not None:
            if self.stop_intent_at is None:
                raise ValueError("Audit Preflight stop evidence lacks its durable intent")
            if self.stop_evidence.observed_at < self.stop_intent_at:
                raise ValueError("Audit Preflight stop evidence precedes its durable intent")
            if (
                self.stop_evidence.disposition is AuditPreflightStopDisposition.STOPPED
                and self.stop_evidence.capsule_id != self.capsule_id
            ):
                raise ValueError("Audit Preflight stopped capsule identity does not match")
            if (
                self.stop_evidence.disposition is AuditPreflightStopDisposition.NEVER_CREATED
                and self.capsule is not None
            ):
                raise ValueError("prepared Audit Preflight capsule cannot be never-created")
        if self.stop_ack is not None:
            if self.stop_evidence is None:
                raise ValueError("Audit Preflight stop ACK lacks affirmative stop evidence")
            expected_stop_status = (
                AuditPreflightJobStatus.CANCELLED
                if self.stop_reason_code == "audit_preflight_cancel_requested"
                else AuditPreflightJobStatus.FAILED
            )
            if self.stop_ack.job_id != self.job_id or self.stop_ack.status not in {
                AuditPreflightJobStatus.CANCELLED,
                AuditPreflightJobStatus.FAILED,
            }:
                raise ValueError("Audit Preflight stop ACK binding does not match")
            if self.stop_ack.status is not expected_stop_status:
                raise ValueError("Audit Preflight stop ACK contradicts its effective fence")
            if self.stop_ack.finished_at is None:
                raise ValueError("Audit Preflight stop ACK requires terminal time")
        if self.finish_ack is not None and self.stop_ack is not None:
            raise ValueError("Audit Preflight journal cannot acknowledge two terminal paths")

        if self.cleanup_intent_at is not None and not self.callback_acknowledged:
            raise ValueError("Audit Preflight cleanup requires a durable callback ACK")
        if self.cleaned_at is not None and self.cleanup_intent_at is None:
            raise ValueError("Audit Preflight cleanup completion lacks its durable intent")
        return self

    @property
    def callback_acknowledged(self) -> bool:
        return self.finish_ack is not None or self.stop_ack is not None

    @property
    def recovery_action(self) -> AuditPreflightRecoveryAction:
        if self.cleaned_at is not None:
            return AuditPreflightRecoveryAction.NONE
        if self.cleanup_intent_at is not None or self.callback_acknowledged:
            return AuditPreflightRecoveryAction.CLEANUP
        if self.stop_intent_at is not None:
            if self.stop_evidence is not None:
                return AuditPreflightRecoveryAction.REPORT_STOP
            return AuditPreflightRecoveryAction.STOP
        if self.terminal_observation is not None:
            return AuditPreflightRecoveryAction.REPORT_FINISH
        if self.start_evidence is not None:
            if self.start_grant is None:
                return AuditPreflightRecoveryAction.REPORT_START
            return AuditPreflightRecoveryAction.WAIT_OR_PROBE
        if self.start_intent_at is not None:
            return AuditPreflightRecoveryAction.PROBE_START
        if self.capsule is not None:
            return AuditPreflightRecoveryAction.START
        if self.prepare_intent_at is not None:
            return AuditPreflightRecoveryAction.PROBE_PREPARE
        return AuditPreflightRecoveryAction.PREPARE

    @property
    def dispatch(self) -> AuditPreflightDispatchEnvelope:
        return _parse_dispatch(self.dispatch_json)

    @property
    def lease_envelope(self) -> AuditPreflightLeaseEnvelope:
        dispatch = self.dispatch
        return AuditPreflightLeaseEnvelope(
            owner=dispatch.owner,
            runner_principal=dispatch.lease.runner_principal,
            lease_id=dispatch.lease.lease_id,
            lease_expires_at=self.lease_expires_at,
            expected_state_version=self.lease_expected_state_version,
            output_contract_digest=dispatch.lease.output_contract_digest,
            lease_envelope_digest=self.current_lease_envelope_digest,
        )


class AuditPreflightCapsuleBackend(Protocol):
    """Runner-only backend boundary suitable for production adapters and fakes."""

    async def prepare(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
    ) -> AuditPreflightCapsuleReference: ...

    async def start(
        self,
        capsule: AuditPreflightCapsuleReference,
    ) -> AuditPreflightCapsuleStartEvidence: ...

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


class AuditPreflightRunnerJournal:
    """Cross-process-safe, fsync-backed Preflight recovery store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def admit(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        *,
        admitted_at: datetime | None = None,
    ) -> tuple[AuditPreflightRunnerJournalRecord, bool]:
        timestamp = admitted_at or utc_now()
        async with self._lock:
            return await asyncio.to_thread(self._admit_locked, dispatch, timestamp)

    async def get(self, job_id: str) -> AuditPreflightRunnerJournalRecord | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_locked, job_id)

    async def list_recoverable(self) -> Sequence[AuditPreflightRunnerJournalRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._list_recoverable_locked)

    async def list_records(self) -> Sequence[AuditPreflightRunnerJournalRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._list_records_locked)

    async def record_lease_grant(
        self,
        job_id: str,
        dispatch_digest: str,
        grant: AuditPreflightLeaseGrant,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._update(
            job_id,
            dispatch_digest,
            recorded_at or utc_now(),
            lambda current, timestamp: _with_lease_grant(current, grant, timestamp),
        )

    async def begin_prepare(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._set_once(
            job_id,
            dispatch_digest,
            field_name="prepare_intent_at",
            value=recorded_at or utc_now(),
            accept_existing=True,
        )

    async def record_prepared(
        self,
        job_id: str,
        dispatch_digest: str,
        capsule: AuditPreflightCapsuleReference,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._set_once(
            job_id,
            dispatch_digest,
            field_name="capsule",
            value=capsule,
            recorded_at=recorded_at,
            requires=("prepare_intent_at",),
        )

    async def begin_start(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._set_once(
            job_id,
            dispatch_digest,
            field_name="start_intent_at",
            value=recorded_at or utc_now(),
            requires=("capsule",),
            accept_existing=True,
        )

    async def record_started(
        self,
        job_id: str,
        dispatch_digest: str,
        evidence: AuditPreflightCapsuleStartEvidence,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._set_once(
            job_id,
            dispatch_digest,
            field_name="start_evidence",
            value=evidence,
            recorded_at=recorded_at,
            requires=("start_intent_at",),
        )

    async def record_start_grant(
        self,
        job_id: str,
        dispatch_digest: str,
        grant: AuditPreflightStartGrant,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._update(
            job_id,
            dispatch_digest,
            recorded_at or utc_now(),
            lambda current, timestamp: _with_start_grant(current, grant, timestamp),
        )

    async def record_terminal_observation(
        self,
        job_id: str,
        dispatch_digest: str,
        observation: AuditPreflightTerminalObservation,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._set_once(
            job_id,
            dispatch_digest,
            field_name="terminal_observation",
            value=observation,
            recorded_at=recorded_at,
            requires=("start_evidence",),
        )

    async def record_finish_ack(
        self,
        job_id: str,
        dispatch_digest: str,
        ack: AuditPreflightCallbackAck,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._update(
            job_id,
            dispatch_digest,
            recorded_at or utc_now(),
            lambda current, timestamp: _with_callback_ack(
                current,
                field_name="finish_ack",
                ack=ack,
                timestamp=timestamp,
                requires="terminal_observation",
            ),
        )

    async def begin_stop(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        reason_code: str,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        timestamp = recorded_at or utc_now()
        return await self._update(
            job_id,
            dispatch_digest,
            timestamp,
            lambda current, changed_at: _with_stop_intent(
                current,
                reason_code=reason_code,
                timestamp=changed_at,
            ),
        )

    async def record_stop_evidence(
        self,
        job_id: str,
        dispatch_digest: str,
        evidence: AuditPreflightCapsuleStopEvidence,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._set_once(
            job_id,
            dispatch_digest,
            field_name="stop_evidence",
            value=evidence,
            recorded_at=recorded_at,
            requires=("stop_intent_at",),
        )

    async def record_stop_ack(
        self,
        job_id: str,
        dispatch_digest: str,
        ack: AuditPreflightCallbackAck,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._update(
            job_id,
            dispatch_digest,
            recorded_at or utc_now(),
            lambda current, timestamp: _with_callback_ack(
                current,
                field_name="stop_ack",
                ack=ack,
                timestamp=timestamp,
                requires="stop_evidence",
            ),
        )

    async def begin_cleanup(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._update(
            job_id,
            dispatch_digest,
            recorded_at or utc_now(),
            _with_cleanup_intent,
        )

    async def record_cleaned(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        return await self._set_once(
            job_id,
            dispatch_digest,
            field_name="cleaned_at",
            value=recorded_at or utc_now(),
            requires=("cleanup_intent_at",),
            accept_existing=True,
        )

    async def _set_once(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        field_name: str,
        value: object,
        recorded_at: datetime | None = None,
        requires: tuple[str, ...] = (),
        accept_existing: bool = False,
    ) -> AuditPreflightRunnerJournalRecord:
        timestamp = recorded_at or (value if isinstance(value, datetime) else utc_now())
        assert isinstance(timestamp, datetime)
        return await self._update(
            job_id,
            dispatch_digest,
            timestamp,
            lambda current, changed_at: _append_once(
                current,
                field_name=field_name,
                value=value,
                timestamp=changed_at,
                requires=requires,
                accept_existing=accept_existing,
            ),
        )

    async def _update(
        self,
        job_id: str,
        dispatch_digest: str,
        timestamp: datetime,
        mutation: Callable[
            [AuditPreflightRunnerJournalRecord, datetime],
            AuditPreflightRunnerJournalRecord,
        ],
    ) -> AuditPreflightRunnerJournalRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._update_locked,
                job_id,
                dispatch_digest,
                timestamp,
                mutation,
            )

    def _admit_locked(
        self,
        dispatch: AuditPreflightDispatchEnvelope,
        admitted_at: datetime,
    ) -> tuple[AuditPreflightRunnerJournalRecord, bool]:
        canonical = _canonical_json(dispatch.model_dump(mode="json"))
        digest = _dispatch_digest(dispatch)
        with locked_file(self.path):
            records = self._read()
            existing = records.get(dispatch.owner.job_id)
            if existing is not None:
                if hmac.compare_digest(existing.dispatch_digest, digest) and hmac.compare_digest(
                    existing.dispatch_json, canonical
                ):
                    return existing, False
                raise AuditPreflightRunnerJournalConflict(dispatch.owner.job_id)
            record = AuditPreflightRunnerJournalRecord(
                job_id=dispatch.owner.job_id,
                dispatch_digest=digest,
                dispatch_json=canonical,
                effect_owner_digest=dispatch.owner.effect_owner_digest,
                request_digest=dispatch.request.request_digest,
                initial_lease_envelope_digest=dispatch.lease.lease_envelope_digest,
                current_lease_envelope_digest=dispatch.lease.lease_envelope_digest,
                lease_expected_state_version=dispatch.lease.expected_state_version,
                state_version=dispatch.state_version,
                lease_expires_at=dispatch.lease.lease_expires_at,
                capsule_id=dispatch.capsule_id,
                admitted_at=admitted_at,
                updated_at=admitted_at,
            )
            records[record.job_id] = record
            self._write(records)
            return record, True

    def _get_locked(self, job_id: str) -> AuditPreflightRunnerJournalRecord | None:
        with locked_file(self.path):
            return self._read().get(job_id)

    def _list_recoverable_locked(self) -> Sequence[AuditPreflightRunnerJournalRecord]:
        with locked_file(self.path):
            return tuple(
                record
                for record in sorted(self._read().values(), key=lambda item: item.job_id)
                if record.recovery_action is not AuditPreflightRecoveryAction.NONE
            )

    def _list_records_locked(self) -> Sequence[AuditPreflightRunnerJournalRecord]:
        with locked_file(self.path):
            return tuple(sorted(self._read().values(), key=lambda item: item.job_id))

    def _update_locked(
        self,
        job_id: str,
        dispatch_digest: str,
        timestamp: datetime,
        mutation: Callable[
            [AuditPreflightRunnerJournalRecord, datetime],
            AuditPreflightRunnerJournalRecord,
        ],
    ) -> AuditPreflightRunnerJournalRecord:
        with locked_file(self.path):
            records = self._read()
            current = records.get(job_id)
            if current is None or not hmac.compare_digest(
                current.dispatch_digest,
                dispatch_digest,
            ):
                raise AuditPreflightRunnerJournalConflict(job_id)
            if timestamp < current.updated_at:
                raise AuditPreflightRunnerJournalConflict(job_id)
            updated = mutation(current, timestamp)
            if updated != current:
                records[job_id] = updated
                self._write(records)
            return updated

    def _read(self) -> dict[str, AuditPreflightRunnerJournalRecord]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            raise AuditPreflightRunnerJournalError(
                "Audit Preflight Runner journal is unavailable or corrupted"
            ) from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "records"}
            or raw.get("schema_version") != AUDIT_PREFLIGHT_RUNNER_JOURNAL_SCHEMA_VERSION
            or not isinstance(raw.get("records"), list)
        ):
            raise AuditPreflightRunnerJournalError(
                "Audit Preflight Runner journal has an invalid shape"
            )
        records: dict[str, AuditPreflightRunnerJournalRecord] = {}
        try:
            for item in raw["records"]:
                record = AuditPreflightRunnerJournalRecord.model_validate_json(
                    _canonical_json(item)
                )
                if record.job_id in records:
                    raise ValueError("duplicate job")
                records[record.job_id] = record
        except (TypeError, ValueError) as exc:
            raise AuditPreflightRunnerJournalError(
                "Audit Preflight Runner journal contains an invalid record"
            ) from exc
        return records

    def _write(self, records: dict[str, AuditPreflightRunnerJournalRecord]) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": AUDIT_PREFLIGHT_RUNNER_JOURNAL_SCHEMA_VERSION,
                "records": [
                    record.model_dump(mode="json")
                    for record in sorted(records.values(), key=lambda item: item.job_id)
                ],
            },
        )


class DurableAuditPreflightCapsuleController:
    """Orders backend effects behind durable journal barriers."""

    def __init__(
        self,
        *,
        journal: AuditPreflightRunnerJournal,
        backend: AuditPreflightCapsuleBackend,
    ) -> None:
        self._journal = journal
        self._backend = backend

    async def prepare(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        existing = await self._journal.get(job_id)
        if (
            existing is not None
            and existing.prepare_intent_at is not None
            and existing.capsule is None
        ):
            raise AuditPreflightRunnerRecoveryRequired(
                job_id,
                AuditPreflightRecoveryAction.PROBE_PREPARE,
            )
        record = await self._journal.begin_prepare(
            job_id,
            dispatch_digest,
            recorded_at=recorded_at,
        )
        if record.capsule is not None:
            return record
        capsule = await self._backend.prepare(record.dispatch)
        return await self._journal.record_prepared(
            job_id,
            dispatch_digest,
            capsule,
            recorded_at=recorded_at,
        )

    async def start(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        existing = await self._journal.get(job_id)
        if (
            existing is not None
            and existing.start_intent_at is not None
            and existing.start_evidence is None
        ):
            raise AuditPreflightRunnerRecoveryRequired(
                job_id,
                AuditPreflightRecoveryAction.PROBE_START,
            )
        record = await self._journal.begin_start(
            job_id,
            dispatch_digest,
            recorded_at=recorded_at,
        )
        if record.start_evidence is not None:
            return record
        capsule = record.capsule
        if capsule is None:  # pragma: no cover - journal validation owns this invariant
            raise AuditPreflightRunnerJournalConflict(job_id)
        evidence = await self._backend.start(capsule)
        return await self._journal.record_started(
            job_id,
            dispatch_digest,
            evidence,
            recorded_at=recorded_at,
        )

    async def stop(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        reason_code: str,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        record = await self._journal.begin_stop(
            job_id,
            dispatch_digest,
            reason_code=reason_code,
            recorded_at=recorded_at,
        )
        if record.stop_evidence is not None:
            return record
        if record.prepare_intent_at is None:
            if record.capsule is not None:
                raise AuditPreflightRunnerJournalConflict(job_id)
            observed_at = recorded_at or utc_now()
            evidence = AuditPreflightCapsuleStopEvidence(
                disposition=AuditPreflightStopDisposition.NEVER_CREATED,
                never_created_proof_digest=_never_created_proof_digest(record),
                observed_terminal_state=AuditPreflightObservedTerminalState.NOT_CREATED,
                observed_at=observed_at,
            )
            return await self._journal.record_stop_evidence(
                job_id,
                dispatch_digest,
                evidence,
                recorded_at=observed_at,
            )
        evidence = await self._backend.stop(
            capsule_id=record.capsule_id,
            capsule=record.capsule,
        )
        return await self._journal.record_stop_evidence(
            job_id,
            dispatch_digest,
            evidence,
            recorded_at=recorded_at,
        )

    async def cleanup(
        self,
        job_id: str,
        dispatch_digest: str,
        *,
        recorded_at: datetime | None = None,
    ) -> AuditPreflightRunnerJournalRecord:
        record = await self._journal.begin_cleanup(
            job_id,
            dispatch_digest,
            recorded_at=recorded_at,
        )
        if record.cleaned_at is not None:
            return record
        await self._backend.cleanup(
            capsule_id=record.capsule_id,
            capsule=record.capsule,
        )
        return await self._journal.record_cleaned(
            job_id,
            dispatch_digest,
            recorded_at=recorded_at,
        )


def audit_preflight_dispatch_digest(
    dispatch: AuditPreflightDispatchEnvelope,
) -> str:
    """Public stable identity used by callers to fence journal mutations."""

    return _dispatch_digest(dispatch)


def _with_lease_grant(
    current: AuditPreflightRunnerJournalRecord,
    grant: AuditPreflightLeaseGrant,
    timestamp: datetime,
) -> AuditPreflightRunnerJournalRecord:
    if grant.job_id != current.job_id:
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    if grant.status not in {
        AuditPreflightJobStatus.CLAIMED,
        AuditPreflightJobStatus.RUNNING,
        AuditPreflightJobStatus.CANCELLING,
        AuditPreflightJobStatus.OUTCOME_UNKNOWN,
    }:
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    if (
        grant.state_version == current.state_version
        and grant.lease_envelope_digest == current.current_lease_envelope_digest
        and grant.lease_expires_at == current.lease_expires_at
    ):
        return current
    if (
        current.terminal_observation is not None
        or current.stop_evidence is not None
        or (
            current.stop_intent_at is not None
            and grant.status is not AuditPreflightJobStatus.CANCELLING
        )
        or grant.state_version <= current.state_version
        or grant.lease_expires_at < current.lease_expires_at
    ):
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    return _replace_record(
        current,
        current_lease_envelope_digest=grant.lease_envelope_digest,
        lease_expected_state_version=grant.state_version,
        state_version=grant.state_version,
        lease_expires_at=grant.lease_expires_at,
        updated_at=timestamp,
    )


def _with_start_grant(
    current: AuditPreflightRunnerJournalRecord,
    grant: AuditPreflightStartGrant,
    timestamp: datetime,
) -> AuditPreflightRunnerJournalRecord:
    if current.start_grant == grant:
        return current
    if (
        current.start_grant is not None
        or current.start_evidence is None
        or grant.job_id != current.job_id
        or grant.capsule_id != current.capsule_id
        or grant.state_version < current.state_version
    ):
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    return _replace_record(
        current,
        start_grant=grant,
        state_version=grant.state_version,
        updated_at=timestamp,
    )


def _with_callback_ack(
    current: AuditPreflightRunnerJournalRecord,
    *,
    field_name: Literal["finish_ack", "stop_ack"],
    ack: AuditPreflightCallbackAck,
    timestamp: datetime,
    requires: str,
) -> AuditPreflightRunnerJournalRecord:
    existing = getattr(current, field_name)
    if existing == ack:
        return current
    if (
        existing is not None
        or getattr(current, requires) is None
        or ack.job_id != current.job_id
        or ack.status not in _TERMINAL_JOB_STATUSES
        or ack.state_version < current.state_version
    ):
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    other = current.stop_ack if field_name == "finish_ack" else current.finish_ack
    if other is not None:
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    return _replace_record(
        current,
        **{
            field_name: ack,
            "state_version": ack.state_version,
            "updated_at": timestamp,
        },
    )


def _with_stop_intent(
    current: AuditPreflightRunnerJournalRecord,
    *,
    reason_code: str,
    timestamp: datetime,
) -> AuditPreflightRunnerJournalRecord:
    if current.stop_intent_at is not None:
        if current.stop_reason_code == reason_code:
            return current
        if reason_code == "audit_preflight_cancel_requested" and current.stop_ack is None:
            failure_code = current.stop_failure_code
            if (
                failure_code is None
                and current.stop_reason_code != "audit_preflight_cancel_requested"
            ):
                failure_code = current.stop_reason_code
            return _replace_record(
                current,
                stop_reason_code=reason_code,
                stop_failure_code=failure_code,
                updated_at=timestamp,
            )
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    if current.finish_ack is not None or current.cleanup_intent_at is not None:
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    return _replace_record(
        current,
        stop_intent_at=timestamp,
        stop_reason_code=reason_code,
        stop_failure_code=(
            None if reason_code == "audit_preflight_cancel_requested" else reason_code
        ),
        updated_at=timestamp,
    )


def _with_cleanup_intent(
    current: AuditPreflightRunnerJournalRecord,
    timestamp: datetime,
) -> AuditPreflightRunnerJournalRecord:
    if current.cleanup_intent_at is not None:
        return current
    if not current.callback_acknowledged:
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    return _replace_record(
        current,
        cleanup_intent_at=timestamp,
        updated_at=timestamp,
    )


def _append_once(
    current: AuditPreflightRunnerJournalRecord,
    *,
    field_name: str,
    value: object,
    timestamp: datetime,
    requires: tuple[str, ...],
    accept_existing: bool,
) -> AuditPreflightRunnerJournalRecord:
    existing = getattr(current, field_name)
    if existing == value:
        return current
    if existing is not None and accept_existing:
        return current
    if existing is not None or any(getattr(current, field) is None for field in requires):
        raise AuditPreflightRunnerJournalConflict(current.job_id)
    return _replace_record(
        current,
        **{field_name: value, "updated_at": timestamp},
    )


def _replace_record(
    current: AuditPreflightRunnerJournalRecord,
    **updates: object,
) -> AuditPreflightRunnerJournalRecord:
    payload = current.model_dump(mode="python")
    payload.update(updates)
    try:
        return AuditPreflightRunnerJournalRecord.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise AuditPreflightRunnerJournalConflict(current.job_id) from exc


def _parse_dispatch(value: str) -> AuditPreflightDispatchEnvelope:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict) or _canonical_json(parsed) != value:
            raise ValueError("dispatch is not canonical")
        return AuditPreflightDispatchEnvelope.model_validate_json(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("Audit Preflight durable dispatch is invalid") from exc


def _dispatch_digest(dispatch: AuditPreflightDispatchEnvelope) -> str:
    return hashlib.sha256(
        AUDIT_PREFLIGHT_RUNNER_DISPATCH_DIGEST_DOMAIN.encode("ascii")
        + b"\0"
        + _canonical_json(dispatch.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def _never_created_proof_digest(record: AuditPreflightRunnerJournalRecord) -> str:
    payload = {
        "backend_id": record.dispatch.owner.backend_id,
        "capsule_id": record.capsule_id,
        "effect_owner_digest": record.effect_owner_digest,
        "image_digest": record.dispatch.owner.image_digest,
        "job_id": record.job_id,
        "lease_envelope_digest": record.current_lease_envelope_digest,
        "policy_digest": record.dispatch.owner.policy_digest,
        "schema_version": AUDIT_PREFLIGHT_RUNNER_NEVER_CREATED_PROOF_DOMAIN,
        "source_node_id": record.dispatch.owner.source_node_id,
    }
    return hashlib.sha256(
        AUDIT_PREFLIGHT_RUNNER_NEVER_CREATED_PROOF_DOMAIN.encode("ascii")
        + b"\0"
        + _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


__all__ = [
    "AUDIT_PREFLIGHT_RUNNER_DISPATCH_DIGEST_DOMAIN",
    "AUDIT_PREFLIGHT_RUNNER_JOURNAL_SCHEMA_VERSION",
    "AUDIT_PREFLIGHT_RUNNER_NEVER_CREATED_PROOF_DOMAIN",
    "AUDIT_PREFLIGHT_RUNNER_RECORD_SCHEMA_VERSION",
    "AuditPreflightCapsuleBackend",
    "AuditPreflightCapsuleReference",
    "AuditPreflightCapsuleStartEvidence",
    "AuditPreflightCapsuleStopEvidence",
    "AuditPreflightRecoveryAction",
    "AuditPreflightRunnerJournal",
    "AuditPreflightRunnerJournalConflict",
    "AuditPreflightRunnerJournalError",
    "AuditPreflightRunnerJournalRecord",
    "AuditPreflightRunnerRecoveryRequired",
    "AuditPreflightTerminalObservation",
    "DurableAuditPreflightCapsuleController",
    "audit_preflight_dispatch_digest",
]
