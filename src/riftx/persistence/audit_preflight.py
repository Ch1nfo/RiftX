"""Durable persistence for pre-Audit source-ingest jobs.

The restricted request/result columns in this module are deliberately absent
from generic repositories and API projections.  They are loaded only by the
owner-bound Preflight service and authenticated Source Runner transport.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import Select

from riftx.application.errors import (
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
)
from riftx.application.ports.audit_preflight import (
    AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION,
    AuditPreflightDispatch,
    AuditPreflightOwnerBinding,
    AuditPreflightReconciliationCandidate,
)
from riftx.domain.audit_preflight import (
    AUDIT_PREFLIGHT_EXIT_RECEIPT_SCHEMA_VERSION,
    AUDIT_PREFLIGHT_JOB_SCHEMA_VERSION,
    AUDIT_PREFLIGHT_STOP_RECEIPT_SCHEMA_VERSION,
    MAX_PREFLIGHT_COUNTER,
    AuditPreflightEffectOwner,
    AuditPreflightExitReceipt,
    AuditPreflightExitTerminalState,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightLeaseEnvelope,
    AuditPreflightResult,
    AuditPreflightStopDisposition,
    AuditPreflightStopReceipt,
    PreflightRequest,
    audit_preflight_is_exact_replay,
)
from riftx.domain.base import new_id
from riftx.domain.runner import RunnerPrincipal

from .orm import Base
from .transactions import serialized_write
from .types import UTCDateTime

SessionFactory = async_sessionmaker[AsyncSession]
type _ReconciliationRow = tuple[
    str,
    str,
    int,
    str,
    datetime,
    datetime | None,
    datetime,
    str | None,
    str | None,
]

_TERMINAL_STATUSES = frozenset(
    {
        AuditPreflightJobStatus.SUCCEEDED,
        AuditPreflightJobStatus.REJECTED,
        AuditPreflightJobStatus.FAILED,
        AuditPreflightJobStatus.CANCELLED,
    }
)
_RECONCILABLE_ACTIVE_STATUSES = (
    AuditPreflightJobStatus.CLAIMED,
    AuditPreflightJobStatus.RUNNING,
    AuditPreflightJobStatus.CANCELLING,
)


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def _optional_lower_hex_digest_check(column: str) -> str:
    return f"{column} IS NULL OR ({_lower_hex_digest_check(column)})"


def _canonical_uuid_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef-":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = 36 AND substr({column}, 9, 1) = '-' "
        f"AND substr({column}, 14, 1) = '-' AND substr({column}, 19, 1) = '-' "
        f"AND substr({column}, 24, 1) = '-' "
        f"AND length(replace({column}, '-', '')) = 32 "
        f"AND length({remainder}) = 0 "
        f"AND {column} <> '00000000-0000-0000-0000-000000000000'"
    )


class AuditPreflightJobRecord(Base):
    __tablename__ = "audit_preflight_jobs"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.audit-preflight-job/v1'",
            name="ck_audit_preflight_jobs_schema",
        ),
        CheckConstraint(
            "request_schema_version = 'riftx.audit-preflight-request/v1'",
            name="ck_audit_preflight_jobs_request_schema",
        ),
        CheckConstraint(
            "source_node_id = 'local'",
            name="ck_audit_preflight_jobs_local_node",
        ),
        CheckConstraint(
            "canonical_empty_context_id = 'riftx.audit-empty-security-context/v1'",
            name="ck_audit_preflight_jobs_empty_context",
        ),
        CheckConstraint(
            _canonical_uuid_check("client_request_id"),
            name="ck_audit_preflight_jobs_client_request_id",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'running', 'succeeded', 'rejected', "
            "'failed', 'cancelling', 'cancelled', 'outcome_unknown')",
            name="ck_audit_preflight_jobs_status",
        ),
        CheckConstraint(
            "state_version >= 1 AND attempt >= 0",
            name="ck_audit_preflight_jobs_versions",
        ),
        CheckConstraint(
            "(attempt = 0 AND lease_id IS NULL AND lease_owner_instance_id IS NULL "
            "AND lease_owner_epoch IS NULL AND lease_expires_at IS NULL "
            "AND lease_expected_state_version IS NULL "
            "AND lease_output_contract_digest IS NULL "
            "AND lease_envelope_digest IS NULL AND capsule_id IS NULL) OR "
            "(attempt >= 1 AND lease_id IS NOT NULL "
            "AND lease_owner_instance_id IS NOT NULL AND lease_owner_epoch >= 1 "
            "AND lease_expires_at IS NOT NULL AND lease_expected_state_version >= 1 "
            "AND lease_output_contract_digest IS NOT NULL "
            "AND lease_envelope_digest IS NOT NULL "
            "AND capsule_id IS NOT NULL)",
            name="ck_audit_preflight_jobs_lease_shape",
        ),
        CheckConstraint(
            "status <> 'pending' OR attempt = 0",
            name="ck_audit_preflight_jobs_pending_shape",
        ),
        CheckConstraint(
            "status NOT IN ('claimed', 'running', 'outcome_unknown') OR attempt >= 1",
            name="ck_audit_preflight_jobs_active_lease",
        ),
        CheckConstraint(
            "status NOT IN ('claimed', 'running') OR lease_expires_at > updated_at",
            name="ck_audit_preflight_jobs_active_lease_expiry",
        ),
        CheckConstraint(
            "capsule_prepare_proof_digest IS NULL OR capsule_id IS NOT NULL",
            name="ck_audit_preflight_jobs_prepare_capsule",
        ),
        CheckConstraint(
            "started_at IS NULL OR capsule_id IS NOT NULL",
            name="ck_audit_preflight_jobs_started_capsule",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND result_digest IS NOT NULL "
            "AND exit_receipt_digest IS NOT NULL AND safe_error_code IS NULL "
            "AND never_created_proof_digest IS NULL AND stop_receipt_digest IS NULL "
            "AND capsule_prepare_proof_digest IS NOT NULL AND started_at IS NOT NULL) "
            "OR status <> 'succeeded'",
            name="ck_audit_preflight_jobs_succeeded_shape",
        ),
        CheckConstraint(
            "(status = 'rejected' AND result_digest IS NOT NULL "
            "AND exit_receipt_digest IS NOT NULL AND safe_error_code IS NOT NULL "
            "AND never_created_proof_digest IS NULL AND stop_receipt_digest IS NULL "
            "AND capsule_prepare_proof_digest IS NOT NULL AND started_at IS NOT NULL) "
            "OR status <> 'rejected'",
            name="ck_audit_preflight_jobs_rejected_shape",
        ),
        CheckConstraint(
            "(status = 'failed' AND result_digest IS NULL "
            "AND safe_error_code IS NOT NULL AND finished_at IS NOT NULL "
            "AND (exit_receipt_digest IS NOT NULL OR stop_receipt_digest IS NOT NULL "
            "OR never_created_proof_digest IS NOT NULL)) OR status <> 'failed'",
            name="ck_audit_preflight_jobs_failed_proof",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND result_digest IS NULL "
            "AND exit_receipt_digest IS NULL AND finished_at IS NOT NULL "
            "AND (stop_receipt_digest IS NOT NULL "
            "OR never_created_proof_digest IS NOT NULL)) OR status <> 'cancelled'",
            name="ck_audit_preflight_jobs_cancelled_proof",
        ),
        CheckConstraint(
            "status IN ('succeeded', 'rejected') OR result_digest IS NULL",
            name="ck_audit_preflight_jobs_result_status",
        ),
        CheckConstraint(
            "status IN ('succeeded', 'rejected', 'failed', 'outcome_unknown') "
            "OR exit_receipt_digest IS NULL",
            name="ck_audit_preflight_jobs_exit_receipt_status",
        ),
        CheckConstraint(
            "exit_receipt_digest IS NULL OR "
            "(stop_receipt_digest IS NULL AND never_created_proof_digest IS NULL)",
            name="ck_audit_preflight_jobs_terminal_proof_exclusive",
        ),
        CheckConstraint(
            "status IN ('cancelling', 'cancelled', 'failed', 'outcome_unknown') "
            "OR (stop_receipt_digest IS NULL AND never_created_proof_digest IS NULL)",
            name="ck_audit_preflight_jobs_stop_proof_status",
        ),
        CheckConstraint(
            "status NOT IN ('pending', 'claimed', 'running') OR safe_error_code IS NULL",
            name="ck_audit_preflight_jobs_active_error",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'rejected', 'failed', 'cancelled') "
            "AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('succeeded', 'rejected', 'failed', 'cancelled') "
            "AND finished_at IS NULL)",
            name="ck_audit_preflight_jobs_finished_state",
        ),
        CheckConstraint(
            "updated_at >= created_at AND expires_at > created_at "
            "AND (started_at IS NULL OR "
            "(started_at >= created_at AND started_at <= updated_at)) "
            "AND (finished_at IS NULL OR "
            "(finished_at >= created_at AND finished_at <= updated_at)) "
            "AND (started_at IS NULL OR finished_at IS NULL "
            "OR finished_at >= started_at)",
            name="ck_audit_preflight_jobs_timestamps",
        ),
        *(
            CheckConstraint(
                _optional_lower_hex_digest_check(column),
                name=f"ck_audit_preflight_jobs_{column}",
            )
            for column in (
                "capsule_prepare_proof_digest",
                "lease_output_contract_digest",
                "lease_envelope_digest",
                "result_digest",
                "exit_receipt_digest",
                "never_created_proof_digest",
                "stop_receipt_digest",
            )
        ),
        *(
            CheckConstraint(
                _lower_hex_digest_check(column),
                name=f"ck_audit_preflight_jobs_{column}",
            )
            for column in (
                "authorization_scope_digest",
                "request_digest",
                "source_root_identity_digest",
                "image_digest",
                "policy_digest",
                "canonical_empty_context_digest",
                "effect_owner_digest",
            )
        ),
        UniqueConstraint(
            "operator_principal_id",
            "client_request_id",
            name="uq_audit_preflight_jobs_client_request",
        ),
        Index(
            "ix_audit_preflight_jobs_dispatch",
            "source_node_id",
            "status",
            "expires_at",
            "created_at",
            "id",
        ),
        Index(
            "ix_audit_preflight_jobs_lease",
            "status",
            "lease_expires_at",
            "updated_at",
            "id",
        ),
        Index(
            "ix_audit_preflight_jobs_reconcile",
            "status",
            "updated_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operator_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    authorization_scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_issuance_schema_version: Mapped[str | None] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_root_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    backend_id: Mapped[str] = mapped_column(String(128), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_empty_context_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_empty_context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    effect_owner_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    capsule_id: Mapped[str | None] = mapped_column(String(128))
    capsule_prepare_proof_digest: Mapped[str | None] = mapped_column(String(64))
    lease_id: Mapped[str | None] = mapped_column(String(128))
    lease_owner_instance_id: Mapped[str | None] = mapped_column(String(64))
    lease_owner_epoch: Mapped[int | None] = mapped_column(BigInteger)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expected_state_version: Mapped[int | None] = mapped_column(BigInteger)
    lease_output_contract_digest: Mapped[str | None] = mapped_column(String(64))
    lease_envelope_digest: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    exit_receipt_digest: Mapped[str | None] = mapped_column(String(64))
    safe_error_code: Mapped[str | None] = mapped_column(String(128))
    never_created_proof_digest: Mapped[str | None] = mapped_column(String(64))
    stop_receipt_digest: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)


class AuditPreflightJobRequestRecord(Base):
    __tablename__ = "audit_preflight_job_requests"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.audit-preflight-request/v1'",
            name="ck_audit_preflight_job_requests_schema",
        ),
        CheckConstraint(
            _lower_hex_digest_check("request_digest"),
            name="ck_audit_preflight_job_requests_digest",
        ),
    )

    job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "audit_preflight_jobs.id",
            name="fk_audit_preflight_job_requests_job",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditPreflightResultRecord(Base):
    __tablename__ = "audit_preflight_results"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.audit-preflight-result/v1'",
            name="ck_audit_preflight_results_schema",
        ),
        CheckConstraint(
            _lower_hex_digest_check("result_digest"),
            name="ck_audit_preflight_results_digest",
        ),
        UniqueConstraint("job_id", name="uq_audit_preflight_results_job"),
        UniqueConstraint("result_digest", name="uq_audit_preflight_results_digest"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "audit_preflight_jobs.id",
            name="fk_audit_preflight_results_job",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditPreflightExitReceiptRecord(Base):
    __tablename__ = "audit_preflight_exit_receipts"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.audit-preflight-exit-receipt/v1'",
            name="ck_audit_preflight_exit_receipts_schema",
        ),
        CheckConstraint(
            _lower_hex_digest_check("receipt_digest"),
            name="ck_audit_preflight_exit_receipts_digest",
        ),
        UniqueConstraint("job_id", name="uq_audit_preflight_exit_receipts_job"),
        UniqueConstraint(
            "receipt_digest",
            name="uq_audit_preflight_exit_receipts_digest",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "audit_preflight_jobs.id",
            name="fk_audit_preflight_exit_receipts_job",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditPreflightStopReceiptRecord(Base):
    __tablename__ = "audit_preflight_stop_receipts"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.audit-preflight-stop-receipt/v1'",
            name="ck_audit_preflight_stop_receipts_schema",
        ),
        CheckConstraint(
            "disposition IN ('stopped', 'never_created')",
            name="ck_audit_preflight_stop_receipts_disposition",
        ),
        CheckConstraint(
            _lower_hex_digest_check("receipt_digest"),
            name="ck_audit_preflight_stop_receipts_digest",
        ),
        UniqueConstraint("job_id", name="uq_audit_preflight_stop_receipts_job"),
        UniqueConstraint(
            "receipt_digest",
            name="uq_audit_preflight_stop_receipts_digest",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "audit_preflight_jobs.id",
            name="fk_audit_preflight_stop_receipts_job",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SQLAlchemyAuditPreflightRepository:
    """Strict create/replay and CAS repository for Preflight Job ownership."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        job: AuditPreflightJob,
    ) -> tuple[AuditPreflightJob, bool]:
        if (
            job.status is not AuditPreflightJobStatus.PENDING
            or job.state_version != 1
            or job.attempt != 0
        ):
            raise ValueError("new Audit Preflight Job must be pending at version one")
        try:
            async with serialized_write(self._session_factory) as session:
                existing_id = await session.scalar(
                    select(AuditPreflightJobRecord.id).where(
                        AuditPreflightJobRecord.operator_principal_id == job.operator_principal_id,
                        AuditPreflightJobRecord.client_request_id == job.client_request_id,
                    )
                )
                if existing_id is not None:
                    existing = await _load_job(session, existing_id)
                    return _require_exact_create_replay(existing, job), False
                session.add(_job_to_record(job))
                session.add(
                    AuditPreflightJobRequestRecord(
                        job_id=job.job_id,
                        schema_version=job.request_schema_version,
                        canonical_json=job.restricted_request_json,
                        request_digest=job.request_digest,
                        created_at=job.created_at,
                    )
                )
                await session.flush()
                return job, True
        except IntegrityError as exc:
            try:
                async with self._session_factory() as session:
                    existing_id = await session.scalar(
                        select(AuditPreflightJobRecord.id).where(
                            AuditPreflightJobRecord.operator_principal_id
                            == job.operator_principal_id,
                            AuditPreflightJobRecord.client_request_id == job.client_request_id,
                        )
                    )
                    if existing_id is not None:
                        existing = await _load_job(session, existing_id)
                        return _require_exact_create_replay(existing, job), False
            except RepositoryIntegrityError:
                raise
            except SQLAlchemyError as read_exc:
                raise RepositoryUnavailableError(
                    "Audit Preflight replay lookup is unavailable"
                ) from read_exc
            raise RepositoryConflictError(
                "Audit Preflight creation conflicts with durable ownership"
            ) from exc
        except RepositoryConflictError:
            raise
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Audit Preflight creation is unavailable") from exc

    async def get_owner_binding(
        self,
        job_id: str,
    ) -> AuditPreflightOwnerBinding | None:
        try:
            async with self._session_factory() as session:
                record = await session.get(AuditPreflightJobRecord, job_id)
            if record is None:
                return None
            return _owner_binding_from_record(record)
        except (TypeError, ValueError):
            raise RepositoryIntegrityError("AuditPreflightJob", job_id) from None
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Audit Preflight owner lookup is unavailable") from exc

    async def get_idempotency_binding(
        self,
        *,
        operator_principal_id: str,
        client_request_id: str,
    ) -> AuditPreflightOwnerBinding | None:
        try:
            async with self._session_factory() as session:
                record = await session.scalar(
                    select(AuditPreflightJobRecord).where(
                        AuditPreflightJobRecord.operator_principal_id == operator_principal_id,
                        AuditPreflightJobRecord.client_request_id == client_request_id,
                    )
                )
            if record is None:
                return None
            return _owner_binding_from_record(record)
        except (TypeError, ValueError):
            raise RepositoryIntegrityError(
                "AuditPreflightJob",
                "idempotency-binding",
            ) from None
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight idempotency lookup is unavailable"
            ) from exc

    async def get(self, job_id: str) -> AuditPreflightJob | None:
        try:
            async with self._session_factory() as session:
                record = await session.get(AuditPreflightJobRecord, job_id)
                if record is None:
                    return None
                return await _load_job(session, job_id, job_record=record)
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Audit Preflight lookup is unavailable") from exc

    async def get_reconciliation_candidate(
        self,
        job_id: str,
    ) -> AuditPreflightReconciliationCandidate | None:
        """Load only bounded state needed by an idempotent reconciler replay."""

        try:
            async with self._session_factory() as session:
                row = (
                    (
                        await session.execute(
                            _reconciliation_projection().where(AuditPreflightJobRecord.id == job_id)
                        )
                    )
                    .tuples()
                    .one_or_none()
                )
            if row is None:
                return None
            return _reconciliation_candidate_from_row(row)
        except RepositoryIntegrityError:
            raise
        except (TypeError, ValueError):
            raise RepositoryIntegrityError("AuditPreflightJob", job_id) from None
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight reconciliation lookup is unavailable"
            ) from exc

    async def get_replayable_claim(
        self,
        *,
        node_id: str,
        runner_instance_id: str,
        runner_epoch: int,
        now: datetime,
    ) -> AuditPreflightDispatch | None:
        """Return only this exact Runner's still-live claimed dispatch.

        This is the durable HTTP-response-loss recovery path.  Filtering occurs
        in SQL before the restricted request row is loaded, so another owner
        generation's source path is never materialized for the caller.
        """

        if node_id != "local":
            return None
        _require_aware_datetime(now, label="claim replay time")
        RunnerPrincipal(instance_id=runner_instance_id, epoch=runner_epoch)
        try:
            async with self._session_factory() as session:
                candidate_id = await session.scalar(
                    select(AuditPreflightJobRecord.id)
                    .where(
                        AuditPreflightJobRecord.source_node_id == node_id,
                        AuditPreflightJobRecord.status == AuditPreflightJobStatus.CLAIMED.value,
                        AuditPreflightJobRecord.lease_owner_instance_id == runner_instance_id,
                        AuditPreflightJobRecord.lease_owner_epoch == runner_epoch,
                        AuditPreflightJobRecord.lease_expires_at > now,
                        AuditPreflightJobRecord.expires_at > now,
                    )
                    .order_by(
                        AuditPreflightJobRecord.updated_at,
                        AuditPreflightJobRecord.id,
                    )
                    .limit(1)
                )
                if candidate_id is None:
                    return None
                job = await _load_job(session, candidate_id)
                if (
                    job.status is not AuditPreflightJobStatus.CLAIMED
                    or job.lease_owner_instance_id != runner_instance_id
                    or job.lease_owner_epoch != runner_epoch
                    or job.lease_expires_at is None
                    or job.lease_expires_at <= now
                    or job.expires_at <= now
                ):
                    return None
                return AuditPreflightDispatch(
                    job=job,
                    request=PreflightRequest.model_validate_json(job.restricted_request_json),
                )
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Audit Preflight claim replay is unavailable") from exc

    async def claim_next(
        self,
        *,
        node_id: str,
        runner_instance_id: str,
        runner_epoch: int,
        now: datetime,
        lease_expires_at: datetime,
        output_contract_digest: str,
    ) -> AuditPreflightDispatch | None:
        if node_id != "local":
            return None
        _require_aware_datetime(now, label="claim time")
        _require_aware_datetime(lease_expires_at, label="lease expiry")
        RunnerPrincipal(instance_id=runner_instance_id, epoch=runner_epoch)
        _require_digest(output_contract_digest, label="output contract digest")
        if runner_epoch < 1 or lease_expires_at <= now:
            raise ValueError("Audit Preflight claim has an invalid Runner lease")
        try:
            async with serialized_write(self._session_factory) as session:
                candidate_ids = list(
                    await session.scalars(
                        select(AuditPreflightJobRecord.id)
                        .where(
                            AuditPreflightJobRecord.source_node_id == node_id,
                            AuditPreflightJobRecord.expires_at > now,
                            AuditPreflightJobRecord.status == AuditPreflightJobStatus.PENDING.value,
                        )
                        .order_by(
                            AuditPreflightJobRecord.created_at,
                            AuditPreflightJobRecord.id,
                        )
                        .limit(20)
                        .with_for_update(skip_locked=True)
                    )
                )
                for candidate_id in candidate_ids:
                    current = await _load_job(session, candidate_id, for_update=True)
                    if current.status is not AuditPreflightJobStatus.PENDING:
                        continue
                    current.validate_transition_to(AuditPreflightJobStatus.CLAIMED)
                    bounded_expiry = min(lease_expires_at, current.expires_at)
                    if bounded_expiry <= now:
                        continue
                    next_state_version = current.state_version + 1
                    lease_id = new_id()
                    runner_principal = RunnerPrincipal(
                        instance_id=runner_instance_id,
                        epoch=runner_epoch,
                    )
                    lease_envelope = AuditPreflightLeaseEnvelope(
                        owner=current.effect_owner(),
                        runner_principal=runner_principal,
                        lease_id=lease_id,
                        lease_expires_at=bounded_expiry,
                        expected_state_version=next_state_version,
                        output_contract_digest=output_contract_digest,
                    )
                    updated = _replace_job(
                        current,
                        status=AuditPreflightJobStatus.CLAIMED,
                        state_version=next_state_version,
                        attempt=1,
                        lease_id=lease_id,
                        lease_owner_instance_id=runner_instance_id,
                        lease_owner_epoch=runner_epoch,
                        lease_expires_at=bounded_expiry,
                        lease_expected_state_version=next_state_version,
                        lease_output_contract_digest=output_contract_digest,
                        lease_envelope_digest=lease_envelope.lease_envelope_digest,
                        capsule_id=new_id(),
                        capsule_prepare_proof_digest=None,
                        started_at=None,
                        updated_at=now,
                    )
                    _apply_job_to_record(
                        updated,
                        await _require_job_record(session, candidate_id),
                    )
                    await session.flush()
                    return AuditPreflightDispatch(
                        job=updated,
                        request=PreflightRequest.model_validate_json(
                            updated.restricted_request_json
                        ),
                    )
                return None
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Audit Preflight claim is unavailable") from exc

    async def compare_and_set(
        self,
        *,
        previous: AuditPreflightJob,
        updated: AuditPreflightJob,
        result: AuditPreflightResult | None = None,
        exit_receipt: AuditPreflightExitReceipt | None = None,
        stop_receipt: AuditPreflightStopReceipt | None = None,
    ) -> AuditPreflightJob:
        terminal_replay = _validate_cas_request(previous, updated)
        try:
            async with serialized_write(self._session_factory) as session:
                persisted = await _load_job(session, previous.job_id, for_update=True)
                if persisted != previous:
                    raise RepositoryConflictError(
                        "Audit Preflight Job changed before compare-and-set"
                    )
                if terminal_replay:
                    await _persist_result(session, persisted, result)
                    await _persist_exit_receipt(session, persisted, exit_receipt)
                    await _persist_stop_receipt(session, persisted, stop_receipt)
                    return persisted
                record = await _require_job_record(session, previous.job_id)
                _apply_job_to_record(updated, record)
                await _persist_result(session, updated, result)
                await _persist_exit_receipt(session, updated, exit_receipt)
                await _persist_stop_receipt(session, updated, stop_receipt)
                await session.flush()
                return updated
        except RepositoryConflictError:
            raise
        except RepositoryIntegrityError:
            raise
        except IntegrityError as exc:
            raise RepositoryConflictError(
                "Audit Preflight compare-and-set conflicts with durable facts"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight compare-and-set is unavailable"
            ) from exc

    async def compare_and_set_reconciliation(
        self,
        *,
        previous: AuditPreflightReconciliationCandidate,
        status: AuditPreflightJobStatus,
        observed_at: datetime,
        never_created_proof_digest: str | None = None,
    ) -> AuditPreflightReconciliationCandidate:
        """Apply one expiry-only CAS without materializing restricted data."""

        _require_aware_datetime(observed_at, label="reconciliation observation")
        if previous.state_version >= MAX_PREFLIGHT_COUNTER:
            raise ValueError("Audit Preflight reconciliation state version is exhausted")
        changed_at = max(observed_at, previous.updated_at)
        values: dict[str, object]
        if previous.status is AuditPreflightJobStatus.PENDING:
            if (
                status is not AuditPreflightJobStatus.CANCELLED
                or previous.expires_at > observed_at
                or previous.lease_expires_at is not None
                or previous.never_created_proof_digest is not None
                or previous.stop_receipt_digest is not None
                or never_created_proof_digest is None
            ):
                raise ValueError("Audit Preflight pending reconciliation is invalid")
            _require_digest(
                never_created_proof_digest,
                label="never-created proof digest",
            )
            values = {
                "status": status.value,
                "state_version": previous.state_version + 1,
                "never_created_proof_digest": never_created_proof_digest,
                "finished_at": changed_at,
                "updated_at": changed_at,
            }
        elif previous.status in _RECONCILABLE_ACTIVE_STATUSES:
            if (
                status is not AuditPreflightJobStatus.OUTCOME_UNKNOWN
                or previous.lease_expires_at is None
                or previous.lease_expires_at > observed_at
                or previous.never_created_proof_digest is not None
                or previous.stop_receipt_digest is not None
                or never_created_proof_digest is not None
            ):
                raise ValueError("Audit Preflight active reconciliation is invalid")
            values = {
                "status": status.value,
                "state_version": previous.state_version + 1,
                "updated_at": changed_at,
            }
        else:
            raise ValueError("Audit Preflight reconciliation source state is invalid")

        try:
            async with serialized_write(self._session_factory) as session:
                result = await session.execute(
                    update(AuditPreflightJobRecord)
                    .where(
                        AuditPreflightJobRecord.id == previous.job_id,
                        AuditPreflightJobRecord.status == previous.status.value,
                        AuditPreflightJobRecord.state_version == previous.state_version,
                        AuditPreflightJobRecord.effect_owner_digest == previous.effect_owner_digest,
                        AuditPreflightJobRecord.expires_at == previous.expires_at,
                        AuditPreflightJobRecord.lease_expires_at == previous.lease_expires_at,
                        AuditPreflightJobRecord.updated_at == previous.updated_at,
                        AuditPreflightJobRecord.never_created_proof_digest
                        == previous.never_created_proof_digest,
                        AuditPreflightJobRecord.stop_receipt_digest == previous.stop_receipt_digest,
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise RepositoryConflictError(
                        "Audit Preflight Job changed before reconciliation"
                    )
                await session.flush()
            return replace(
                previous,
                status=status,
                state_version=previous.state_version + 1,
                updated_at=changed_at,
                never_created_proof_digest=(
                    never_created_proof_digest
                    if status is AuditPreflightJobStatus.CANCELLED
                    else previous.never_created_proof_digest
                ),
            )
        except RepositoryConflictError:
            raise
        except IntegrityError as exc:
            raise RepositoryConflictError(
                "Audit Preflight reconciliation conflicts with durable facts"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight reconciliation mutation is unavailable"
            ) from exc

    async def list_reconciliation_candidates(
        self,
        *,
        observed_at: datetime,
        limit: int,
    ) -> tuple[AuditPreflightReconciliationCandidate, ...]:
        """Return only already-expired, actionable, bounded job projections."""

        if not 1 <= limit <= 1_000:
            raise ValueError("Audit Preflight reconciliation limit is invalid")
        _require_aware_datetime(observed_at, label="reconciliation observation")
        pending_expired = and_(
            AuditPreflightJobRecord.status == AuditPreflightJobStatus.PENDING.value,
            AuditPreflightJobRecord.expires_at <= observed_at,
        )
        active_lease_expired = and_(
            AuditPreflightJobRecord.status.in_(
                tuple(status.value for status in _RECONCILABLE_ACTIVE_STATUSES)
            ),
            AuditPreflightJobRecord.lease_expires_at.is_not(None),
            AuditPreflightJobRecord.lease_expires_at <= observed_at,
            AuditPreflightJobRecord.never_created_proof_digest.is_(None),
            AuditPreflightJobRecord.stop_receipt_digest.is_(None),
        )
        try:
            async with self._session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            _reconciliation_projection()
                            .where(
                                AuditPreflightJobRecord.updated_at <= observed_at,
                                or_(pending_expired, active_lease_expired),
                            )
                            .order_by(
                                func.coalesce(
                                    AuditPreflightJobRecord.lease_expires_at,
                                    AuditPreflightJobRecord.expires_at,
                                ),
                                AuditPreflightJobRecord.id,
                            )
                            .limit(limit)
                        )
                    )
                    .tuples()
                    .all()
                )
            return tuple(_reconciliation_candidate_from_row(row) for row in rows)
        except RepositoryIntegrityError:
            raise
        except (TypeError, ValueError):
            raise RepositoryIntegrityError(
                "AuditPreflightJob",
                "reconciliation-scan",
            ) from None
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Audit Preflight reconciliation scan is unavailable"
            ) from exc


def _reconciliation_projection() -> Select[_ReconciliationRow]:
    return select(
        AuditPreflightJobRecord.id,
        AuditPreflightJobRecord.status,
        AuditPreflightJobRecord.state_version,
        AuditPreflightJobRecord.effect_owner_digest,
        AuditPreflightJobRecord.expires_at,
        AuditPreflightJobRecord.lease_expires_at,
        AuditPreflightJobRecord.updated_at,
        AuditPreflightJobRecord.never_created_proof_digest,
        AuditPreflightJobRecord.stop_receipt_digest,
    )


def _reconciliation_candidate_from_row(
    row: _ReconciliationRow,
) -> AuditPreflightReconciliationCandidate:
    (
        job_id,
        persisted_status,
        state_version,
        effect_owner_digest,
        expires_at,
        lease_expires_at,
        updated_at,
        never_created_proof_digest,
        stop_receipt_digest,
    ) = row
    if not job_id or len(job_id) > 128 or not 1 <= state_version <= MAX_PREFLIGHT_COUNTER:
        raise ValueError("invalid Audit Preflight reconciliation identity")
    status = AuditPreflightJobStatus(persisted_status)
    _require_digest(effect_owner_digest, label="effect owner digest")
    _require_aware_datetime(expires_at, label="expiry")
    _require_aware_datetime(updated_at, label="update time")
    if lease_expires_at is not None:
        _require_aware_datetime(lease_expires_at, label="lease expiry")
    if never_created_proof_digest is not None:
        _require_digest(
            never_created_proof_digest,
            label="never-created proof digest",
        )
    if stop_receipt_digest is not None:
        _require_digest(stop_receipt_digest, label="stop receipt digest")
    if never_created_proof_digest is not None and stop_receipt_digest is not None:
        raise ValueError("Audit Preflight reconciliation proof is ambiguous")
    if status is AuditPreflightJobStatus.PENDING and lease_expires_at is not None:
        raise ValueError("pending Audit Preflight reconciliation has a lease")
    if status in _RECONCILABLE_ACTIVE_STATUSES and lease_expires_at is None:
        raise ValueError("active Audit Preflight reconciliation lacks a lease")
    return AuditPreflightReconciliationCandidate(
        job_id=job_id,
        status=status,
        state_version=state_version,
        effect_owner_digest=effect_owner_digest,
        expires_at=expires_at,
        lease_expires_at=lease_expires_at,
        updated_at=updated_at,
        never_created_proof_digest=never_created_proof_digest,
        stop_receipt_digest=stop_receipt_digest,
    )


def _job_to_record(job: AuditPreflightJob) -> AuditPreflightJobRecord:
    record = AuditPreflightJobRecord(
        id=job.job_id,
        plan_issuance_schema_version=AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION,
    )
    _apply_job_to_record(job, record)
    return record


def _apply_job_to_record(
    job: AuditPreflightJob,
    record: AuditPreflightJobRecord,
) -> None:
    values = {
        "schema_version": job.schema_version,
        "client_request_id": job.client_request_id,
        "operator_principal_id": job.operator_principal_id,
        "authorization_scope_digest": job.authorization_scope_digest,
        "request_schema_version": job.request_schema_version,
        "request_digest": job.request_digest,
        "source_node_id": job.source_node_id,
        "source_root_identity_digest": job.source_root_identity_digest,
        "backend_id": job.backend_id,
        "image_digest": job.image_digest,
        "policy_digest": job.policy_digest,
        "canonical_empty_context_id": job.canonical_empty_context_id,
        "canonical_empty_context_digest": job.canonical_empty_context_digest,
        "status": job.status.value,
        "effect_owner_digest": job.effect_owner_digest,
        "capsule_id": job.capsule_id,
        "capsule_prepare_proof_digest": job.capsule_prepare_proof_digest,
        "lease_id": job.lease_id,
        "lease_owner_instance_id": job.lease_owner_instance_id,
        "lease_owner_epoch": job.lease_owner_epoch,
        "lease_expires_at": job.lease_expires_at,
        "lease_expected_state_version": job.lease_expected_state_version,
        "lease_output_contract_digest": job.lease_output_contract_digest,
        "lease_envelope_digest": job.lease_envelope_digest,
        "attempt": job.attempt,
        "result_digest": job.result_digest,
        "exit_receipt_digest": job.exit_receipt_digest,
        "safe_error_code": job.safe_error_code,
        "never_created_proof_digest": job.never_created_proof_digest,
        "stop_receipt_digest": job.stop_receipt_digest,
        "expires_at": job.expires_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "state_version": job.state_version,
    }
    for field_name, value in values.items():
        setattr(record, field_name, value)


async def _load_job(
    session: AsyncSession,
    job_id: str,
    *,
    job_record: AuditPreflightJobRecord | None = None,
    for_update: bool = False,
) -> AuditPreflightJob:
    try:
        record = job_record
        if record is None:
            statement = select(AuditPreflightJobRecord).where(AuditPreflightJobRecord.id == job_id)
            if for_update:
                statement = statement.with_for_update()
            record = await session.scalar(statement)
        if record is None:
            raise RepositoryConflictError("Audit Preflight Job no longer exists")
        request_record = await session.get(AuditPreflightJobRequestRecord, job_id)
        if request_record is None:
            raise RepositoryIntegrityError(
                "AuditPreflightJob",
                job_id,
                reason_code="restricted_request_missing",
            )
        if (
            request_record.schema_version != record.request_schema_version
            or request_record.request_digest != record.request_digest
            or request_record.created_at != record.created_at
        ):
            raise RepositoryIntegrityError(
                "AuditPreflightJob",
                job_id,
                reason_code="restricted_request_binding_mismatch",
            )
        request = PreflightRequest.model_validate_json(request_record.canonical_json)
        if request.canonical_json() != request_record.canonical_json:
            raise RepositoryIntegrityError(
                "AuditPreflightJob",
                job_id,
                reason_code="restricted_request_not_canonical",
            )
        result_record = await session.scalar(
            select(AuditPreflightResultRecord).where(AuditPreflightResultRecord.job_id == job_id)
        )
        if (record.result_digest is None) != (result_record is None):
            raise RepositoryIntegrityError(
                "AuditPreflightJob",
                job_id,
                reason_code="result_binding_missing",
            )
        exit_record = await session.scalar(
            select(AuditPreflightExitReceiptRecord).where(
                AuditPreflightExitReceiptRecord.job_id == job_id
            )
        )
        if (record.exit_receipt_digest is None) != (exit_record is None):
            raise RepositoryIntegrityError(
                "AuditPreflightJob",
                job_id,
                reason_code="exit_receipt_binding_missing",
            )
        stop_record = await session.scalar(
            select(AuditPreflightStopReceiptRecord).where(
                AuditPreflightStopReceiptRecord.job_id == job_id
            )
        )
        if (record.stop_receipt_digest is None) != (stop_record is None):
            raise RepositoryIntegrityError(
                "AuditPreflightJob",
                job_id,
                reason_code="stop_receipt_binding_missing",
            )

        result: AuditPreflightResult | None = None
        if result_record is not None:
            result = AuditPreflightResult.model_validate_json(result_record.canonical_json)
            if (
                result_record.schema_version != result.schema_version
                or result_record.result_digest != record.result_digest
                or result_record.result_digest != result.result_digest
                or result_record.created_at != result.completed_at
                or result.canonical_json() != result_record.canonical_json
            ):
                raise RepositoryIntegrityError(
                    "AuditPreflightJob",
                    job_id,
                    reason_code="result_binding_mismatch",
                )

        exit_receipt: AuditPreflightExitReceipt | None = None
        if exit_record is not None:
            exit_receipt = AuditPreflightExitReceipt.model_validate_json(exit_record.canonical_json)
            if (
                exit_record.schema_version != exit_receipt.schema_version
                or exit_receipt.receipt_digest != record.exit_receipt_digest
                or exit_record.receipt_digest != exit_receipt.receipt_digest
                or exit_record.received_at != exit_receipt.received_at
                or exit_receipt.canonical_json() != exit_record.canonical_json
            ):
                raise RepositoryIntegrityError(
                    "AuditPreflightJob",
                    job_id,
                    reason_code="exit_receipt_binding_mismatch",
                )

        stop_receipt: AuditPreflightStopReceipt | None = None
        if stop_record is not None:
            stop_receipt = AuditPreflightStopReceipt.model_validate_json(stop_record.canonical_json)
            if (
                stop_record.schema_version != stop_receipt.schema_version
                or stop_record.disposition != stop_receipt.disposition.value
                or stop_receipt.receipt_digest != record.stop_receipt_digest
                or stop_record.receipt_digest != stop_receipt.receipt_digest
                or stop_record.received_at != stop_receipt.received_at
                or stop_receipt.canonical_json() != stop_record.canonical_json
            ):
                raise RepositoryIntegrityError(
                    "AuditPreflightJob",
                    job_id,
                    reason_code="stop_receipt_binding_mismatch",
                )

        job = AuditPreflightJob.model_validate(
            {
                "schema_version": record.schema_version,
                "job_id": record.id,
                "client_request_id": record.client_request_id,
                "operator_principal_id": record.operator_principal_id,
                "authorization_scope_digest": record.authorization_scope_digest,
                "request_schema_version": record.request_schema_version,
                "request_digest": record.request_digest,
                "restricted_request_json": request_record.canonical_json,
                "source_node_id": record.source_node_id,
                "source_root_identity_digest": record.source_root_identity_digest,
                "backend_id": record.backend_id,
                "image_digest": record.image_digest,
                "policy_digest": record.policy_digest,
                "canonical_empty_context_id": record.canonical_empty_context_id,
                "canonical_empty_context_digest": record.canonical_empty_context_digest,
                "status": AuditPreflightJobStatus(record.status),
                "state_version": record.state_version,
                "effect_owner_digest": record.effect_owner_digest,
                "attempt": record.attempt,
                "lease_id": record.lease_id,
                "lease_owner_instance_id": record.lease_owner_instance_id,
                "lease_owner_epoch": record.lease_owner_epoch,
                "lease_expires_at": record.lease_expires_at,
                "lease_expected_state_version": record.lease_expected_state_version,
                "lease_output_contract_digest": record.lease_output_contract_digest,
                "lease_envelope_digest": record.lease_envelope_digest,
                "capsule_id": record.capsule_id,
                "capsule_prepare_proof_digest": record.capsule_prepare_proof_digest,
                "result_schema_version": (
                    result_record.schema_version if result_record is not None else None
                ),
                "result_json": (
                    result_record.canonical_json if result_record is not None else None
                ),
                "result_digest": record.result_digest,
                "exit_receipt_digest": record.exit_receipt_digest,
                "safe_error_code": record.safe_error_code,
                "never_created_proof_digest": record.never_created_proof_digest,
                "stop_receipt_digest": record.stop_receipt_digest,
                "expires_at": record.expires_at,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
            }
        )
        if request != PreflightRequest.model_validate_json(job.restricted_request_json):
            raise RepositoryIntegrityError(
                "AuditPreflightJob",
                job_id,
                reason_code="restricted_request_round_trip_mismatch",
            )
        if result is not None:
            _validate_result_binding(job, result)
        if exit_receipt is not None:
            _validate_exit_receipt_binding(job, exit_receipt)
        if stop_receipt is not None:
            _validate_stop_receipt_binding(job, stop_receipt)
        return job
    except RepositoryConflictError:
        raise
    except RepositoryIntegrityError:
        raise
    except (AttributeError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
        raise RepositoryIntegrityError("AuditPreflightJob", job_id) from None


async def _require_job_record(
    session: AsyncSession,
    job_id: str,
) -> AuditPreflightJobRecord:
    record = await session.get(AuditPreflightJobRecord, job_id)
    if record is None:
        raise RepositoryConflictError("Audit Preflight Job no longer exists")
    return record


def _owner_binding_from_record(
    record: AuditPreflightJobRecord,
) -> AuditPreflightOwnerBinding:
    if record.schema_version != AUDIT_PREFLIGHT_JOB_SCHEMA_VERSION:
        raise ValueError("unsupported Audit Preflight Job schema")
    owner = AuditPreflightEffectOwner.model_validate(
        {
            "job_id": record.id,
            "operator_principal_id": record.operator_principal_id,
            "authorization_scope_digest": record.authorization_scope_digest,
            "source_node_id": record.source_node_id,
            "source_root_identity_digest": record.source_root_identity_digest,
            "request_schema_version": record.request_schema_version,
            "request_digest": record.request_digest,
            "backend_id": record.backend_id,
            "image_digest": record.image_digest,
            "policy_digest": record.policy_digest,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "effect_owner_digest": record.effect_owner_digest,
        }
    )
    status = AuditPreflightJobStatus(record.status)
    if record.state_version < 1:
        raise ValueError("invalid Audit Preflight Job state version")
    return AuditPreflightOwnerBinding(
        job_id=owner.job_id,
        operator_principal_id=owner.operator_principal_id,
        authorization_scope_digest=owner.authorization_scope_digest,
        request_schema_version=owner.request_schema_version,
        request_digest=owner.request_digest,
        source_node_id=owner.source_node_id,
        source_root_identity_digest=owner.source_root_identity_digest,
        backend_id=owner.backend_id,
        image_digest=owner.image_digest,
        policy_digest=owner.policy_digest,
        status=status,
        state_version=record.state_version,
        effect_owner_digest=owner.effect_owner_digest,
        plan_issuance_schema_version=record.plan_issuance_schema_version,
    )


def _require_exact_create_replay(
    existing: AuditPreflightJob,
    requested: AuditPreflightJob,
) -> AuditPreflightJob:
    if audit_preflight_is_exact_replay(
        existing,
        operator_principal_id=requested.operator_principal_id,
        client_request_id=requested.client_request_id,
        authorization_scope_digest=requested.authorization_scope_digest,
        request_schema_version=requested.request_schema_version,
        request_digest=requested.request_digest,
    ):
        return existing
    raise RepositoryConflictError(
        "Audit Preflight client request is bound to different immutable facts"
    )


def _replace_job(job: AuditPreflightJob, **updates: object) -> AuditPreflightJob:
    payload = job.model_dump(mode="python")
    payload.update(updates)
    return AuditPreflightJob.model_validate(payload)


def _validate_cas_request(
    previous: AuditPreflightJob,
    updated: AuditPreflightJob,
) -> bool:
    immutable_fields = (
        "job_id",
        "client_request_id",
        "operator_principal_id",
        "authorization_scope_digest",
        "request_schema_version",
        "request_digest",
        "restricted_request_json",
        "source_node_id",
        "source_root_identity_digest",
        "backend_id",
        "image_digest",
        "policy_digest",
        "canonical_empty_context_id",
        "canonical_empty_context_digest",
        "effect_owner_digest",
        "expires_at",
        "created_at",
    )
    if any(getattr(previous, field) != getattr(updated, field) for field in immutable_fields):
        raise ValueError("Audit Preflight compare-and-set changed immutable ownership")
    if previous == updated:
        if previous.status not in _TERMINAL_STATUSES:
            raise ValueError("only terminal Audit Preflight facts support exact replay")
        return True
    if updated.state_version != previous.state_version + 1:
        raise ValueError("Audit Preflight compare-and-set must advance one state version")
    if previous.status in _TERMINAL_STATUSES:
        raise ValueError("terminal Audit Preflight facts are immutable")
    if updated.status is not previous.status:
        previous.validate_transition_to(updated.status)
    if updated.updated_at < previous.updated_at:
        raise ValueError("Audit Preflight updated_at must be monotonic")
    if updated.attempt != previous.attempt:
        raise ValueError("Audit Preflight attempt may change only during claim")

    _validate_lease_mutation(previous, updated)
    for field in (
        "capsule_prepare_proof_digest",
        "started_at",
        "result_schema_version",
        "result_json",
        "result_digest",
        "safe_error_code",
        "never_created_proof_digest",
        "exit_receipt_digest",
        "stop_receipt_digest",
        "finished_at",
    ):
        previous_value = getattr(previous, field)
        updated_value = getattr(updated, field)
        if previous_value is not None and previous_value != updated_value:
            raise ValueError(f"Audit Preflight {field} is append-only")
    return False


def _validate_lease_mutation(
    previous: AuditPreflightJob,
    updated: AuditPreflightJob,
) -> None:
    immutable_lease_fields = (
        "lease_id",
        "lease_owner_instance_id",
        "lease_owner_epoch",
        "lease_output_contract_digest",
        "capsule_id",
    )
    if previous.lease_id is None:
        if any(
            getattr(previous, field) != getattr(updated, field) for field in immutable_lease_fields
        ):
            raise ValueError("Audit Preflight lease creation is restricted to claim")
        if any(
            getattr(updated, field) is not None
            for field in (
                "lease_expires_at",
                "lease_expected_state_version",
                "lease_envelope_digest",
            )
        ):
            raise ValueError("Audit Preflight lease creation is restricted to claim")
        return

    if any(getattr(previous, field) != getattr(updated, field) for field in immutable_lease_fields):
        raise ValueError("Audit Preflight lease and capsule identity are immutable")
    assert previous.lease_expires_at is not None
    assert updated.lease_expires_at is not None
    if updated.lease_expires_at < previous.lease_expires_at:
        raise ValueError("Audit Preflight lease expiry cannot move backwards")
    envelope_changed = (
        updated.lease_envelope_digest != previous.lease_envelope_digest
        or updated.lease_expected_state_version != previous.lease_expected_state_version
        or updated.lease_expires_at != previous.lease_expires_at
    )
    if envelope_changed and updated.lease_expected_state_version != updated.state_version:
        raise ValueError("renewed Audit Preflight lease must bind the new state version")


async def _persist_result(
    session: AsyncSession,
    updated: AuditPreflightJob,
    result: AuditPreflightResult | None,
) -> None:
    existing = await session.scalar(
        select(AuditPreflightResultRecord).where(
            AuditPreflightResultRecord.job_id == updated.job_id
        )
    )
    if result is None:
        if updated.result_digest is not None and existing is None:
            raise RepositoryConflictError("Audit Preflight result body is missing")
        return
    canonical = result.canonical_json()
    _validate_result_binding(updated, result)
    if (
        updated.result_digest != result.result_digest
        or updated.result_json != canonical
        or updated.result_schema_version != result.schema_version
    ):
        raise ValueError("Audit Preflight result does not match the terminal Job")
    if existing is not None:
        if existing.result_digest == result.result_digest and existing.canonical_json == canonical:
            return
        raise RepositoryConflictError("Audit Preflight result already differs")
    session.add(
        AuditPreflightResultRecord(
            id=new_id(),
            job_id=updated.job_id,
            schema_version=result.schema_version,
            canonical_json=canonical,
            result_digest=result.result_digest,
            created_at=result.completed_at,
        )
    )


async def _persist_exit_receipt(
    session: AsyncSession,
    updated: AuditPreflightJob,
    receipt: AuditPreflightExitReceipt | None,
) -> None:
    existing = await session.scalar(
        select(AuditPreflightExitReceiptRecord).where(
            AuditPreflightExitReceiptRecord.job_id == updated.job_id
        )
    )
    if receipt is None:
        if updated.exit_receipt_digest is not None and existing is None:
            raise RepositoryConflictError("Audit Preflight exit receipt body is missing")
        return
    canonical = receipt.canonical_json()
    _validate_exit_receipt_binding(updated, receipt)
    if receipt.job_id != updated.job_id or receipt.receipt_digest != updated.exit_receipt_digest:
        raise ValueError("Audit Preflight exit receipt does not match the terminal Job")
    if existing is not None:
        if (
            existing.receipt_digest == receipt.receipt_digest
            and existing.canonical_json == canonical
        ):
            return
        raise RepositoryConflictError("Audit Preflight exit receipt already differs")
    session.add(
        AuditPreflightExitReceiptRecord(
            id=new_id(),
            job_id=updated.job_id,
            schema_version=AUDIT_PREFLIGHT_EXIT_RECEIPT_SCHEMA_VERSION,
            canonical_json=canonical,
            receipt_digest=receipt.receipt_digest,
            received_at=receipt.received_at,
        )
    )


async def _persist_stop_receipt(
    session: AsyncSession,
    updated: AuditPreflightJob,
    receipt: AuditPreflightStopReceipt | None,
) -> None:
    existing = await session.scalar(
        select(AuditPreflightStopReceiptRecord).where(
            AuditPreflightStopReceiptRecord.job_id == updated.job_id
        )
    )
    if receipt is None:
        if updated.stop_receipt_digest is not None and existing is None:
            raise RepositoryConflictError("Audit Preflight stop receipt body is missing")
        return
    canonical = receipt.canonical_json()
    _validate_stop_receipt_binding(updated, receipt)
    if receipt.job_id != updated.job_id or receipt.receipt_digest != updated.stop_receipt_digest:
        raise ValueError("Audit Preflight stop receipt does not match the terminal Job")
    if existing is not None:
        if (
            existing.receipt_digest == receipt.receipt_digest
            and existing.canonical_json == canonical
        ):
            return
        raise RepositoryConflictError("Audit Preflight stop receipt already differs")
    session.add(
        AuditPreflightStopReceiptRecord(
            id=new_id(),
            job_id=updated.job_id,
            schema_version=AUDIT_PREFLIGHT_STOP_RECEIPT_SCHEMA_VERSION,
            disposition=receipt.disposition.value,
            canonical_json=canonical,
            receipt_digest=receipt.receipt_digest,
            received_at=receipt.received_at,
        )
    )


def _validate_result_binding(
    job: AuditPreflightJob,
    result: AuditPreflightResult,
) -> None:
    if (
        result.preflight_job_id != job.job_id
        or result.request_digest != job.request_digest
        or result.effect_owner_digest != job.effect_owner_digest
        or result.source_node_id != job.source_node_id
        or result.source_root_identity_digest != job.source_root_identity_digest
        or result.backend_id != job.backend_id
        or result.image_digest != job.image_digest
        or result.policy_digest != job.policy_digest
        or result.capsule_prepare_proof_digest != job.capsule_prepare_proof_digest
        or result.result_digest != job.result_digest
        or result.completed_at != job.finished_at
        or result.expires_at > job.expires_at
    ):
        raise ValueError("Audit Preflight result does not bind the durable Job")


def _validate_exit_receipt_binding(
    job: AuditPreflightJob,
    receipt: AuditPreflightExitReceipt,
) -> None:
    expected_terminal = {
        AuditPreflightJobStatus.SUCCEEDED: AuditPreflightExitTerminalState.SUCCEEDED,
        AuditPreflightJobStatus.REJECTED: AuditPreflightExitTerminalState.REJECTED,
        AuditPreflightJobStatus.FAILED: AuditPreflightExitTerminalState.FAILED,
    }.get(job.status)
    if job.status is AuditPreflightJobStatus.OUTCOME_UNKNOWN:
        expected_terminal = receipt.terminal_state
    if (
        expected_terminal is None
        or receipt.terminal_state is not expected_terminal
        or receipt.job_id != job.job_id
        or receipt.effect_owner_digest != job.effect_owner_digest
        or receipt.lease_envelope_digest != job.lease_envelope_digest
        or receipt.capsule_id != job.capsule_id
        or receipt.source_node_id != job.source_node_id
        or receipt.runner_principal.instance_id != job.lease_owner_instance_id
        or receipt.runner_principal.epoch != job.lease_owner_epoch
        or receipt.backend_id != job.backend_id
        or receipt.image_digest != job.image_digest
        or receipt.policy_digest != job.policy_digest
        or receipt.result_digest != job.result_digest
        or receipt.receipt_digest != job.exit_receipt_digest
        or not job.created_at <= receipt.received_at <= job.updated_at
    ):
        raise ValueError("Audit Preflight exit receipt does not bind the durable Job")


def _validate_stop_receipt_binding(
    job: AuditPreflightJob,
    receipt: AuditPreflightStopReceipt,
) -> None:
    if (
        receipt.job_id != job.job_id
        or receipt.effect_owner_digest != job.effect_owner_digest
        or receipt.lease_envelope_digest != job.lease_envelope_digest
        or receipt.source_node_id != job.source_node_id
        or receipt.runner_principal.instance_id != job.lease_owner_instance_id
        or receipt.runner_principal.epoch != job.lease_owner_epoch
        or receipt.backend_id != job.backend_id
        or receipt.image_digest != job.image_digest
        or receipt.policy_digest != job.policy_digest
        or receipt.receipt_digest != job.stop_receipt_digest
        or not job.created_at <= receipt.received_at <= job.updated_at
    ):
        raise ValueError("Audit Preflight stop receipt does not bind the durable Job")
    if receipt.disposition is AuditPreflightStopDisposition.STOPPED:
        if receipt.capsule_id != job.capsule_id or job.never_created_proof_digest is not None:
            raise ValueError("Audit Preflight stopped receipt has invalid capsule binding")
    elif receipt.never_created_proof_digest != job.never_created_proof_digest:
        raise ValueError("Audit Preflight never-created proof does not match the Job")


def _require_digest(value: str, *, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"Audit Preflight {label} must be a lower-hex SHA-256 digest")


def _require_aware_datetime(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Audit Preflight {label} must be timezone-aware")


__all__ = [
    "AuditPreflightExitReceiptRecord",
    "AuditPreflightJobRecord",
    "AuditPreflightJobRequestRecord",
    "AuditPreflightResultRecord",
    "AuditPreflightStopReceiptRecord",
    "SQLAlchemyAuditPreflightRepository",
]
