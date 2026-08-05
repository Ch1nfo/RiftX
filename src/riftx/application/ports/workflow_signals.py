"""Application ports for the durable Workflow signal outbox."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Protocol

from riftx.domain.workflow_signal import WorkflowSignalIntent


class WorkflowSignalSourceValidator(Protocol):
    """Revalidate the immutable business fact that produced an intent."""

    async def validate_for_delivery(self, intent: WorkflowSignalIntent) -> None: ...

    def guard_for_delivery(
        self,
        intent: WorkflowSignalIntent,
    ) -> AbstractAsyncContextManager[None]:
        """Hold the authoritative owner/source fence through the remote call."""

        ...


class WorkflowSignalIntentRepository(WorkflowSignalSourceValidator, Protocol):
    """CAS/lease persistence required by dispatch and reconciliation workers."""

    async def create(
        self,
        intent: WorkflowSignalIntent,
    ) -> tuple[WorkflowSignalIntent, bool]: ...

    async def get(self, intent_id: str) -> WorkflowSignalIntent | None: ...

    async def recover_expired_delivery_claims(
        self,
        *,
        now: datetime,
        next_attempt_at: datetime,
        limit: int = 100,
    ) -> int: ...

    async def claim_delivery_batch(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int = 100,
    ) -> Sequence[WorkflowSignalIntent]: ...

    async def claim_reconciliation_batch(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int = 100,
    ) -> Sequence[WorkflowSignalIntent]: ...

    async def mark_delivered(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        receipt_digest: str,
        delivered_at: datetime,
    ) -> WorkflowSignalIntent: ...

    async def mark_observed_delivered(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        receipt_digest: str,
        observed_at: datetime,
    ) -> WorkflowSignalIntent: ...

    async def mark_retryable(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        next_attempt_at: datetime,
        updated_at: datetime,
    ) -> WorkflowSignalIntent: ...

    async def mark_superseded(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        updated_at: datetime,
    ) -> WorkflowSignalIntent: ...

    async def mark_outcome_unknown(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        next_attempt_at: datetime,
        updated_at: datetime,
    ) -> WorkflowSignalIntent: ...

    async def mark_reconciled_not_delivered(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        next_attempt_at: datetime,
        updated_at: datetime,
    ) -> WorkflowSignalIntent: ...

    async def defer_reconciliation(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        next_attempt_at: datetime,
        updated_at: datetime,
    ) -> WorkflowSignalIntent: ...


__all__ = [
    "WorkflowSignalIntentRepository",
    "WorkflowSignalSourceValidator",
]
