"""add durable pre-Audit source-ingest jobs

Revision ID: 2b7d9e4a6c10
Revises: 4f9a6c1d2e30
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import context, op

revision: str = "2b7d9e4a6c10"
down_revision: str | Sequence[str] | None = "4f9a6c1d2e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_TABLE = "audit_preflight_jobs"
_REQUEST_TABLE = "audit_preflight_job_requests"
_RESULT_TABLE = "audit_preflight_results"
_EXIT_RECEIPT_TABLE = "audit_preflight_exit_receipts"
_STOP_RECEIPT_TABLE = "audit_preflight_stop_receipts"


def upgrade() -> None:
    with _serialized_preflight_schema_change():
        _upgrade()


def _upgrade() -> None:
    _acquire_upgrade_lock()
    op.create_table(
        _JOB_TABLE,
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("client_request_id", sa.String(length=36), nullable=False),
        sa.Column("operator_principal_id", sa.String(length=128), nullable=False),
        sa.Column("authorization_scope_digest", sa.String(length=64), nullable=False),
        sa.Column("request_schema_version", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("source_node_id", sa.String(length=64), nullable=False),
        sa.Column("source_root_identity_digest", sa.String(length=64), nullable=False),
        sa.Column("backend_id", sa.String(length=128), nullable=False),
        sa.Column("image_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("canonical_empty_context_id", sa.String(length=128), nullable=False),
        sa.Column("canonical_empty_context_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("effect_owner_digest", sa.String(length=64), nullable=False),
        sa.Column("capsule_id", sa.String(length=128), nullable=True),
        sa.Column("capsule_prepare_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("lease_id", sa.String(length=128), nullable=True),
        sa.Column("lease_owner_instance_id", sa.String(length=64), nullable=True),
        sa.Column("lease_owner_epoch", sa.BigInteger(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expected_state_version", sa.BigInteger(), nullable=True),
        sa.Column("lease_output_contract_digest", sa.String(length=64), nullable=True),
        sa.Column("lease_envelope_digest", sa.String(length=64), nullable=True),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("exit_receipt_digest", sa.String(length=64), nullable=True),
        sa.Column("safe_error_code", sa.String(length=128), nullable=True),
        sa.Column("never_created_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("stop_receipt_digest", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.audit-preflight-job/v1'",
            name="ck_audit_preflight_jobs_schema",
        ),
        sa.CheckConstraint(
            "request_schema_version = 'riftx.audit-preflight-request/v1'",
            name="ck_audit_preflight_jobs_request_schema",
        ),
        sa.CheckConstraint(
            "source_node_id = 'local'",
            name="ck_audit_preflight_jobs_local_node",
        ),
        sa.CheckConstraint(
            "canonical_empty_context_id = 'riftx.audit-empty-security-context/v1'",
            name="ck_audit_preflight_jobs_empty_context",
        ),
        sa.CheckConstraint(
            _canonical_uuid_check("client_request_id"),
            name="ck_audit_preflight_jobs_client_request_id",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'running', 'succeeded', 'rejected', "
            "'failed', 'cancelling', 'cancelled', 'outcome_unknown')",
            name="ck_audit_preflight_jobs_status",
        ),
        sa.CheckConstraint(
            "state_version >= 1 AND attempt >= 0",
            name="ck_audit_preflight_jobs_versions",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "status <> 'pending' OR attempt = 0",
            name="ck_audit_preflight_jobs_pending_shape",
        ),
        sa.CheckConstraint(
            "status NOT IN ('claimed', 'running', 'outcome_unknown') OR attempt >= 1",
            name="ck_audit_preflight_jobs_active_lease",
        ),
        sa.CheckConstraint(
            "status NOT IN ('claimed', 'running') OR lease_expires_at > updated_at",
            name="ck_audit_preflight_jobs_active_lease_expiry",
        ),
        sa.CheckConstraint(
            "capsule_prepare_proof_digest IS NULL OR capsule_id IS NOT NULL",
            name="ck_audit_preflight_jobs_prepare_capsule",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR capsule_id IS NOT NULL",
            name="ck_audit_preflight_jobs_started_capsule",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND result_digest IS NOT NULL "
            "AND exit_receipt_digest IS NOT NULL AND safe_error_code IS NULL "
            "AND never_created_proof_digest IS NULL AND stop_receipt_digest IS NULL "
            "AND capsule_prepare_proof_digest IS NOT NULL AND started_at IS NOT NULL) "
            "OR status <> 'succeeded'",
            name="ck_audit_preflight_jobs_succeeded_shape",
        ),
        sa.CheckConstraint(
            "(status = 'rejected' AND result_digest IS NOT NULL "
            "AND exit_receipt_digest IS NOT NULL AND safe_error_code IS NOT NULL "
            "AND never_created_proof_digest IS NULL AND stop_receipt_digest IS NULL "
            "AND capsule_prepare_proof_digest IS NOT NULL AND started_at IS NOT NULL) "
            "OR status <> 'rejected'",
            name="ck_audit_preflight_jobs_rejected_shape",
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND result_digest IS NULL "
            "AND safe_error_code IS NOT NULL AND finished_at IS NOT NULL "
            "AND (exit_receipt_digest IS NOT NULL OR stop_receipt_digest IS NOT NULL "
            "OR never_created_proof_digest IS NOT NULL)) OR status <> 'failed'",
            name="ck_audit_preflight_jobs_failed_proof",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND result_digest IS NULL "
            "AND exit_receipt_digest IS NULL AND finished_at IS NOT NULL "
            "AND (stop_receipt_digest IS NOT NULL "
            "OR never_created_proof_digest IS NOT NULL)) OR status <> 'cancelled'",
            name="ck_audit_preflight_jobs_cancelled_proof",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'rejected') OR result_digest IS NULL",
            name="ck_audit_preflight_jobs_result_status",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'rejected', 'failed', 'outcome_unknown') "
            "OR exit_receipt_digest IS NULL",
            name="ck_audit_preflight_jobs_exit_receipt_status",
        ),
        sa.CheckConstraint(
            "exit_receipt_digest IS NULL OR "
            "(stop_receipt_digest IS NULL AND never_created_proof_digest IS NULL)",
            name="ck_audit_preflight_jobs_terminal_proof_exclusive",
        ),
        sa.CheckConstraint(
            "status IN ('cancelling', 'cancelled', 'failed', 'outcome_unknown') "
            "OR (stop_receipt_digest IS NULL AND never_created_proof_digest IS NULL)",
            name="ck_audit_preflight_jobs_stop_proof_status",
        ),
        sa.CheckConstraint(
            "status NOT IN ('pending', 'claimed', 'running') OR safe_error_code IS NULL",
            name="ck_audit_preflight_jobs_active_error",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'rejected', 'failed', 'cancelled') "
            "AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('succeeded', 'rejected', 'failed', 'cancelled') "
            "AND finished_at IS NULL)",
            name="ck_audit_preflight_jobs_finished_state",
        ),
        sa.CheckConstraint(
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
            sa.CheckConstraint(
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
            sa.CheckConstraint(
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operator_principal_id",
            "client_request_id",
            name="uq_audit_preflight_jobs_client_request",
        ),
    )
    op.create_index(
        "ix_audit_preflight_jobs_dispatch",
        _JOB_TABLE,
        ["source_node_id", "status", "expires_at", "created_at", "id"],
    )
    op.create_index(
        "ix_audit_preflight_jobs_lease",
        _JOB_TABLE,
        ["status", "lease_expires_at", "updated_at", "id"],
    )
    op.create_index(
        "ix_audit_preflight_jobs_reconcile",
        _JOB_TABLE,
        ["status", "updated_at", "id"],
    )

    op.create_table(
        _REQUEST_TABLE,
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.audit-preflight-request/v1'",
            name="ck_audit_preflight_job_requests_schema",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("request_digest"),
            name="ck_audit_preflight_job_requests_digest",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["audit_preflight_jobs.id"],
            name="fk_audit_preflight_job_requests_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )

    op.create_table(
        _RESULT_TABLE,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("result_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.audit-preflight-result/v1'",
            name="ck_audit_preflight_results_schema",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("result_digest"),
            name="ck_audit_preflight_results_digest",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["audit_preflight_jobs.id"],
            name="fk_audit_preflight_results_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_audit_preflight_results_job"),
        sa.UniqueConstraint("result_digest", name="uq_audit_preflight_results_digest"),
    )

    op.create_table(
        _EXIT_RECEIPT_TABLE,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("receipt_digest", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.audit-preflight-exit-receipt/v1'",
            name="ck_audit_preflight_exit_receipts_schema",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("receipt_digest"),
            name="ck_audit_preflight_exit_receipts_digest",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["audit_preflight_jobs.id"],
            name="fk_audit_preflight_exit_receipts_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_audit_preflight_exit_receipts_job"),
        sa.UniqueConstraint(
            "receipt_digest",
            name="uq_audit_preflight_exit_receipts_digest",
        ),
    )

    op.create_table(
        _STOP_RECEIPT_TABLE,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("receipt_digest", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.audit-preflight-stop-receipt/v1'",
            name="ck_audit_preflight_stop_receipts_schema",
        ),
        sa.CheckConstraint(
            "disposition IN ('stopped', 'never_created')",
            name="ck_audit_preflight_stop_receipts_disposition",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("receipt_digest"),
            name="ck_audit_preflight_stop_receipts_digest",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["audit_preflight_jobs.id"],
            name="fk_audit_preflight_stop_receipts_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_audit_preflight_stop_receipts_job"),
        sa.UniqueConstraint(
            "receipt_digest",
            name="uq_audit_preflight_stop_receipts_digest",
        ),
    )


def _acquire_upgrade_lock() -> None:
    """Serialize capability publication before the dedicated fact tables exist."""

    migration_context = op.get_context()
    dialect_name = migration_context.dialect.name
    if dialect_name == "sqlite":
        # The schema-change wrapper already owns BEGIN EXCLUSIVE.
        return
    if dialect_name != "postgresql":
        raise RuntimeError(f"Audit Preflight migration does not support dialect {dialect_name!r}")

    statement = "LOCK TABLE runner_credentials IN ACCESS EXCLUSIVE MODE"
    if migration_context.as_sql:
        op.execute(sa.text(statement))
    else:
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("offline downgrade cannot prove that Audit Preflight facts are empty")
    with _serialized_preflight_schema_change():
        _downgrade()


def _downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "LOCK TABLE runner_credentials, audit_preflight_stop_receipts, "
            "audit_preflight_exit_receipts, audit_preflight_results, "
            "audit_preflight_job_requests, audit_preflight_jobs "
            "IN ACCESS EXCLUSIVE MODE"
        )
    elif connection.dialect.name != "sqlite":
        raise RuntimeError(
            f"Audit Preflight downgrade cannot safely serialize dialect {connection.dialect.name!r}"
        )
    capability = connection.execute(
        sa.text(
            "SELECT 1 FROM runner_credentials "
            "WHERE CAST(protocol_capabilities_json AS TEXT) "
            "LIKE '%\"preflight\\_job\\_owner\\_v1\"%' ESCAPE '\\' LIMIT 1"
        )
    ).first()
    if capability is not None:
        raise RuntimeError("cannot downgrade while Audit Preflight Runner capability facts exist")
    for table in (
        _STOP_RECEIPT_TABLE,
        _EXIT_RECEIPT_TABLE,
        _RESULT_TABLE,
        _REQUEST_TABLE,
        _JOB_TABLE,
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table}" LIMIT 1')).first():
            raise RuntimeError("cannot downgrade while durable Audit Preflight facts exist")
    op.drop_table(_STOP_RECEIPT_TABLE)
    op.drop_table(_EXIT_RECEIPT_TABLE)
    op.drop_table(_RESULT_TABLE)
    op.drop_table(_REQUEST_TABLE)
    op.drop_table(_JOB_TABLE)


@contextmanager
def _serialized_preflight_schema_change() -> Iterator[None]:
    """Own SQLite validation and DDL with one exclusive transaction."""

    migration_context = op.get_context()
    if migration_context.dialect.name != "sqlite" or migration_context.as_sql:
        yield
        return

    connection = op.get_bind()
    with migration_context.autocommit_block():
        transaction_started = False
        try:
            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            transaction_started = True
            yield
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "Audit Preflight schema change introduced foreign-key violations"
                )
            connection.exec_driver_sql("COMMIT")
            transaction_started = False
        except BaseException:
            if transaction_started:
                connection.exec_driver_sql("ROLLBACK")
                transaction_started = False
            raise
        finally:
            if transaction_started:
                connection.exec_driver_sql("ROLLBACK")


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
