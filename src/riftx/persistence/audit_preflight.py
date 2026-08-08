"""Historical Audit Preflight ORM records retained for schema compatibility."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .orm import Base
from .types import UTCDateTime


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
