from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from riftx.application.services.workflow_signals import (
    WorkflowSignalDefinitelyNotDelivered,
    WorkflowSignalDispatcher,
    WorkflowSignalObservation,
    WorkflowSignalObservationState,
    WorkflowSignalReconciler,
    WorkflowSignalTerminallyRejected,
    WorkflowSignalTransportReceipt,
)
from riftx.domain.workflow_signal import (
    WorkflowSignalDeliveryState,
    WorkflowSignalIntent,
    WorkflowSignalKind,
    WorkflowSignalSourceKind,
)

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _pending() -> WorkflowSignalIntent:
    return WorkflowSignalIntent.general_run(
        run_id="run-1",
        workflow_id="riftx-run-run-1",
        signal_kind=WorkflowSignalKind.EXECUTION_COMPLETED,
        source_event_kind=WorkflowSignalSourceKind.EXECUTION_TERMINAL,
        source_event_id="execution-1",
        source_state_version=2,
        payload={"execution_id": "execution-1"},
        created_at=NOW,
    )


def _claimed() -> WorkflowSignalIntent:
    payload = _pending().model_dump(mode="python")
    payload.update(
        delivery_state=WorkflowSignalDeliveryState.CLAIMED,
        lease_owner="dispatcher-1",
        lease_expires_at=NOW + timedelta(seconds=30),
        attempt=1,
        next_attempt_at=None,
        updated_at=NOW,
        state_version=2,
    )
    return WorkflowSignalIntent.model_validate(payload)


def _reconciliation_claim() -> WorkflowSignalIntent:
    payload = _pending().model_dump(mode="python")
    payload.update(
        delivery_state=WorkflowSignalDeliveryState.OUTCOME_UNKNOWN,
        lease_owner="reconciler-1",
        lease_expires_at=NOW + timedelta(seconds=30),
        attempt=1,
        next_attempt_at=NOW,
        last_error_code="transport_outcome_unknown",
        updated_at=NOW,
        state_version=4,
    )
    return WorkflowSignalIntent.model_validate(payload)


def _repository(*, delivery: list[WorkflowSignalIntent] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        recover_expired_delivery_claims=AsyncMock(return_value=0),
        claim_delivery_batch=AsyncMock(return_value=delivery or []),
        claim_reconciliation_batch=AsyncMock(return_value=[]),
        mark_delivered=AsyncMock(),
        mark_observed_delivered=AsyncMock(),
        mark_retryable=AsyncMock(),
        mark_superseded=AsyncMock(),
        mark_outcome_unknown=AsyncMock(),
        mark_reconciled_not_delivered=AsyncMock(),
        defer_reconciliation=AsyncMock(),
    )


def _transport_receipt(intent: WorkflowSignalIntent) -> WorkflowSignalTransportReceipt:
    return WorkflowSignalTransportReceipt(
        owner_kind=intent.owner_kind,
        workflow_protocol_version=intent.workflow_protocol_version,
        workflow_id=intent.workflow_id,
        signal_kind=intent.signal_kind,
        identity_digest=intent.identity_digest,
        payload_digest=intent.payload_digest,
        transport_receipt="temporal-returned:execution-1",
    )


@pytest.mark.asyncio
async def test_dispatcher_marks_exact_transport_receipt_delivered() -> None:
    intent = _claimed()
    repository = _repository(delivery=[intent])
    transport = SimpleNamespace(send=AsyncMock(return_value=_transport_receipt(intent)))
    dispatcher = WorkflowSignalDispatcher(
        repository=repository,
        transport=transport,
        lease_owner="dispatcher-1",
        clock=lambda: NOW,
    )

    result = await dispatcher.dispatch_batch()

    assert result.claimed == result.delivered == 1
    assert result.conflicts == 0
    repository.mark_delivered.assert_awaited_once()
    kwargs = repository.mark_delivered.await_args.kwargs
    assert kwargs["expected_state_version"] == 2
    assert len(kwargs["receipt_digest"]) == 64


@pytest.mark.asyncio
async def test_definitely_not_delivered_becomes_retryable() -> None:
    intent = _claimed()
    repository = _repository(delivery=[intent])
    transport = SimpleNamespace(
        send=AsyncMock(side_effect=WorkflowSignalDefinitelyNotDelivered("temporal_unavailable"))
    )
    dispatcher = WorkflowSignalDispatcher(
        repository=repository,
        transport=transport,
        lease_owner="dispatcher-1",
        clock=lambda: NOW,
    )

    result = await dispatcher.dispatch_batch()

    assert result.retryable == 1
    repository.mark_retryable.assert_awaited_once()
    repository.mark_outcome_unknown.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_rejection_becomes_superseded_without_retry() -> None:
    intent = _claimed()
    repository = _repository(delivery=[intent])
    transport = SimpleNamespace(
        send=AsyncMock(
            side_effect=WorkflowSignalTerminallyRejected("run_owner_mismatch")
        )
    )
    dispatcher = WorkflowSignalDispatcher(
        repository=repository,
        transport=transport,
        lease_owner="dispatcher-1",
        clock=lambda: NOW,
    )

    result = await dispatcher.dispatch_batch()

    assert result.superseded == 1
    repository.mark_superseded.assert_awaited_once_with(
        intent.id,
        lease_owner="dispatcher-1",
        expected_state_version=2,
        error_code="run_owner_mismatch",
        updated_at=NOW,
    )
    repository.mark_retryable.assert_not_awaited()
    repository.mark_outcome_unknown.assert_not_awaited()


@pytest.mark.asyncio
async def test_unclassified_transport_failure_becomes_outcome_unknown() -> None:
    intent = _claimed()
    repository = _repository(delivery=[intent])
    transport = SimpleNamespace(send=AsyncMock(side_effect=TimeoutError("response lost")))
    dispatcher = WorkflowSignalDispatcher(
        repository=repository,
        transport=transport,
        lease_owner="dispatcher-1",
        clock=lambda: NOW,
    )

    result = await dispatcher.dispatch_batch()

    assert result.outcome_unknown == 1
    repository.mark_outcome_unknown.assert_awaited_once_with(
        intent.id,
        lease_owner="dispatcher-1",
        expected_state_version=2,
        error_code="transport_outcome_unknown",
        next_attempt_at=NOW + timedelta(seconds=2),
        updated_at=NOW,
    )
    repository.mark_retryable.assert_not_awaited()


@pytest.mark.asyncio
async def test_mismatched_receipt_never_marks_signal_delivered() -> None:
    intent = _claimed()
    receipt = _transport_receipt(intent)
    receipt = WorkflowSignalTransportReceipt(
        owner_kind=receipt.owner_kind,
        workflow_protocol_version=receipt.workflow_protocol_version,
        workflow_id="riftx-run-other",
        signal_kind=receipt.signal_kind,
        identity_digest=receipt.identity_digest,
        payload_digest=receipt.payload_digest,
        transport_receipt=receipt.transport_receipt,
    )
    repository = _repository(delivery=[intent])
    dispatcher = WorkflowSignalDispatcher(
        repository=repository,
        transport=SimpleNamespace(send=AsyncMock(return_value=receipt)),
        lease_owner="dispatcher-1",
        clock=lambda: NOW,
    )

    result = await dispatcher.dispatch_batch()

    assert result.outcome_unknown == 1
    repository.mark_delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_observes_delivery_without_resending() -> None:
    intent = _reconciliation_claim()
    repository = _repository()
    repository.claim_reconciliation_batch.return_value = [intent]
    observation = WorkflowSignalObservation(
        state=WorkflowSignalObservationState.DELIVERED,
        owner_kind=intent.owner_kind,
        workflow_protocol_version=intent.workflow_protocol_version,
        workflow_id=intent.workflow_id,
        signal_kind=intent.signal_kind,
        identity_digest=intent.identity_digest,
        payload_digest=intent.payload_digest,
        observation_receipt="temporal-history:event-42",
    )
    reconciler = WorkflowSignalReconciler(
        repository=repository,
        probe=SimpleNamespace(observe=AsyncMock(return_value=observation)),
        lease_owner="reconciler-1",
        clock=lambda: NOW,
    )

    result = await reconciler.reconcile_batch()

    assert result.observed_delivered == 1
    repository.mark_observed_delivered.assert_awaited_once()
    repository.mark_reconciled_not_delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_only_requeues_after_authoritative_not_delivered() -> None:
    intent = _reconciliation_claim()
    repository = _repository()
    repository.claim_reconciliation_batch.return_value = [intent]
    observation = WorkflowSignalObservation(
        state=WorkflowSignalObservationState.NOT_DELIVERED,
        owner_kind=intent.owner_kind,
        workflow_protocol_version=intent.workflow_protocol_version,
        workflow_id=intent.workflow_id,
        signal_kind=intent.signal_kind,
        identity_digest=intent.identity_digest,
        payload_digest=intent.payload_digest,
        observation_receipt="temporal-history:not-found",
    )
    reconciler = WorkflowSignalReconciler(
        repository=repository,
        probe=SimpleNamespace(observe=AsyncMock(return_value=observation)),
        lease_owner="reconciler-1",
        clock=lambda: NOW,
    )

    result = await reconciler.reconcile_batch()

    assert result.retryable == 1
    repository.mark_reconciled_not_delivered.assert_awaited_once()
    repository.mark_observed_delivered.assert_not_awaited()
