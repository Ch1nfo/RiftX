"""Lease-based delivery and reconciliation for durable Workflow signals.

These workers are feature-flag independent.  Historical completion, approval,
control, and safety intents must continue converging while Code Audit admission
is disabled.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from riftx.application.errors import RepositoryConflictError
from riftx.application.ports.workflow_signals import WorkflowSignalIntentRepository
from riftx.domain.base import utc_now
from riftx.domain.workflow_signal import (
    WorkflowSignalIntent,
    WorkflowSignalKind,
    WorkflowSignalOwnerKind,
    WorkflowSignalReceiptKind,
    workflow_signal_delivery_receipt_digest,
)

type WorkflowSignalClock = Callable[[], datetime]
type WorkflowSignalBackoff = Callable[[int], timedelta]


@dataclass(frozen=True, slots=True)
class WorkflowSignalTransportReceipt:
    """Transport evidence that echoes the exact owner/protocol identity."""

    owner_kind: WorkflowSignalOwnerKind
    workflow_protocol_version: str
    workflow_id: str
    signal_kind: WorkflowSignalKind
    identity_digest: str
    payload_digest: str
    transport_receipt: str

    def __post_init__(self) -> None:
        if not self.workflow_protocol_version or not self.workflow_id:
            raise ValueError("Workflow signal transport receipt omitted Workflow identity")
        if not self.transport_receipt:
            raise ValueError("Workflow signal transport receipt must be non-empty")


class WorkflowSignalObservationState(StrEnum):
    DELIVERED = "delivered"
    NOT_DELIVERED = "not_delivered"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WorkflowSignalObservation:
    """Authoritative delivery probe result for an outcome-unknown intent."""

    state: WorkflowSignalObservationState
    owner_kind: WorkflowSignalOwnerKind
    workflow_protocol_version: str
    workflow_id: str
    signal_kind: WorkflowSignalKind
    identity_digest: str
    payload_digest: str
    observation_receipt: str

    def __post_init__(self) -> None:
        if not self.observation_receipt:
            raise ValueError("Workflow signal observation receipt must be non-empty")


class WorkflowSignalTransport(Protocol):
    """Send only to the exact protocol and Workflow ID carried by the intent.

    Implementations must never derive a Workflow ID from ``run_id`` and must
    never fall back from a Code Audit protocol to the General Run protocol.
    """

    async def send(self, intent: WorkflowSignalIntent) -> WorkflowSignalTransportReceipt: ...


class WorkflowSignalOutcomeProbe(Protocol):
    """Inspect Workflow history/projection without sending a signal."""

    async def observe(self, intent: WorkflowSignalIntent) -> WorkflowSignalObservation: ...


class WorkflowSignalDeliveryError(RuntimeError):
    """A bounded transport error with a stable non-sensitive code."""

    def __init__(self, error_code: str) -> None:
        _validate_error_code(error_code)
        super().__init__(error_code)
        self.error_code = error_code


class WorkflowSignalDefinitelyNotDelivered(WorkflowSignalDeliveryError):
    """The transport proved that no signal reached the target Workflow."""


class WorkflowSignalTerminallyRejected(WorkflowSignalDeliveryError):
    """The signal was not delivered and can never become valid for this owner."""


class WorkflowSignalOutcomeUnknown(WorkflowSignalDeliveryError):
    """The transport may have delivered the signal before losing its response."""


@dataclass(frozen=True, slots=True)
class WorkflowSignalBatchResult:
    claimed: int = 0
    delivered: int = 0
    observed_delivered: int = 0
    retryable: int = 0
    outcome_unknown: int = 0
    superseded: int = 0
    deferred: int = 0
    conflicts: int = 0


class WorkflowSignalOutboxApplicationService:
    """Stable application edge for creating independently idempotent intents."""

    def __init__(self, repository: WorkflowSignalIntentRepository) -> None:
        self._repository = repository

    async def create(
        self,
        intent: WorkflowSignalIntent,
    ) -> tuple[WorkflowSignalIntent, bool]:
        return await self._repository.create(intent)


class WorkflowSignalDispatcher:
    """Deliver pending intents; uncertain sends are never automatically repeated."""

    def __init__(
        self,
        *,
        repository: WorkflowSignalIntentRepository,
        transport: WorkflowSignalTransport,
        lease_owner: str,
        lease_duration: timedelta = timedelta(seconds=30),
        clock: WorkflowSignalClock = utc_now,
        backoff: WorkflowSignalBackoff | None = None,
    ) -> None:
        _validate_worker(lease_owner, lease_duration)
        self._repository = repository
        self._transport = transport
        self._lease_owner = lease_owner
        self._lease_duration = lease_duration
        self._clock = clock
        self._backoff = backoff or _default_backoff

    async def dispatch_batch(self, *, limit: int = 100) -> WorkflowSignalBatchResult:
        now = self._aware_now()
        await self._repository.recover_expired_delivery_claims(
            now=now,
            next_attempt_at=now + self._backoff(1),
            limit=limit,
        )
        intents = await self._repository.claim_delivery_batch(
            lease_owner=self._lease_owner,
            now=now,
            lease_duration=self._lease_duration,
            limit=limit,
        )
        delivered = retryable = outcome_unknown = superseded = conflicts = 0
        for intent in intents:
            try:
                receipt = await self._transport.send(intent)
            except WorkflowSignalTerminallyRejected as exc:
                transitioned = await self._mark_superseded(intent, exc.error_code)
                superseded += int(transitioned)
                conflicts += int(not transitioned)
                continue
            except WorkflowSignalDefinitelyNotDelivered as exc:
                transitioned = await self._mark_retryable(intent, exc.error_code)
                retryable += int(transitioned)
                conflicts += int(not transitioned)
                continue
            except WorkflowSignalOutcomeUnknown as exc:
                transitioned = await self._mark_outcome_unknown(intent, exc.error_code)
                outcome_unknown += int(transitioned)
                conflicts += int(not transitioned)
                continue
            except Exception:
                # Unknown transport exceptions may occur after Temporal accepted
                # the signal. Fail into reconciliation, never blind redelivery.
                transitioned = await self._mark_outcome_unknown(
                    intent,
                    "transport_outcome_unknown",
                )
                outcome_unknown += int(transitioned)
                conflicts += int(not transitioned)
                continue

            if not _receipt_matches(intent, receipt):
                transitioned = await self._mark_outcome_unknown(
                    intent,
                    "transport_receipt_identity_mismatch",
                )
                outcome_unknown += int(transitioned)
                conflicts += int(not transitioned)
                continue
            completed_at = self._aware_now()
            receipt_digest = workflow_signal_delivery_receipt_digest(
                intent,
                receipt_kind=WorkflowSignalReceiptKind.DELIVERED,
                dispatcher_id=self._lease_owner,
                observed_at=completed_at,
                transport_receipt=receipt.transport_receipt,
            )
            try:
                await self._repository.mark_delivered(
                    intent.id,
                    lease_owner=self._lease_owner,
                    expected_state_version=intent.state_version,
                    receipt_digest=receipt_digest,
                    delivered_at=completed_at,
                )
            except RepositoryConflictError:
                # The expired lease recovery path will move the row to
                # outcome_unknown. A successful send is never blindly retried.
                conflicts += 1
            else:
                delivered += 1
        return WorkflowSignalBatchResult(
            claimed=len(intents),
            delivered=delivered,
            retryable=retryable,
            outcome_unknown=outcome_unknown,
            superseded=superseded,
            conflicts=conflicts,
        )

    async def _mark_retryable(self, intent: WorkflowSignalIntent, error_code: str) -> bool:
        now = self._aware_now()
        try:
            await self._repository.mark_retryable(
                intent.id,
                lease_owner=self._lease_owner,
                expected_state_version=intent.state_version,
                error_code=error_code,
                next_attempt_at=now + self._backoff(intent.attempt),
                updated_at=now,
            )
        except RepositoryConflictError:
            return False
        return True

    async def _mark_outcome_unknown(
        self,
        intent: WorkflowSignalIntent,
        error_code: str,
    ) -> bool:
        now = self._aware_now()
        try:
            await self._repository.mark_outcome_unknown(
                intent.id,
                lease_owner=self._lease_owner,
                expected_state_version=intent.state_version,
                error_code=error_code,
                next_attempt_at=now + self._backoff(intent.attempt),
                updated_at=now,
            )
        except RepositoryConflictError:
            return False
        return True

    async def _mark_superseded(
        self,
        intent: WorkflowSignalIntent,
        error_code: str,
    ) -> bool:
        now = self._aware_now()
        try:
            await self._repository.mark_superseded(
                intent.id,
                lease_owner=self._lease_owner,
                expected_state_version=intent.state_version,
                error_code=error_code,
                updated_at=now,
            )
        except RepositoryConflictError:
            return False
        return True

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise ValueError("Workflow signal clock must return an aware datetime")
        return value


class WorkflowSignalReconciler:
    """Resolve uncertain outcomes before any intent becomes retryable again."""

    def __init__(
        self,
        *,
        repository: WorkflowSignalIntentRepository,
        probe: WorkflowSignalOutcomeProbe,
        lease_owner: str,
        lease_duration: timedelta = timedelta(seconds=30),
        clock: WorkflowSignalClock = utc_now,
        backoff: WorkflowSignalBackoff | None = None,
    ) -> None:
        _validate_worker(lease_owner, lease_duration)
        self._repository = repository
        self._probe = probe
        self._lease_owner = lease_owner
        self._lease_duration = lease_duration
        self._clock = clock
        self._backoff = backoff or _default_backoff

    async def reconcile_batch(self, *, limit: int = 100) -> WorkflowSignalBatchResult:
        now = self._aware_now()
        intents = await self._repository.claim_reconciliation_batch(
            lease_owner=self._lease_owner,
            now=now,
            lease_duration=self._lease_duration,
            limit=limit,
        )
        observed = retryable = deferred = conflicts = 0
        for intent in intents:
            try:
                observation = await self._probe.observe(intent)
            except Exception:
                transitioned = await self._defer(intent, "reconciliation_probe_unavailable")
                deferred += int(transitioned)
                conflicts += int(not transitioned)
                continue
            if not _observation_matches(intent, observation):
                transitioned = await self._defer(
                    intent,
                    "reconciliation_receipt_identity_mismatch",
                )
                deferred += int(transitioned)
                conflicts += int(not transitioned)
                continue
            if observation.state is WorkflowSignalObservationState.DELIVERED:
                observed_at = self._aware_now()
                receipt_digest = workflow_signal_delivery_receipt_digest(
                    intent,
                    receipt_kind=WorkflowSignalReceiptKind.OBSERVED_DELIVERED,
                    dispatcher_id=self._lease_owner,
                    observed_at=observed_at,
                    transport_receipt=observation.observation_receipt,
                )
                try:
                    await self._repository.mark_observed_delivered(
                        intent.id,
                        lease_owner=self._lease_owner,
                        expected_state_version=intent.state_version,
                        receipt_digest=receipt_digest,
                        observed_at=observed_at,
                    )
                except RepositoryConflictError:
                    conflicts += 1
                else:
                    observed += 1
                continue
            if observation.state is WorkflowSignalObservationState.NOT_DELIVERED:
                transitioned = await self._mark_not_delivered(intent)
                retryable += int(transitioned)
                conflicts += int(not transitioned)
                continue
            transitioned = await self._defer(intent, "delivery_outcome_still_unknown")
            deferred += int(transitioned)
            conflicts += int(not transitioned)
        return WorkflowSignalBatchResult(
            claimed=len(intents),
            observed_delivered=observed,
            retryable=retryable,
            deferred=deferred,
            conflicts=conflicts,
        )

    async def _mark_not_delivered(self, intent: WorkflowSignalIntent) -> bool:
        now = self._aware_now()
        try:
            await self._repository.mark_reconciled_not_delivered(
                intent.id,
                lease_owner=self._lease_owner,
                expected_state_version=intent.state_version,
                error_code="reconciled_not_delivered",
                next_attempt_at=now + self._backoff(intent.attempt),
                updated_at=now,
            )
        except RepositoryConflictError:
            return False
        return True

    async def _defer(self, intent: WorkflowSignalIntent, error_code: str) -> bool:
        now = self._aware_now()
        try:
            await self._repository.defer_reconciliation(
                intent.id,
                lease_owner=self._lease_owner,
                expected_state_version=intent.state_version,
                error_code=error_code,
                next_attempt_at=now + self._backoff(intent.attempt),
                updated_at=now,
            )
        except RepositoryConflictError:
            return False
        return True

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise ValueError("Workflow signal clock must return an aware datetime")
        return value


def _receipt_matches(
    intent: WorkflowSignalIntent,
    receipt: WorkflowSignalTransportReceipt,
) -> bool:
    return (
        receipt.owner_kind is intent.owner_kind
        and receipt.workflow_protocol_version == intent.workflow_protocol_version
        and receipt.workflow_id == intent.workflow_id
        and receipt.signal_kind is intent.signal_kind
        and receipt.identity_digest == intent.identity_digest
        and receipt.payload_digest == intent.payload_digest
    )


def _observation_matches(
    intent: WorkflowSignalIntent,
    observation: WorkflowSignalObservation,
) -> bool:
    return (
        observation.owner_kind is intent.owner_kind
        and observation.workflow_protocol_version == intent.workflow_protocol_version
        and observation.workflow_id == intent.workflow_id
        and observation.signal_kind is intent.signal_kind
        and observation.identity_digest == intent.identity_digest
        and observation.payload_digest == intent.payload_digest
    )


def _default_backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(300, 2 ** min(max(attempt, 1), 8)))


def _validate_worker(lease_owner: str, lease_duration: timedelta) -> None:
    if not lease_owner or len(lease_owner) > 255:
        raise ValueError("Workflow signal lease owner must be 1-255 characters")
    if lease_duration <= timedelta(0):
        raise ValueError("Workflow signal lease duration must be positive")


def _validate_error_code(error_code: str) -> None:
    if (
        not error_code
        or len(error_code) > 128
        or not error_code[0].isalpha()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
            for character in error_code
        )
    ):
        raise ValueError("Workflow signal error code is invalid")


__all__ = [
    "WorkflowSignalBatchResult",
    "WorkflowSignalDefinitelyNotDelivered",
    "WorkflowSignalDispatcher",
    "WorkflowSignalObservation",
    "WorkflowSignalObservationState",
    "WorkflowSignalOutcomeProbe",
    "WorkflowSignalOutcomeUnknown",
    "WorkflowSignalOutboxApplicationService",
    "WorkflowSignalReconciler",
    "WorkflowSignalTerminallyRejected",
    "WorkflowSignalTransport",
    "WorkflowSignalTransportReceipt",
]
