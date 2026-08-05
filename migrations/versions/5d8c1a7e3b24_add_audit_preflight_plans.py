"""add signed Audit Preflight Plans and issuance eligibility

Revision ID: 5d8c1a7e3b24
Revises: 2b7d9e4a6c10
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import context, op

revision: str = "5d8c1a7e3b24"
down_revision: str | Sequence[str] | None = "2b7d9e4a6c10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_TABLE = "audit_preflight_jobs"
_PLAN_TABLE = "audit_preflight_plans"
_BINDING_TABLE = "audit_security_context_bindings"
_PLAN_ISSUANCE_SCHEMA_VERSION = "riftx.audit-preflight-plan-issuance/v1"
_MAX_PLAN_BYTES = 256 * 1_024
_MAX_PLAN_COUNTER = 2**63 - 1


def upgrade() -> None:
    with _serialized_plan_schema_change():
        _upgrade()


def _upgrade() -> None:
    _acquire_upgrade_lock()
    # Intentionally nullable with no backfill. Jobs created before this migration
    # stay ineligible and must be rerun instead of receiving a retroactive token.
    op.add_column(
        _JOB_TABLE,
        sa.Column("plan_issuance_schema_version", sa.String(length=64), nullable=True),
    )
    op.create_table(
        _PLAN_TABLE,
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("preflight_job_id", sa.String(length=128), nullable=False),
        sa.Column("preflight_client_request_id", sa.String(length=36), nullable=False),
        sa.Column("operator_principal_id", sa.String(length=128), nullable=False),
        sa.Column("authorization_scope_digest", sa.String(length=64), nullable=False),
        sa.Column("request_schema_version", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("result_schema_version", sa.String(length=64), nullable=False),
        sa.Column("result_digest", sa.String(length=64), nullable=False),
        sa.Column("effect_owner_digest", sa.String(length=64), nullable=False),
        sa.Column("source_node_id", sa.String(length=64), nullable=False),
        sa.Column("source_root_identity_digest", sa.String(length=64), nullable=False),
        sa.Column("repository_identity_digest", sa.String(length=64), nullable=False),
        sa.Column("content_identity_digest", sa.String(length=64), nullable=False),
        sa.Column("backend_id", sa.String(length=128), nullable=False),
        sa.Column("image_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("capsule_prepare_proof_digest", sa.String(length=64), nullable=False),
        sa.Column("target_digest", sa.String(length=64), nullable=False),
        sa.Column("scope_digest", sa.String(length=64), nullable=False),
        sa.Column("capability_matrix_digest", sa.String(length=64), nullable=False),
        sa.Column("minimum_feasible_budget_digest", sa.String(length=64), nullable=False),
        sa.Column("security_context_id", sa.String(length=128), nullable=False),
        sa.Column("security_context_digest", sa.String(length=64), nullable=False),
        sa.Column("preflight_completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_verifier_schema_version", sa.String(length=64), nullable=False),
        sa.Column("token_key_id", sa.String(length=64), nullable=False),
        sa.Column("token_nonce", sa.String(length=43), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("reserved_audit_id", sa.String(length=128), nullable=True),
        sa.Column("reserved_client_request_id", sa.String(length=36), nullable=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_audit_id", sa.String(length=128), nullable=True),
        sa.Column("consumed_start_request_id", sa.String(length=36), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(length=128), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.audit-preflight-plan/v1'",
            name="ck_audit_preflight_plans_schema",
        ),
        sa.CheckConstraint(
            "request_schema_version = 'riftx.audit-preflight-request/v1'",
            name="ck_audit_preflight_plans_request_schema",
        ),
        sa.CheckConstraint(
            "result_schema_version = 'riftx.audit-preflight-result/v1'",
            name="ck_audit_preflight_plans_result_schema",
        ),
        sa.CheckConstraint(
            "token_verifier_schema_version = "
            "'riftx.audit-preflight-token-verifier/v1'",
            name="ck_audit_preflight_plans_token_verifier_schema",
        ),
        sa.CheckConstraint(
            "source_node_id = 'local'",
            name="ck_audit_preflight_plans_local_node",
        ),
        sa.CheckConstraint(
            "security_context_id = 'riftx.audit-empty-security-context/v1'",
            name="ck_audit_preflight_plans_empty_context",
        ),
        sa.CheckConstraint(
            "status IN ('available', 'reserved', 'consumed', 'revoked')",
            name="ck_audit_preflight_plans_status",
        ),
        sa.CheckConstraint(
            f"state_version BETWEEN 1 AND {_MAX_PLAN_COUNTER}",
            name="ck_audit_preflight_plans_state_version",
        ),
        sa.CheckConstraint(
            f"length(canonical_json) BETWEEN 2 AND {_MAX_PLAN_BYTES}",
            name="ck_audit_preflight_plans_canonical_size",
        ),
        sa.CheckConstraint(
            "length(token_key_id) BETWEEN 1 AND 64 "
            "AND token_key_id = trim(token_key_id)",
            name="ck_audit_preflight_plans_token_key_id",
        ),
        sa.CheckConstraint(
            "length(token_nonce) = 43",
            name="ck_audit_preflight_plans_token_nonce",
        ),
        sa.CheckConstraint(
            _canonical_uuid_check("preflight_client_request_id"),
            name="ck_audit_preflight_plans_preflight_request_id",
        ),
        sa.CheckConstraint(
            _optional_canonical_uuid_check("reserved_client_request_id"),
            name="ck_audit_preflight_plans_reserved_request_id",
        ),
        sa.CheckConstraint(
            _optional_canonical_uuid_check("consumed_start_request_id"),
            name="ck_audit_preflight_plans_consumed_request_id",
        ),
        sa.CheckConstraint(
            "(reserved_audit_id IS NULL AND reserved_client_request_id IS NULL "
            "AND reserved_at IS NULL) OR "
            "(reserved_audit_id IS NOT NULL AND reserved_client_request_id IS NOT NULL "
            "AND reserved_at IS NOT NULL)",
            name="ck_audit_preflight_plans_reservation_shape",
        ),
        sa.CheckConstraint(
            "(consumed_audit_id IS NULL AND consumed_start_request_id IS NULL "
            "AND consumed_at IS NULL) OR "
            "(consumed_audit_id IS NOT NULL AND consumed_start_request_id IS NOT NULL "
            "AND consumed_at IS NOT NULL)",
            name="ck_audit_preflight_plans_consumption_shape",
        ),
        sa.CheckConstraint(
            "(revocation_reason IS NULL AND revoked_at IS NULL) OR "
            "(revocation_reason IS NOT NULL AND revoked_at IS NOT NULL)",
            name="ck_audit_preflight_plans_revocation_shape",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
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
            sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["preflight_job_id"],
            ["audit_preflight_jobs.id"],
            name="fk_audit_preflight_plans_preflight_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "preflight_job_id",
            name="uq_audit_preflight_plans_preflight_job",
        ),
        sa.UniqueConstraint(
            "plan_digest",
            name="uq_audit_preflight_plans_plan_digest",
        ),
        sa.UniqueConstraint(
            "token_hash",
            name="uq_audit_preflight_plans_token_hash",
        ),
        sa.UniqueConstraint(
            "reserved_audit_id",
            name="uq_audit_preflight_plans_reserved_audit",
        ),
        sa.UniqueConstraint(
            "consumed_audit_id",
            name="uq_audit_preflight_plans_consumed_audit",
        ),
        sa.UniqueConstraint(
            "id",
            "plan_digest",
            "operator_principal_id",
            "authorization_scope_digest",
            "security_context_id",
            "security_context_digest",
            "reserved_audit_id",
            name="uq_audit_preflight_plans_context_binding",
        ),
    )
    op.create_index(
        "ix_audit_preflight_plans_owner",
        _PLAN_TABLE,
        [
            "operator_principal_id",
            "authorization_scope_digest",
            "status",
            "expires_at",
            "id",
        ],
    )
    op.create_index(
        "ix_audit_preflight_plans_key_lifecycle",
        _PLAN_TABLE,
        ["token_key_id", "status", "expires_at", "id"],
    )
    _upgrade_create_v2_tables()


def _upgrade_create_v2_tables() -> None:
    with op.batch_alter_table("audit_scans") as batch:
        batch.alter_column("required_backend_id", existing_type=sa.String(length=256), nullable=True)
        batch.alter_column("policy_digest", existing_type=sa.String(length=64), nullable=True)
        batch.alter_column("config_digest", existing_type=sa.String(length=64), nullable=True)

    with op.batch_alter_table("audit_contracts") as batch:
        batch.drop_constraint("ck_audit_contracts_schema_version", type_="check")
        batch.drop_constraint("ck_audit_contracts_hydration_digest", type_="check")
        batch.alter_column("selected_node_id", existing_type=sa.String(length=64), nullable=True)
        batch.alter_column(
            "required_backend_id", existing_type=sa.String(length=256), nullable=True
        )
        batch.alter_column(
            "snapshot_hydration_policy_digest",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch.add_column(sa.Column("preflight_plan_id", sa.String(length=128)))
        batch.add_column(sa.Column("preflight_plan_digest", sa.String(length=64)))
        batch.add_column(sa.Column("security_context_bundle_id", sa.String(length=128)))
        batch.add_column(
            sa.Column("security_context_bundle_digest", sa.String(length=64))
        )
        batch.create_check_constraint(
            "ck_audit_contracts_schema_version",
            "schema_version IN ('riftx.audit-contract/v1', 'riftx.audit-contract/v2')",
        )
        batch.create_check_constraint(
            "ck_audit_contracts_hydration_digest",
            _optional_lower_hex_digest_check("snapshot_hydration_policy_digest"),
        )
        batch.create_check_constraint(
            "ck_audit_contracts_preflight_plan_digest",
            _optional_lower_hex_digest_check("preflight_plan_digest"),
        )
        batch.create_check_constraint(
            "ck_audit_contracts_security_context_digest",
            _optional_lower_hex_digest_check("security_context_bundle_digest"),
        )
        batch.create_check_constraint(
            "ck_audit_contracts_version_shape",
            "(schema_version = 'riftx.audit-contract/v1' "
            "AND selected_node_id IS NOT NULL AND required_backend_id IS NOT NULL "
            "AND snapshot_hydration_policy_digest IS NOT NULL "
            "AND preflight_plan_id IS NULL AND preflight_plan_digest IS NULL "
            "AND security_context_bundle_id IS NULL "
            "AND security_context_bundle_digest IS NULL) OR "
            "(schema_version = 'riftx.audit-contract/v2' "
            "AND selected_node_id IS NULL AND required_backend_id IS NULL "
            "AND snapshot_hydration_policy_digest IS NULL "
            "AND preflight_plan_id IS NOT NULL AND preflight_plan_digest IS NOT NULL "
            "AND security_context_bundle_id = 'riftx.audit-empty-security-context/v1' "
            "AND security_context_bundle_digest IS NOT NULL)",
        )

    with op.batch_alter_table("audit_client_requests") as batch:
        batch.drop_constraint("ck_audit_client_requests_schema", type_="check")
        batch.add_column(sa.Column("preflight_plan_id", sa.String(length=128)))
        batch.add_column(sa.Column("preflight_plan_digest", sa.String(length=64)))
        batch.add_column(sa.Column("security_context_id", sa.String(length=128)))
        batch.add_column(sa.Column("security_context_digest", sa.String(length=64)))
        batch.add_column(sa.Column("contract_stage", sa.String(length=64)))
        batch.create_check_constraint(
            "ck_audit_client_requests_schema",
            "request_schema_version IN ('riftx.audit-create-draft-request/v1', "
            "'riftx.audit-create-draft-request/v2')",
        )
        batch.create_check_constraint(
            "ck_audit_client_requests_preflight_plan_digest",
            _optional_lower_hex_digest_check("preflight_plan_digest"),
        )
        batch.create_check_constraint(
            "ck_audit_client_requests_security_context_digest",
            _optional_lower_hex_digest_check("security_context_digest"),
        )
        batch.create_check_constraint(
            "ck_audit_client_requests_version_shape",
            "(request_schema_version = 'riftx.audit-create-draft-request/v1' "
            "AND preflight_plan_id IS NULL AND preflight_plan_digest IS NULL "
            "AND security_context_id IS NULL AND security_context_digest IS NULL "
            "AND contract_stage IS NULL) OR "
            "(request_schema_version = 'riftx.audit-create-draft-request/v2' "
            "AND preflight_plan_id IS NOT NULL AND preflight_plan_digest IS NOT NULL "
            "AND security_context_id = 'riftx.audit-empty-security-context/v1' "
            "AND security_context_digest IS NOT NULL "
            "AND contract_stage = 'preflight_bound_draft')",
        )

    op.create_table(
        _BINDING_TABLE,
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("preflight_plan_id", sa.String(length=128), nullable=False),
        sa.Column("preflight_plan_digest", sa.String(length=64), nullable=False),
        sa.Column("operator_principal_id", sa.String(length=128), nullable=False),
        sa.Column("authorization_scope_digest", sa.String(length=64), nullable=False),
        sa.Column("security_context_bundle_id", sa.String(length=128), nullable=False),
        sa.Column("security_context_bundle_digest", sa.String(length=64), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audit_scans.id"],
            name="fk_audit_security_context_bindings_audit",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "preflight_plan_id",
                "preflight_plan_digest",
                "operator_principal_id",
                "authorization_scope_digest",
                "security_context_bundle_id",
                "security_context_bundle_digest",
                "audit_id",
            ],
            [
                "audit_preflight_plans.id",
                "audit_preflight_plans.plan_digest",
                "audit_preflight_plans.operator_principal_id",
                "audit_preflight_plans.authorization_scope_digest",
                "audit_preflight_plans.security_context_id",
                "audit_preflight_plans.security_context_digest",
                "audit_preflight_plans.reserved_audit_id",
            ],
            name="fk_audit_security_context_bindings_plan",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "schema_version = 'riftx.audit-security-context-binding/v2'",
            name="ck_audit_security_context_bindings_schema",
        ),
        sa.CheckConstraint(
            "security_context_bundle_id = 'riftx.audit-empty-security-context/v1'",
            name="ck_audit_security_context_bindings_empty_context",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("preflight_plan_digest"),
            name="ck_audit_security_context_bindings_plan_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("authorization_scope_digest"),
            name="ck_audit_security_context_bindings_authorization_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("security_context_bundle_digest"),
            name="ck_audit_security_context_bindings_context_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("binding_digest"),
            name="ck_audit_security_context_bindings_binding_digest",
        ),
        sa.PrimaryKeyConstraint("audit_id"),
        sa.UniqueConstraint(
            "preflight_plan_id", name="uq_audit_security_context_bindings_plan"
        ),
    )


def _acquire_upgrade_lock() -> None:
    migration_context = op.get_context()
    dialect_name = migration_context.dialect.name
    if dialect_name == "sqlite":
        return
    if dialect_name != "postgresql":
        raise RuntimeError(
            f"Audit Preflight Plan migration does not support dialect {dialect_name!r}"
        )
    statement = "LOCK TABLE audit_preflight_jobs IN ACCESS EXCLUSIVE MODE"
    if migration_context.as_sql:
        op.execute(sa.text(statement))
    else:
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot prove that Audit Preflight Plan facts are empty"
        )
    with _serialized_plan_schema_change():
        _downgrade()


def _downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "LOCK TABLE audit_preflight_plans, audit_preflight_jobs "
            "IN ACCESS EXCLUSIVE MODE"
        )
    elif connection.dialect.name != "sqlite":
        raise RuntimeError(
            "Audit Preflight Plan downgrade cannot safely serialize dialect "
            f"{connection.dialect.name!r}"
        )
    if connection.execute(sa.text(f'SELECT 1 FROM "{_PLAN_TABLE}" LIMIT 1')).first():
        raise RuntimeError("cannot downgrade while durable Audit Preflight Plan facts exist")
    if connection.execute(
        sa.text(
            f'SELECT 1 FROM "{_JOB_TABLE}" '
            "WHERE plan_issuance_schema_version = :schema_version LIMIT 1"
        ),
        {"schema_version": _PLAN_ISSUANCE_SCHEMA_VERSION},
    ).first():
        raise RuntimeError(
            "cannot downgrade while durable Audit Preflight facts exist: "
            "Plan-eligible Jobs remain"
        )
    _downgrade_create_v2_tables()
    op.drop_table(_PLAN_TABLE)
    op.drop_column(_JOB_TABLE, "plan_issuance_schema_version")


def _downgrade_create_v2_tables() -> None:
    op.drop_table(_BINDING_TABLE)
    with op.batch_alter_table("audit_client_requests") as batch:
        batch.drop_constraint("ck_audit_client_requests_version_shape", type_="check")
        batch.drop_constraint(
            "ck_audit_client_requests_security_context_digest", type_="check"
        )
        batch.drop_constraint(
            "ck_audit_client_requests_preflight_plan_digest", type_="check"
        )
        batch.drop_constraint("ck_audit_client_requests_schema", type_="check")
        batch.drop_column("contract_stage")
        batch.drop_column("security_context_digest")
        batch.drop_column("security_context_id")
        batch.drop_column("preflight_plan_digest")
        batch.drop_column("preflight_plan_id")
        batch.create_check_constraint(
            "ck_audit_client_requests_schema",
            "request_schema_version = 'riftx.audit-create-draft-request/v1'",
        )

    with op.batch_alter_table("audit_contracts") as batch:
        batch.drop_constraint("ck_audit_contracts_version_shape", type_="check")
        batch.drop_constraint("ck_audit_contracts_security_context_digest", type_="check")
        batch.drop_constraint("ck_audit_contracts_preflight_plan_digest", type_="check")
        batch.drop_constraint("ck_audit_contracts_hydration_digest", type_="check")
        batch.drop_constraint("ck_audit_contracts_schema_version", type_="check")
        batch.drop_column("security_context_bundle_digest")
        batch.drop_column("security_context_bundle_id")
        batch.drop_column("preflight_plan_digest")
        batch.drop_column("preflight_plan_id")
        batch.alter_column(
            "snapshot_hydration_policy_digest",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.alter_column(
            "required_backend_id", existing_type=sa.String(length=256), nullable=False
        )
        batch.alter_column("selected_node_id", existing_type=sa.String(length=64), nullable=False)
        batch.create_check_constraint(
            "ck_audit_contracts_schema_version",
            "schema_version = 'riftx.audit-contract/v1'",
        )
        batch.create_check_constraint(
            "ck_audit_contracts_hydration_digest",
            _lower_hex_digest_check("snapshot_hydration_policy_digest"),
        )

    with op.batch_alter_table("audit_scans") as batch:
        batch.alter_column("config_digest", existing_type=sa.String(length=64), nullable=False)
        batch.alter_column("policy_digest", existing_type=sa.String(length=64), nullable=False)
        batch.alter_column(
            "required_backend_id", existing_type=sa.String(length=256), nullable=False
        )


@contextmanager
def _serialized_plan_schema_change() -> Iterator[None]:
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
                    "Audit Preflight Plan schema change introduced foreign-key violations"
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


def _optional_canonical_uuid_check(column: str) -> str:
    return f"{column} IS NULL OR ({_canonical_uuid_check(column)})"
