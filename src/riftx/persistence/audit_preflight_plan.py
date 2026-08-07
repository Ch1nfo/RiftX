"""Restricted durable persistence for Code Audit Preflight Plans.

The table stores one canonical immutable Plan payload plus redundant bounded
owner/digest columns.  Token verifier material and lifecycle facts live in
separate columns so token-hash admission never has to materialize repository
paths or the canonical Plan body.
"""

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

MAX_AUDIT_PREFLIGHT_PLAN_BYTES = 256 * 1_024
MAX_PLAN_COUNTER = 2**63 - 1


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


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


def _optional_canonical_uuid_check(column: str) -> str:
    return f"{column} IS NULL OR ({_canonical_uuid_check(column)})"


class AuditPreflightPlanRecord(Base):
    __tablename__ = "audit_preflight_plans"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'riftx.audit-preflight-plan/v1'",
            name="ck_audit_preflight_plans_schema",
        ),
        CheckConstraint(
            "request_schema_version = 'riftx.audit-preflight-request/v1'",
            name="ck_audit_preflight_plans_request_schema",
        ),
        CheckConstraint(
            "result_schema_version = 'riftx.audit-preflight-result/v1'",
            name="ck_audit_preflight_plans_result_schema",
        ),
        CheckConstraint(
            "token_verifier_schema_version = "
            "'riftx.audit-preflight-token-verifier/v1'",
            name="ck_audit_preflight_plans_token_verifier_schema",
        ),
        CheckConstraint(
            "source_node_id = 'local'",
            name="ck_audit_preflight_plans_local_node",
        ),
        CheckConstraint(
            "security_context_id = 'riftx.audit-empty-security-context/v1'",
            name="ck_audit_preflight_plans_empty_context",
        ),
        CheckConstraint(
            "status IN ('available', 'reserved', 'consumed', 'revoked')",
            name="ck_audit_preflight_plans_status",
        ),
        CheckConstraint(
            f"state_version BETWEEN 1 AND {MAX_PLAN_COUNTER}",
            name="ck_audit_preflight_plans_state_version",
        ),
        CheckConstraint(
            f"length(canonical_json) BETWEEN 2 AND {MAX_AUDIT_PREFLIGHT_PLAN_BYTES}",
            name="ck_audit_preflight_plans_canonical_size",
        ),
        CheckConstraint(
            "length(token_key_id) BETWEEN 1 AND 64 "
            "AND token_key_id = trim(token_key_id)",
            name="ck_audit_preflight_plans_token_key_id",
        ),
        CheckConstraint(
            "length(token_nonce) = 43",
            name="ck_audit_preflight_plans_token_nonce",
        ),
        CheckConstraint(
            _canonical_uuid_check("preflight_client_request_id"),
            name="ck_audit_preflight_plans_preflight_request_id",
        ),
        CheckConstraint(
            _optional_canonical_uuid_check("reserved_client_request_id"),
            name="ck_audit_preflight_plans_reserved_request_id",
        ),
        CheckConstraint(
            _optional_canonical_uuid_check("consumed_start_request_id"),
            name="ck_audit_preflight_plans_consumed_request_id",
        ),
        CheckConstraint(
            "(reserved_audit_id IS NULL AND reserved_client_request_id IS NULL "
            "AND reserved_at IS NULL) OR "
            "(reserved_audit_id IS NOT NULL AND reserved_client_request_id IS NOT NULL "
            "AND reserved_at IS NOT NULL)",
            name="ck_audit_preflight_plans_reservation_shape",
        ),
        CheckConstraint(
            "(consumed_audit_id IS NULL AND consumed_start_request_id IS NULL "
            "AND consumed_at IS NULL) OR "
            "(consumed_audit_id IS NOT NULL AND consumed_start_request_id IS NOT NULL "
            "AND consumed_at IS NOT NULL)",
            name="ck_audit_preflight_plans_consumption_shape",
        ),
        CheckConstraint(
            "(revocation_reason IS NULL AND revoked_at IS NULL) OR "
            "(revocation_reason IS NOT NULL AND revoked_at IS NOT NULL)",
            name="ck_audit_preflight_plans_revocation_shape",
        ),
        CheckConstraint(
            "(status = 'available' AND state_version = 1 "
            "AND reserved_audit_id IS NULL AND consumed_audit_id IS NULL "
            "AND revocation_reason IS NULL AND updated_at = created_at) OR "
            "(status = 'reserved' AND state_version = 2 "
            "AND reserved_audit_id IS NOT NULL AND consumed_audit_id IS NULL "
            "AND revocation_reason IS NULL AND updated_at = reserved_at) OR "
            "(status = 'consumed' AND state_version = 3 "
            "AND reserved_audit_id IS NOT NULL AND consumed_audit_id IS NOT NULL "
            "AND consumed_audit_id = reserved_audit_id "
            "AND revocation_reason IS NULL AND updated_at = consumed_at) OR "
            "(status = 'revoked' AND consumed_audit_id IS NULL "
            "AND revocation_reason IS NOT NULL AND updated_at = revoked_at "
            "AND ((reserved_audit_id IS NULL AND state_version = 2) OR "
            "(reserved_audit_id IS NOT NULL AND state_version = 3)))",
            name="ck_audit_preflight_plans_lifecycle",
        ),
        CheckConstraint(
            "preflight_completed_at <= created_at AND created_at < expires_at "
            "AND updated_at >= created_at "
            "AND (reserved_at IS NULL OR "
            "(reserved_at >= created_at AND reserved_at < expires_at)) "
            "AND (consumed_at IS NULL OR "
            "(consumed_at >= reserved_at AND consumed_at < expires_at)) "
            "AND (revoked_at IS NULL OR "
            "(revoked_at >= created_at AND "
            "(reserved_at IS NULL OR revoked_at >= reserved_at)))",
            name="ck_audit_preflight_plans_timestamps",
        ),
        *(
            CheckConstraint(
                _lower_hex_digest_check(column),
                name=f"ck_audit_preflight_plans_{column}",
            )
            for column in (
                "plan_digest",
                "authorization_scope_digest",
                "request_digest",
                "result_digest",
                "effect_owner_digest",
                "source_root_identity_digest",
                "repository_identity_digest",
                "content_identity_digest",
                "image_digest",
                "policy_digest",
                "capsule_prepare_proof_digest",
                "target_digest",
                "scope_digest",
                "capability_matrix_digest",
                "minimum_feasible_budget_digest",
                "security_context_digest",
                "token_hash",
            )
        ),
        UniqueConstraint(
            "preflight_job_id",
            name="uq_audit_preflight_plans_preflight_job",
        ),
        UniqueConstraint(
            "plan_digest",
            name="uq_audit_preflight_plans_plan_digest",
        ),
        UniqueConstraint(
            "token_hash",
            name="uq_audit_preflight_plans_token_hash",
        ),
        UniqueConstraint(
            "reserved_audit_id",
            name="uq_audit_preflight_plans_reserved_audit",
        ),
        UniqueConstraint(
            "consumed_audit_id",
            name="uq_audit_preflight_plans_consumed_audit",
        ),
        UniqueConstraint(
            "id",
            "plan_digest",
            "operator_principal_id",
            "authorization_scope_digest",
            "security_context_id",
            "security_context_digest",
            "reserved_audit_id",
            name="uq_audit_preflight_plans_context_binding",
        ),
        Index(
            "ix_audit_preflight_plans_owner",
            "operator_principal_id",
            "authorization_scope_digest",
            "status",
            "expires_at",
            "id",
        ),
        Index(
            "ix_audit_preflight_plans_key_lifecycle",
            "token_key_id",
            "status",
            "expires_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    preflight_job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "audit_preflight_jobs.id",
            name="fk_audit_preflight_plans_preflight_job",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    preflight_client_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operator_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    authorization_scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_owner_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_root_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    repository_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    backend_id: Mapped[str] = mapped_column(String(128), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    capsule_prepare_proof_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_matrix_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    minimum_feasible_budget_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    security_context_id: Mapped[str] = mapped_column(String(128), nullable=False)
    security_context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    preflight_completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    token_verifier_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    token_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    token_nonce: Mapped[str] = mapped_column(String(43), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_audit_id: Mapped[str | None] = mapped_column(String(128))
    reserved_client_request_id: Mapped[str | None] = mapped_column(String(36))
    reserved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consumed_audit_id: Mapped[str | None] = mapped_column(String(128))
    consumed_start_request_id: Mapped[str | None] = mapped_column(String(36))
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revocation_reason: Mapped[str | None] = mapped_column(String(128))
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
