"""SQLAlchemy persistence for owner-bound durable Workflow signal intents."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Never

from pydantic import ValidationError
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from riftx.application.errors import (
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
)
from riftx.domain.audit import AuditLifecycleStatus
from riftx.domain.enums import ApprovalStatus, ExecutionStatus, RunKind, RunStatus
from riftx.domain.workflow_signal import (
    CODE_AUDIT_WORKFLOW_PROTOCOL_V1,
    GENERAL_RUN_WORKFLOW_PROTOCOL_V1,
    WORKFLOW_SIGNAL_INTENT_SCHEMA_VERSION,
    WorkflowSignalDeliveryState,
    WorkflowSignalIntent,
    WorkflowSignalKind,
    WorkflowSignalOwnerKind,
    WorkflowSignalSourceKind,
    canonical_workflow_signal_payload,
)

from .orm import (
    ApprovalRecord,
    AuditScanRecord,
    Base,
    ExecutionRecord,
    RunEventRecord,
    RunRecord,
)
from .transactions import serialized_write
from .types import UTCDateTime

SessionFactory = async_sessionmaker[AsyncSession]


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def _optional_lower_hex_digest_check(column: str) -> str:
    return f"{column} IS NULL OR ({_lower_hex_digest_check(column)})"


class WorkflowSignalIntentRecord(Base):
    __tablename__ = "workflow_signal_intents"
    __table_args__ = (
        CheckConstraint(
            f"schema_version = '{WORKFLOW_SIGNAL_INTENT_SCHEMA_VERSION}'",
            name="ck_workflow_signal_intents_schema",
        ),
        CheckConstraint(
            "owner_kind IN ('general_run', 'code_audit')",
            name="ck_workflow_signal_intents_owner_kind",
        ),
        CheckConstraint(
            "run_kind IN ('general', 'code_audit')",
            name="ck_workflow_signal_intents_run_kind",
        ),
        CheckConstraint(
            "(owner_kind = 'general_run' AND run_kind = 'general' "
            "AND audit_id IS NULL AND owner_identity = 'general_run:' || run_id "
            f"AND workflow_protocol_version = '{GENERAL_RUN_WORKFLOW_PROTOCOL_V1}' "
            "AND workflow_id NOT LIKE 'riftx-code-audit-%') OR "
            "(owner_kind = 'code_audit' AND run_kind = 'code_audit' "
            "AND audit_id IS NOT NULL AND owner_identity = 'code_audit:' || audit_id "
            f"AND workflow_protocol_version = '{CODE_AUDIT_WORKFLOW_PROTOCOL_V1}' "
            "AND workflow_id = 'riftx-code-audit-' || audit_id)",
            name="ck_workflow_signal_intents_owner_binding",
        ),
        CheckConstraint(
            "signal_kind IN ('pause', 'resume', 'cancel', 'approve', 'reject', "
            "'execution_completed', 'safety_reconcile')",
            name="ck_workflow_signal_intents_signal_kind",
        ),
        CheckConstraint(
            "source_event_kind IN ('control_intent', 'approval_decision', "
            "'execution_terminal', 'safety_reconciliation')",
            name="ck_workflow_signal_intents_source_kind",
        ),
        CheckConstraint(
            "(source_event_kind = 'control_intent' "
            "AND signal_kind IN ('pause', 'resume', 'cancel')) OR "
            "(source_event_kind = 'approval_decision' "
            "AND signal_kind IN ('approve', 'reject')) OR "
            "(source_event_kind = 'execution_terminal' "
            "AND signal_kind = 'execution_completed') OR "
            "(source_event_kind = 'safety_reconciliation' "
            "AND signal_kind = 'safety_reconcile')",
            name="ck_workflow_signal_intents_source_signal",
        ),
        CheckConstraint(
            "delivery_state IN ('pending', 'claimed', 'delivered', "
            "'observed_delivered', 'retryable', 'outcome_unknown', 'superseded')",
            name="ck_workflow_signal_intents_delivery_state",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_workflow_signal_intents_lease_pair",
        ),
        CheckConstraint(
            "(delivery_state = 'claimed' AND lease_owner IS NOT NULL) OR "
            "(delivery_state = 'outcome_unknown') OR "
            "(delivery_state NOT IN ('claimed', 'outcome_unknown') "
            "AND lease_owner IS NULL)",
            name="ck_workflow_signal_intents_lease_state",
        ),
        CheckConstraint(
            "(delivery_state IN ('delivered', 'observed_delivered') "
            "AND delivery_receipt_digest IS NOT NULL AND delivered_at IS NOT NULL "
            "AND next_attempt_at IS NULL AND last_error_code IS NULL) OR "
            "(delivery_state NOT IN ('delivered', 'observed_delivered') "
            "AND delivery_receipt_digest IS NULL AND delivered_at IS NULL)",
            name="ck_workflow_signal_intents_receipt_state",
        ),
        CheckConstraint(
            "(delivery_state IN ('pending', 'retryable', 'outcome_unknown') "
            "AND next_attempt_at IS NOT NULL) OR "
            "(delivery_state NOT IN ('pending', 'retryable', 'outcome_unknown') "
            "AND next_attempt_at IS NULL)",
            name="ck_workflow_signal_intents_schedule_state",
        ),
        CheckConstraint(
            "(delivery_state = 'pending' AND attempt = 0) OR "
            "(delivery_state <> 'pending' AND attempt >= 1)",
            name="ck_workflow_signal_intents_attempt_state",
        ),
        CheckConstraint(
            "delivery_state NOT IN ('retryable', 'outcome_unknown', 'superseded') "
            "OR last_error_code IS NOT NULL",
            name="ck_workflow_signal_intents_error_state",
        ),
        CheckConstraint(
            _lower_hex_digest_check("identity_digest"),
            name="ck_workflow_signal_intents_identity_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("payload_digest"),
            name="ck_workflow_signal_intents_payload_digest",
        ),
        CheckConstraint(
            _optional_lower_hex_digest_check("delivery_receipt_digest"),
            name="ck_workflow_signal_intents_receipt_digest",
        ),
        CheckConstraint(
            "source_state_version >= 1 AND state_version >= 1 AND attempt >= 0",
            name="ck_workflow_signal_intents_versions",
        ),
        CheckConstraint(
            "updated_at >= created_at AND "
            "(delivered_at IS NULL OR delivered_at >= created_at) AND "
            "(lease_expires_at IS NULL OR lease_expires_at > updated_at)",
            name="ck_workflow_signal_intents_timestamp_order",
        ),
        UniqueConstraint(
            "identity_digest",
            name="uq_workflow_signal_intents_identity_digest",
        ),
        UniqueConstraint(
            "owner_identity",
            "workflow_protocol_version",
            "workflow_id",
            "signal_kind",
            "source_event_kind",
            "source_event_id",
            "source_state_version",
            name="uq_workflow_signal_intents_source_identity",
        ),
        Index(
            "ix_workflow_signal_intents_delivery_schedule",
            "delivery_state",
            "next_attempt_at",
            "created_at",
            "id",
        ),
        Index(
            "ix_workflow_signal_intents_lease",
            "delivery_state",
            "lease_expires_at",
            "id",
        ),
        Index(
            "ix_workflow_signal_intents_run_owner",
            "run_id",
            "owner_kind",
            "created_at",
            "id",
        ),
        Index(
            "ix_workflow_signal_intents_audit_owner",
            "audit_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    audit_id: Mapped[str | None] = mapped_column(
        ForeignKey("audit_scans.id", ondelete="RESTRICT")
    )
    workflow_protocol_version: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    signal_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    delivery_receipt_digest: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SQLAlchemyWorkflowSignalIntentRepository:
    """Portable CAS implementation shared by Control Plane and Worker loops."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        intent: WorkflowSignalIntent,
    ) -> tuple[WorkflowSignalIntent, bool]:
        try:
            async with self._session_factory() as session, session.begin():
                return await self.create_in_session(session, intent)
        except IntegrityError as exc:
            # A concurrent creator may win after the pre-insert lookup. The
            # failed transaction is closed before reading the winning row.
            async with self._session_factory() as session:
                existing = await _find_creation_identity(session, intent)
            if existing is not None:
                persisted = _intent_from_record(existing)
                if _same_creation(persisted, intent):
                    return persisted, False
                raise RepositoryConflictError(
                    "Workflow signal source identity is bound to different immutable facts"
                ) from exc
            raise RepositoryConflictError(
                "Workflow signal intent conflicts with an existing durable identity"
            ) from exc

    async def create_in_session(
        self,
        session: AsyncSession,
        intent: WorkflowSignalIntent,
    ) -> tuple[WorkflowSignalIntent, bool]:
        """Stage an intent in an existing business transaction.

        Approval/Execution UoWs should call this method before committing their
        terminal state so the state transition and signal intent are atomic.
        """

        await _require_exact_owner_binding(session, intent)
        await _require_exact_source_binding(session, intent)
        existing = await _find_creation_identity(session, intent)
        if existing is not None:
            persisted = _intent_from_record(existing)
            if _same_creation(persisted, intent):
                return persisted, False
            raise RepositoryConflictError(
                "Workflow signal source identity is bound to different immutable facts"
            )
        session.add(_intent_to_record(intent))
        # Do not hide this flush behind a SQLite SAVEPOINT: create_in_session
        # must participate in the caller's outer business transaction. A
        # concurrent uniqueness failure invalidates that transaction and the
        # caller retries the whole UoW.
        await session.flush()
        return intent, True

    async def validate_for_delivery(self, intent: WorkflowSignalIntent) -> None:
        """Re-read the exact owner and child fact before any Workflow call."""

        async with self._session_factory() as session, session.begin():
            await _require_exact_owner_binding(session, intent)
            await _require_exact_source_binding(session, intent)

    @asynccontextmanager
    async def guard_for_delivery(
        self,
        intent: WorkflowSignalIntent,
    ) -> AsyncIterator[None]:
        """Linearize owner validation, Workflow send, and competing fences.

        PostgreSQL holds the exact Audit/Run/source rows with ``FOR UPDATE``;
        SQLite uses ``BEGIN IMMEDIATE`` because it ignores row-lock clauses.
        The guard intentionally spans the bounded transport RPC so a cancel
        CAS cannot commit after resume validation but before resume dispatch.
        """

        async with serialized_write(self._session_factory) as session:
            await _require_exact_owner_binding(session, intent)
            await _require_exact_source_binding(session, intent)
            yield

    async def get(self, intent_id: str) -> WorkflowSignalIntent | None:
        async with self._session_factory() as session:
            record = await session.get(WorkflowSignalIntentRecord, intent_id)
        return _intent_from_record(record) if record is not None else None

    async def recover_expired_delivery_claims(
        self,
        *,
        now: datetime,
        next_attempt_at: datetime,
        limit: int = 100,
    ) -> int:
        _validate_batch(limit)
        _validate_schedule(now, next_attempt_at)
        recovered = 0
        async with self._session_factory() as session, session.begin():
            ids = list(
                await session.scalars(
                    select(WorkflowSignalIntentRecord.id)
                    .where(
                        WorkflowSignalIntentRecord.delivery_state
                        == WorkflowSignalDeliveryState.CLAIMED.value,
                        WorkflowSignalIntentRecord.lease_expires_at <= now,
                    )
                    .order_by(
                        WorkflowSignalIntentRecord.lease_expires_at,
                        WorkflowSignalIntentRecord.id,
                    )
                    .limit(limit)
                )
            )
            for intent_id in ids:
                result = await session.execute(
                    update(WorkflowSignalIntentRecord)
                    .where(
                        WorkflowSignalIntentRecord.id == intent_id,
                        WorkflowSignalIntentRecord.delivery_state
                        == WorkflowSignalDeliveryState.CLAIMED.value,
                        WorkflowSignalIntentRecord.lease_expires_at <= now,
                    )
                    .values(
                        delivery_state=WorkflowSignalDeliveryState.OUTCOME_UNKNOWN.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        next_attempt_at=next_attempt_at,
                        last_error_code="delivery_lease_expired",
                        updated_at=now,
                        state_version=WorkflowSignalIntentRecord.state_version + 1,
                    )
                )
                recovered += int(result.rowcount == 1)  # type: ignore[attr-defined]
        return recovered

    async def claim_delivery_batch(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int = 100,
    ) -> Sequence[WorkflowSignalIntent]:
        _validate_claim(lease_owner, lease_duration, limit)
        lease_expires_at = now + lease_duration
        claimed: list[WorkflowSignalIntent] = []
        async with self._session_factory() as session, session.begin():
            ids = list(
                await session.scalars(
                    select(WorkflowSignalIntentRecord.id)
                    .where(
                        WorkflowSignalIntentRecord.delivery_state.in_(
                            (
                                WorkflowSignalDeliveryState.PENDING.value,
                                WorkflowSignalDeliveryState.RETRYABLE.value,
                            )
                        ),
                        WorkflowSignalIntentRecord.next_attempt_at <= now,
                        WorkflowSignalIntentRecord.lease_owner.is_(None),
                    )
                    .order_by(
                        WorkflowSignalIntentRecord.next_attempt_at,
                        WorkflowSignalIntentRecord.created_at,
                        WorkflowSignalIntentRecord.id,
                    )
                    .limit(limit)
                )
            )
            for intent_id in ids:
                result = await session.execute(
                    update(WorkflowSignalIntentRecord)
                    .where(
                        WorkflowSignalIntentRecord.id == intent_id,
                        WorkflowSignalIntentRecord.delivery_state.in_(
                            (
                                WorkflowSignalDeliveryState.PENDING.value,
                                WorkflowSignalDeliveryState.RETRYABLE.value,
                            )
                        ),
                        WorkflowSignalIntentRecord.next_attempt_at <= now,
                        WorkflowSignalIntentRecord.lease_owner.is_(None),
                    )
                    .values(
                        delivery_state=WorkflowSignalDeliveryState.CLAIMED.value,
                        lease_owner=lease_owner,
                        lease_expires_at=lease_expires_at,
                        attempt=WorkflowSignalIntentRecord.attempt + 1,
                        next_attempt_at=None,
                        last_error_code=None,
                        updated_at=now,
                        state_version=WorkflowSignalIntentRecord.state_version + 1,
                    )
                )
                if result.rowcount != 1:  # type: ignore[attr-defined]
                    continue
                record = await session.get(WorkflowSignalIntentRecord, intent_id)
                if record is None:
                    raise EntityNotFoundError("WorkflowSignalIntent", intent_id)
                claimed.append(_intent_from_record(record))
        return claimed

    async def claim_reconciliation_batch(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int = 100,
    ) -> Sequence[WorkflowSignalIntent]:
        _validate_claim(lease_owner, lease_duration, limit)
        lease_expires_at = now + lease_duration
        claimed: list[WorkflowSignalIntent] = []
        async with self._session_factory() as session, session.begin():
            claimable = or_(
                WorkflowSignalIntentRecord.lease_owner.is_(None),
                WorkflowSignalIntentRecord.lease_expires_at <= now,
            )
            ids = list(
                await session.scalars(
                    select(WorkflowSignalIntentRecord.id)
                    .where(
                        WorkflowSignalIntentRecord.delivery_state
                        == WorkflowSignalDeliveryState.OUTCOME_UNKNOWN.value,
                        WorkflowSignalIntentRecord.next_attempt_at <= now,
                        claimable,
                    )
                    .order_by(
                        WorkflowSignalIntentRecord.next_attempt_at,
                        WorkflowSignalIntentRecord.created_at,
                        WorkflowSignalIntentRecord.id,
                    )
                    .limit(limit)
                )
            )
            for intent_id in ids:
                result = await session.execute(
                    update(WorkflowSignalIntentRecord)
                    .where(
                        WorkflowSignalIntentRecord.id == intent_id,
                        WorkflowSignalIntentRecord.delivery_state
                        == WorkflowSignalDeliveryState.OUTCOME_UNKNOWN.value,
                        WorkflowSignalIntentRecord.next_attempt_at <= now,
                        or_(
                            WorkflowSignalIntentRecord.lease_owner.is_(None),
                            WorkflowSignalIntentRecord.lease_expires_at <= now,
                        ),
                    )
                    .values(
                        lease_owner=lease_owner,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                        state_version=WorkflowSignalIntentRecord.state_version + 1,
                    )
                )
                if result.rowcount != 1:  # type: ignore[attr-defined]
                    continue
                record = await session.get(WorkflowSignalIntentRecord, intent_id)
                if record is None:
                    raise EntityNotFoundError("WorkflowSignalIntent", intent_id)
                claimed.append(_intent_from_record(record))
        return claimed

    async def mark_delivered(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        receipt_digest: str,
        delivered_at: datetime,
    ) -> WorkflowSignalIntent:
        return await self._complete_delivery(
            intent_id,
            lease_owner=lease_owner,
            expected_state_version=expected_state_version,
            receipt_digest=receipt_digest,
            delivered_at=delivered_at,
            state=WorkflowSignalDeliveryState.DELIVERED,
        )

    async def mark_observed_delivered(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        receipt_digest: str,
        observed_at: datetime,
    ) -> WorkflowSignalIntent:
        return await self._complete_delivery(
            intent_id,
            lease_owner=lease_owner,
            expected_state_version=expected_state_version,
            receipt_digest=receipt_digest,
            delivered_at=observed_at,
            state=WorkflowSignalDeliveryState.OBSERVED_DELIVERED,
        )

    async def _complete_delivery(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        receipt_digest: str,
        delivered_at: datetime,
        state: WorkflowSignalDeliveryState,
    ) -> WorkflowSignalIntent:
        source_state = (
            WorkflowSignalDeliveryState.CLAIMED
            if state is WorkflowSignalDeliveryState.DELIVERED
            else WorkflowSignalDeliveryState.OUTCOME_UNKNOWN
        )
        return await self._transition(
            intent_id,
            source_state=source_state,
            lease_owner=lease_owner,
            expected_state_version=expected_state_version,
            now=delivered_at,
            values={
                "delivery_state": state.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": None,
                "delivery_receipt_digest": receipt_digest,
                "last_error_code": None,
                "delivered_at": delivered_at,
            },
        )

    async def mark_retryable(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        next_attempt_at: datetime,
        updated_at: datetime,
    ) -> WorkflowSignalIntent:
        _validate_schedule(updated_at, next_attempt_at)
        return await self._transition(
            intent_id,
            source_state=WorkflowSignalDeliveryState.CLAIMED,
            lease_owner=lease_owner,
            expected_state_version=expected_state_version,
            now=updated_at,
            values={
                "delivery_state": WorkflowSignalDeliveryState.RETRYABLE.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": next_attempt_at,
                "last_error_code": error_code,
            },
        )

    async def mark_outcome_unknown(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        next_attempt_at: datetime,
        updated_at: datetime,
    ) -> WorkflowSignalIntent:
        _validate_schedule(updated_at, next_attempt_at)
        return await self._transition(
            intent_id,
            source_state=WorkflowSignalDeliveryState.CLAIMED,
            lease_owner=lease_owner,
            expected_state_version=expected_state_version,
            now=updated_at,
            values={
                "delivery_state": WorkflowSignalDeliveryState.OUTCOME_UNKNOWN.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": next_attempt_at,
                "last_error_code": error_code,
            },
        )

    async def mark_superseded(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        updated_at: datetime,
    ) -> WorkflowSignalIntent:
        return await self._transition(
            intent_id,
            source_state=WorkflowSignalDeliveryState.CLAIMED,
            lease_owner=lease_owner,
            expected_state_version=expected_state_version,
            now=updated_at,
            values={
                "delivery_state": WorkflowSignalDeliveryState.SUPERSEDED.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": None,
                "last_error_code": error_code,
            },
        )

    async def mark_reconciled_not_delivered(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        next_attempt_at: datetime,
        updated_at: datetime,
    ) -> WorkflowSignalIntent:
        _validate_schedule(updated_at, next_attempt_at)
        return await self._transition(
            intent_id,
            source_state=WorkflowSignalDeliveryState.OUTCOME_UNKNOWN,
            lease_owner=lease_owner,
            expected_state_version=expected_state_version,
            now=updated_at,
            values={
                "delivery_state": WorkflowSignalDeliveryState.RETRYABLE.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": next_attempt_at,
                "last_error_code": error_code,
            },
        )

    async def defer_reconciliation(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        expected_state_version: int,
        error_code: str,
        next_attempt_at: datetime,
        updated_at: datetime,
    ) -> WorkflowSignalIntent:
        _validate_schedule(updated_at, next_attempt_at)
        return await self._transition(
            intent_id,
            source_state=WorkflowSignalDeliveryState.OUTCOME_UNKNOWN,
            lease_owner=lease_owner,
            expected_state_version=expected_state_version,
            now=updated_at,
            values={
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": next_attempt_at,
                "last_error_code": error_code,
            },
        )

    async def _transition(
        self,
        intent_id: str,
        *,
        source_state: WorkflowSignalDeliveryState,
        lease_owner: str,
        expected_state_version: int,
        now: datetime,
        values: dict[str, object],
    ) -> WorkflowSignalIntent:
        if not lease_owner:
            raise ValueError("Workflow signal lease owner must be non-empty")
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(WorkflowSignalIntentRecord)
                .where(
                    WorkflowSignalIntentRecord.id == intent_id,
                    WorkflowSignalIntentRecord.delivery_state == source_state.value,
                    WorkflowSignalIntentRecord.lease_owner == lease_owner,
                    WorkflowSignalIntentRecord.lease_expires_at > now,
                    WorkflowSignalIntentRecord.state_version == expected_state_version,
                )
                .values(
                    **values,
                    updated_at=now,
                    state_version=WorkflowSignalIntentRecord.state_version + 1,
                )
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                _transition_conflict(intent_id)
            record = await session.get(WorkflowSignalIntentRecord, intent_id)
            if record is None:
                raise EntityNotFoundError("WorkflowSignalIntent", intent_id)
            return _intent_from_record(record)


async def _require_exact_owner_binding(
    session: AsyncSession,
    intent: WorkflowSignalIntent,
) -> None:
    audit: AuditScanRecord | None = None
    if intent.owner_kind is WorkflowSignalOwnerKind.CODE_AUDIT:
        if intent.audit_id is None:
            raise RepositoryConflictError(
                "Code Audit Workflow signal intent has no Audit owner"
            )
        # Audit control writers use Audit -> Run lock order. Keep delivery on
        # the same order so the cross-RPC guard cannot deadlock a cancel CAS.
        audit = await session.get(
            AuditScanRecord,
            intent.audit_id,
            with_for_update=True,
        )
    run = await session.get(RunRecord, intent.run_id, with_for_update=True)
    if run is None:
        raise EntityNotFoundError("Run", intent.run_id)
    if (
        run.kind != intent.run_kind.value
        or run.temporal_workflow_id != intent.workflow_id
    ):
        raise RepositoryConflictError(
            "Workflow signal intent does not match its authoritative Run owner"
        )
    if intent.owner_kind is WorkflowSignalOwnerKind.GENERAL_RUN:
        if run.kind != RunKind.GENERAL.value or intent.audit_id is not None:
            raise RepositoryConflictError(
                "General Workflow signal intent has a non-General owner"
            )
        return
    assert intent.audit_id is not None
    if (
        audit is None
        or audit.run_id != intent.run_id
        or audit.run_kind != RunKind.CODE_AUDIT.value
        or audit.temporal_workflow_id != intent.workflow_id
        or intent.workflow_id != f"riftx-code-audit-{intent.audit_id}"
    ):
        raise RepositoryConflictError(
            "Code Audit Workflow signal intent does not match its Audit owner"
        )


_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.COMPLETED.value,
        ExecutionStatus.EXITED.value,
        ExecutionStatus.FAILED.value,
        ExecutionStatus.CANCELLED.value,
        ExecutionStatus.HARD_TIMEOUT.value,
        ExecutionStatus.LOST.value,
    }
)


async def _require_exact_source_binding(
    session: AsyncSession,
    intent: WorkflowSignalIntent,
) -> None:
    """Prove the immutable child/source fact without trusting intent payloads."""

    if intent.source_event_kind is WorkflowSignalSourceKind.CONTROL_INTENT:
        await _require_exact_audit_control_source(session, intent)
        return

    if intent.source_event_kind is WorkflowSignalSourceKind.APPROVAL_DECISION:
        approval_id = _exact_payload_identifier(intent, "approval_id")
        approval = await session.get(ApprovalRecord, approval_id, with_for_update=True)
        if approval is None:
            raise EntityNotFoundError("Approval", approval_id)
        expected_status = (
            ApprovalStatus.APPROVED.value
            if intent.signal_kind is WorkflowSignalKind.APPROVE
            else ApprovalStatus.REJECTED.value
        )
        expected_event_type = (
            "tool.approved"
            if intent.signal_kind is WorkflowSignalKind.APPROVE
            else "tool.rejected"
        )
        source_event = await session.get(
            RunEventRecord,
            intent.source_event_id,
            with_for_update=True,
        )
        if source_event is None:
            raise EntityNotFoundError("RunEvent", intent.source_event_id)
        event_approval_id = source_event.payload_json.get("approval_id")
        if (
            intent.source_state_version != 1
            or approval.run_id != intent.run_id
            or approval.status != expected_status
            or approval.decided_at is None
            or source_event.run_id != intent.run_id
            or source_event.event_type != expected_event_type
            or event_approval_id != approval_id
        ):
            raise RepositoryConflictError(
                "Workflow signal intent does not match its authoritative Approval source"
            )
        return

    if intent.source_event_kind is WorkflowSignalSourceKind.EXECUTION_TERMINAL:
        execution_id = _exact_payload_identifier(intent, "execution_id")
        # General terminal writers stage the intent atomically while holding
        # Execution -> Run locks.  The delivery guard already holds the Run
        # owner fence across the transport RPC, so taking the Execution lock
        # here would invert that order and can deadlock on PostgreSQL.
        #
        # A consistent read is sufficient: a visible intent proves an earlier
        # terminal transition committed with it, later Execution transitions
        # are monotonic (FAILED/LOST may only converge to CANCELLED), and the
        # admission owner fields checked below are immutable.  Keep this read
        # non-locking unless the writer lock order is changed at the same time.
        execution = await session.get(ExecutionRecord, execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        if (
            intent.source_state_version != 1
            or intent.source_event_id != execution_id
            or execution.run_id != intent.run_id
            or execution.audit_id is not None
            or execution.plan_digest is not None
            or execution.status not in _TERMINAL_EXECUTION_STATUSES
        ):
            raise RepositoryConflictError(
                "Workflow signal intent does not match its authoritative Execution source"
            )
        return

    # A safety reconciliation signal needs its own durable, owner-bound source
    # fact before it can be admitted.  AUD-106 deliberately defines no such
    # producer, so it (and every future unknown source kind) fails closed.
    raise RepositoryConflictError(
        "Workflow signal intent source kind has no authoritative binding"
    )


_AUDIT_CONTROL_EVENT_TRANSITIONS: dict[
    WorkflowSignalKind,
    tuple[
        str,
        str,
        str,
        frozenset[tuple[str, str]],
    ],
] = {
    WorkflowSignalKind.PAUSE: (
        "audit_pause_requested",
        AuditLifecycleStatus.PAUSING.value,
        RunStatus.PAUSING.value,
        frozenset(
            {
                (AuditLifecycleStatus.RUNNING.value, RunStatus.RUNNING.value),
                (
                    AuditLifecycleStatus.WAITING_APPROVAL.value,
                    RunStatus.WAITING_APPROVAL.value,
                ),
            }
        ),
    ),
    WorkflowSignalKind.RESUME: (
        "audit_resume_requested",
        AuditLifecycleStatus.RUNNING.value,
        RunStatus.RUNNING.value,
        frozenset(
            {(AuditLifecycleStatus.PAUSED.value, RunStatus.PAUSED.value)}
        ),
    ),
    WorkflowSignalKind.CANCEL: (
        "audit_cancel_requested",
        AuditLifecycleStatus.CANCELLING.value,
        RunStatus.CANCELLING.value,
        frozenset(
            {
                (AuditLifecycleStatus.QUEUED.value, RunStatus.PREPARING.value),
                (AuditLifecycleStatus.PREFLIGHTING.value, RunStatus.PREPARING.value),
                (AuditLifecycleStatus.SNAPSHOTTING.value, RunStatus.PREPARING.value),
                (AuditLifecycleStatus.RUNNING.value, RunStatus.RUNNING.value),
                (
                    AuditLifecycleStatus.WAITING_APPROVAL.value,
                    RunStatus.WAITING_APPROVAL.value,
                ),
                (AuditLifecycleStatus.PAUSING.value, RunStatus.PAUSING.value),
                (AuditLifecycleStatus.PAUSED.value, RunStatus.PAUSED.value),
                (AuditLifecycleStatus.FINALIZING.value, RunStatus.COMPLETING.value),
                (AuditLifecycleStatus.FAILING.value, RunStatus.COMPLETING.value),
                (AuditLifecycleStatus.CLEANING.value, RunStatus.COMPLETING.value),
            }
        ),
    ),
}

_AUDIT_CONTROL_DELIVERY_FENCES: dict[
    WorkflowSignalKind,
    frozenset[tuple[str, str]],
] = {
    WorkflowSignalKind.PAUSE: frozenset(
        {
            (AuditLifecycleStatus.PAUSING.value, RunStatus.PAUSING.value),
            (AuditLifecycleStatus.PAUSED.value, RunStatus.PAUSED.value),
        }
    ),
    WorkflowSignalKind.RESUME: frozenset(
        {
            (AuditLifecycleStatus.RUNNING.value, RunStatus.RUNNING.value),
            (
                AuditLifecycleStatus.WAITING_APPROVAL.value,
                RunStatus.WAITING_APPROVAL.value,
            ),
        }
    ),
    WorkflowSignalKind.CANCEL: frozenset(
        {
            (AuditLifecycleStatus.CANCELLING.value, RunStatus.CANCELLING.value),
            (AuditLifecycleStatus.CLEANING.value, RunStatus.CANCELLING.value),
            (AuditLifecycleStatus.CLEANING.value, RunStatus.CANCELLED.value),
            (AuditLifecycleStatus.CANCELLED.value, RunStatus.CANCELLED.value),
        }
    ),
}


async def _require_exact_audit_control_source(
    session: AsyncSession,
    intent: WorkflowSignalIntent,
) -> None:
    """Bind one Audit control intent to its exact CAS event and live fence."""

    if (
        intent.owner_kind is not WorkflowSignalOwnerKind.CODE_AUDIT
        or intent.audit_id is None
    ):
        raise RepositoryConflictError(
            "Workflow control intent does not have an authoritative Code Audit owner"
        )
    audit_id = _exact_payload_identifier(intent, "audit_id")
    if audit_id != intent.audit_id:
        raise RepositoryConflictError(
            "Workflow control intent payload does not match its Audit owner"
        )
    contract = _AUDIT_CONTROL_EVENT_TRANSITIONS.get(intent.signal_kind)
    delivery_fences = _AUDIT_CONTROL_DELIVERY_FENCES.get(intent.signal_kind)
    if contract is None or delivery_fences is None:
        raise RepositoryConflictError(
            "Workflow control intent signal has no authoritative Audit contract"
        )

    audit = await session.get(AuditScanRecord, audit_id, with_for_update=True)
    if audit is None:
        raise EntityNotFoundError("AuditScan", audit_id)
    run = await session.get(RunRecord, intent.run_id, with_for_update=True)
    if run is None:
        raise EntityNotFoundError("Run", intent.run_id)
    source_event = await session.get(
        RunEventRecord,
        intent.source_event_id,
        with_for_update=True,
    )
    if source_event is None:
        raise EntityNotFoundError("RunEvent", intent.source_event_id)

    reason_code, target_audit, target_run, allowed_sources = contract
    payload = source_event.payload_json
    source_pair = (
        payload.get("from_audit_lifecycle"),
        payload.get("from_run_status"),
    )
    expected_payload = {
        "audit_id": audit_id,
        "operation": intent.signal_kind.value,
        "reason_code": reason_code,
        "from_audit_lifecycle": source_pair[0],
        "to_audit_lifecycle": target_audit,
        "from_run_status": source_pair[1],
        "to_run_status": target_run,
        "audit_state_version": intent.source_state_version,
    }
    current_pair = (audit.lifecycle_status, run.status)
    if (
        source_event.run_id != intent.run_id
        or source_event.event_type != "audit.control_projected"
        or source_pair not in allowed_sources
        or payload != expected_payload
        or audit.run_id != intent.run_id
        or audit.state_version < intent.source_state_version
        or current_pair not in delivery_fences
    ):
        raise RepositoryConflictError(
            "Workflow signal intent does not match its authoritative Audit control source"
        )


def _exact_payload_identifier(intent: WorkflowSignalIntent, key: str) -> str:
    value = intent.payload.get(key)
    if (
        not isinstance(value, str)
        or not value
        or set(intent.payload) != {key}
    ):
        raise RepositoryConflictError(
            "Workflow signal intent payload does not match its source contract"
        )
    return value


async def _find_creation_identity(
    session: AsyncSession,
    intent: WorkflowSignalIntent,
) -> WorkflowSignalIntentRecord | None:
    existing = await session.scalar(
        select(WorkflowSignalIntentRecord).where(
            WorkflowSignalIntentRecord.identity_digest == intent.identity_digest
        )
    )
    if existing is not None:
        return existing
    return await session.get(WorkflowSignalIntentRecord, intent.id)


def _intent_to_record(intent: WorkflowSignalIntent) -> WorkflowSignalIntentRecord:
    return WorkflowSignalIntentRecord(
        id=intent.id,
        schema_version=intent.schema_version,
        owner_kind=intent.owner_kind.value,
        owner_identity=intent.owner_identity,
        run_id=intent.run_id,
        run_kind=intent.run_kind.value,
        audit_id=intent.audit_id,
        workflow_protocol_version=intent.workflow_protocol_version,
        workflow_id=intent.workflow_id,
        signal_kind=intent.signal_kind.value,
        source_event_kind=intent.source_event_kind.value,
        source_event_id=intent.source_event_id,
        source_state_version=intent.source_state_version,
        identity_digest=intent.identity_digest,
        payload_json=canonical_workflow_signal_payload(intent.payload).decode("utf-8"),
        payload_digest=intent.payload_digest,
        delivery_state=intent.delivery_state.value,
        lease_owner=intent.lease_owner,
        lease_expires_at=intent.lease_expires_at,
        attempt=intent.attempt,
        next_attempt_at=intent.next_attempt_at,
        delivery_receipt_digest=intent.delivery_receipt_digest,
        last_error_code=intent.last_error_code,
        created_at=intent.created_at,
        updated_at=intent.updated_at,
        delivered_at=intent.delivered_at,
        state_version=intent.state_version,
    )


def _intent_from_record(record: WorkflowSignalIntentRecord) -> WorkflowSignalIntent:
    try:
        payload = json.loads(record.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("payload root is not an object")
        canonical = canonical_workflow_signal_payload(payload).decode("utf-8")
        if canonical != record.payload_json:
            raise ValueError("payload is not canonical")
        return WorkflowSignalIntent.model_validate(
            {
                "id": record.id,
                "schema_version": record.schema_version,
                "owner_kind": record.owner_kind,
                "owner_identity": record.owner_identity,
                "run_id": record.run_id,
                "run_kind": record.run_kind,
                "audit_id": record.audit_id,
                "workflow_protocol_version": record.workflow_protocol_version,
                "workflow_id": record.workflow_id,
                "signal_kind": record.signal_kind,
                "source_event_kind": record.source_event_kind,
                "source_event_id": record.source_event_id,
                "source_state_version": record.source_state_version,
                "identity_digest": record.identity_digest,
                "payload": payload,
                "payload_digest": record.payload_digest,
                "delivery_state": record.delivery_state,
                "lease_owner": record.lease_owner,
                "lease_expires_at": record.lease_expires_at,
                "attempt": record.attempt,
                "next_attempt_at": record.next_attempt_at,
                "delivery_receipt_digest": record.delivery_receipt_digest,
                "last_error_code": record.last_error_code,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "delivered_at": record.delivered_at,
                "state_version": record.state_version,
            }
        )
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError):
        raise RepositoryIntegrityError(
            "WorkflowSignalIntent",
            record.id,
            reason_code="invalid_persisted_state",
        ) from None


def _same_creation(
    existing: WorkflowSignalIntent,
    requested: WorkflowSignalIntent,
) -> bool:
    return (
        existing.identity_digest == requested.identity_digest
        and existing.owner_kind is requested.owner_kind
        and existing.owner_identity == requested.owner_identity
        and existing.run_id == requested.run_id
        and existing.run_kind is requested.run_kind
        and existing.audit_id == requested.audit_id
        and existing.workflow_protocol_version == requested.workflow_protocol_version
        and existing.workflow_id == requested.workflow_id
        and existing.signal_kind is requested.signal_kind
        and existing.source_event_kind is requested.source_event_kind
        and existing.source_event_id == requested.source_event_id
        and existing.source_state_version == requested.source_state_version
        and existing.payload_digest == requested.payload_digest
        and canonical_workflow_signal_payload(existing.payload)
        == canonical_workflow_signal_payload(requested.payload)
    )


def _validate_batch(limit: int) -> None:
    if limit < 1 or limit > 1000:
        raise ValueError("Workflow signal batch limit must be between 1 and 1000")


def _validate_claim(lease_owner: str, lease_duration: timedelta, limit: int) -> None:
    _validate_batch(limit)
    if not lease_owner or len(lease_owner) > 255:
        raise ValueError("Workflow signal lease owner must be 1-255 characters")
    if lease_duration <= timedelta(0):
        raise ValueError("Workflow signal lease duration must be positive")


def _validate_schedule(now: datetime, next_attempt_at: datetime) -> None:
    if now.utcoffset() is None or next_attempt_at.utcoffset() is None:
        raise ValueError("Workflow signal timestamps must be timezone-aware")
    if next_attempt_at < now:
        raise ValueError("Workflow signal next attempt cannot precede the update time")


def _transition_conflict(intent_id: str) -> Never:
    raise RepositoryConflictError(
        f"Workflow signal intent {intent_id!r} lease or state version changed"
    )


__all__ = [
    "SQLAlchemyWorkflowSignalIntentRepository",
    "WorkflowSignalIntentRecord",
]
