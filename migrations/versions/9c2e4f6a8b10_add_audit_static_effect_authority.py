"""add durable static effect and Snapshot mount authority

Revision ID: 9c2e4f6a8b10
Revises: 8a1f3c5e7b90
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import context, op

revision: str = "9c2e4f6a8b10"
down_revision: str | Sequence[str] | None = "8a1f3c5e7b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLAN_TABLE = "audit_static_effect_plans"
_LEASE_TABLE = "snapshot_mount_leases"
_PIN_TABLE = "snapshot_mount_pins"
_PROOF_TABLE = "snapshot_mount_stop_proofs"
_TABLES = (_PLAN_TABLE, _LEASE_TABLE, _PIN_TABLE, _PROOF_TABLE)


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def upgrade() -> None:
    with _serialized_schema_change():
        _upgrade()


def _upgrade() -> None:
    op.create_table(
        _PLAN_TABLE,
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_reference_role", sa.String(length=32), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("operation_family", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("backend_id", sa.String(length=64), nullable=False),
        sa.Column("backend_digest", sa.String(length=64), nullable=False),
        sa.Column("created_by_policy", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.audit-static-effect-plan/v1'",
            name="ck_audit_static_effect_plans_schema_version",
        ),
        sa.CheckConstraint(
            "operation_family IN ('snapshot_materialize', 'snapshot_mount')",
            name="ck_audit_static_effect_plans_operation_family",
        ),
        sa.CheckConstraint(
            "snapshot_reference_role = 'primary'",
            name="ck_audit_static_effect_plans_reference_role",
        ),
        sa.CheckConstraint(
            "backend_id = 'private_materialization'",
            name="ck_audit_static_effect_plans_backend",
        ),
        sa.CheckConstraint(
            "created_by_policy = 'riftx_policy'",
            name="ck_audit_static_effect_plans_policy_owner",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("plan_digest"),
            name="ck_audit_static_effect_plans_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("snapshot_digest")
            + " AND "
            + _lower_hex_digest_check("manifest_digest")
            + " AND "
            + _lower_hex_digest_check("backend_digest"),
            name="ck_audit_static_effect_plans_owner_digests",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_static_effect_plans_audit_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_audit_static_effect_plans_run_id_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_audit_static_effect_plans_snapshot_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id", "snapshot_id", "snapshot_reference_role"],
            [
                "snapshot_references.audit_id",
                "snapshot_references.snapshot_id",
                "snapshot_references.role",
            ],
            name="fk_audit_static_effect_plans_snapshot_reference",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["nodes.id"],
            name="fk_audit_static_effect_plans_node_id_nodes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_static_effect_plans"),
        sa.UniqueConstraint(
            "id",
            "plan_digest",
            name="uq_audit_static_effect_plans_id_digest",
        ),
    )
    op.create_index(
        "ix_audit_static_effect_plans_audit_family_created",
        _PLAN_TABLE,
        ["audit_id", "operation_family", "created_at", "id"],
    )

    op.create_table(
        _LEASE_TABLE,
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("lease_digest", sa.String(length=64), nullable=False),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("effect_execution_id", sa.String(length=128), nullable=False),
        sa.Column("target_runner_instance_id", sa.String(length=64), nullable=False),
        sa.Column("target_runner_epoch", sa.BigInteger(), nullable=False),
        sa.Column("target_node_id", sa.String(length=128), nullable=False),
        sa.Column("backend_id", sa.String(length=64), nullable=False),
        sa.Column("backend_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("mount_key", sa.String(length=128), nullable=True),
        sa.Column("mount_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("stop_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.snapshot-mount-lease/v1'",
            name="ck_snapshot_mount_leases_schema_version",
        ),
        sa.CheckConstraint(
            "status IN ('issued', 'active', 'revocation_pending', 'expiration_pending', "
            "'revoked', 'expired', 'outcome_unknown')",
            name="ck_snapshot_mount_leases_status",
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name="ck_snapshot_mount_leases_state_version",
        ),
        sa.CheckConstraint(
            "backend_id = 'private_materialization'",
            name="ck_snapshot_mount_leases_backend",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("lease_digest")
            + " AND "
            + _lower_hex_digest_check("nonce_hash")
            + " AND "
            + _lower_hex_digest_check("plan_digest")
            + " AND "
            + _lower_hex_digest_check("backend_digest"),
            name="ck_snapshot_mount_leases_digests",
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND updated_at >= created_at",
            name="ck_snapshot_mount_leases_timestamp_order",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "plan_digest"],
            ["audit_static_effect_plans.id", "audit_static_effect_plans.plan_digest"],
            name="fk_snapshot_mount_leases_plan_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_runner_instance_id"],
            ["runner_credentials.runner_instance_id"],
            name="fk_snapshot_mount_leases_runner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_snapshot_mount_leases"),
        sa.UniqueConstraint(
            "effect_execution_id",
            name="uq_snapshot_mount_leases_effect",
        ),
        sa.UniqueConstraint(
            "id",
            "lease_digest",
            name="uq_snapshot_mount_leases_id_digest",
        ),
    )
    op.create_index(
        "ix_snapshot_mount_leases_reconcile",
        _LEASE_TABLE,
        ["target_node_id", "status", "expires_at", "updated_at", "id"],
    )

    op.create_table(
        _PIN_TABLE,
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("pin_digest", sa.String(length=64), nullable=False),
        sa.Column("lease_id", sa.String(length=128), nullable=False),
        sa.Column("lease_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("effect_execution_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("backend_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("mount_key", sa.String(length=128), nullable=True),
        sa.Column("mount_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "schema_version = 'riftx.snapshot-mount-pin/v1'",
            name="ck_snapshot_mount_pins_schema_version",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revocation_pending', 'revoked')",
            name="ck_snapshot_mount_pins_status",
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name="ck_snapshot_mount_pins_state_version",
        ),
        sa.CheckConstraint(
            "backend_id = 'private_materialization'",
            name="ck_snapshot_mount_pins_backend",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("pin_digest")
            + " AND "
            + _lower_hex_digest_check("lease_digest")
            + " AND "
            + _lower_hex_digest_check("plan_digest"),
            name="ck_snapshot_mount_pins_digests",
        ),
        sa.ForeignKeyConstraint(
            ["lease_id", "lease_digest"],
            ["snapshot_mount_leases.id", "snapshot_mount_leases.lease_digest"],
            name="fk_snapshot_mount_pins_lease_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "plan_digest"],
            ["audit_static_effect_plans.id", "audit_static_effect_plans.plan_digest"],
            name="fk_snapshot_mount_pins_plan_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_snapshot_mount_pins"),
        sa.UniqueConstraint("lease_id", name="uq_snapshot_mount_pins_lease"),
        sa.UniqueConstraint(
            "id",
            "pin_digest",
            name="uq_snapshot_mount_pins_id_digest",
        ),
    )
    op.create_index(
        "ix_snapshot_mount_pins_status_updated",
        _PIN_TABLE,
        ["status", "updated_at", "id"],
    )

    op.create_table(
        _PROOF_TABLE,
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("proof_digest", sa.String(length=64), nullable=False),
        sa.Column("lease_id", sa.String(length=128), nullable=False),
        sa.Column("lease_digest", sa.String(length=64), nullable=False),
        sa.Column("pin_id", sa.String(length=128), nullable=False),
        sa.Column("pin_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("effect_execution_id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("backend_id", sa.String(length=64), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.snapshot-mount-stop-proof/v1'",
            name="ck_snapshot_mount_stop_proofs_schema_version",
        ),
        sa.CheckConstraint(
            "disposition IN ('revoked', 'expired')",
            name="ck_snapshot_mount_stop_proofs_disposition",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("proof_digest")
            + " AND "
            + _lower_hex_digest_check("lease_digest")
            + " AND "
            + _lower_hex_digest_check("pin_digest"),
            name="ck_snapshot_mount_stop_proofs_digests",
        ),
        sa.ForeignKeyConstraint(
            ["lease_id", "lease_digest"],
            ["snapshot_mount_leases.id", "snapshot_mount_leases.lease_digest"],
            name="fk_snapshot_mount_stop_proofs_lease_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pin_id", "pin_digest"],
            ["snapshot_mount_pins.id", "snapshot_mount_pins.pin_digest"],
            name="fk_snapshot_mount_stop_proofs_pin_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_snapshot_mount_stop_proofs"),
        sa.UniqueConstraint("lease_id", name="uq_snapshot_mount_stop_proofs_lease"),
        sa.UniqueConstraint("pin_id", name="uq_snapshot_mount_stop_proofs_pin"),
    )
    op.create_index(
        "ix_snapshot_mount_stop_proofs_stopped",
        _PROOF_TABLE,
        ["stopped_at", "id"],
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot prove that static effect authority facts are empty"
        )
    with _serialized_schema_change():
        _downgrade()


def _downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "LOCK TABLE snapshot_mount_stop_proofs, snapshot_mount_pins, "
            "snapshot_mount_leases, audit_static_effect_plans IN ACCESS EXCLUSIVE MODE"
        )
    elif connection.dialect.name != "sqlite":
        raise RuntimeError(
            "static effect authority downgrade cannot safely serialize dialect "
            f"{connection.dialect.name!r}"
        )
    for table_name in _TABLES:
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError(
                "cannot downgrade while durable static effect authority facts exist"
            )
    _require_safe_cross_boundary_downgrade(connection)
    op.drop_index("ix_snapshot_mount_stop_proofs_stopped", table_name=_PROOF_TABLE)
    op.drop_table(_PROOF_TABLE)
    op.drop_index("ix_snapshot_mount_pins_status_updated", table_name=_PIN_TABLE)
    op.drop_table(_PIN_TABLE)
    op.drop_index("ix_snapshot_mount_leases_reconcile", table_name=_LEASE_TABLE)
    op.drop_table(_LEASE_TABLE)
    op.drop_index(
        "ix_audit_static_effect_plans_audit_family_created",
        table_name=_PLAN_TABLE,
    )
    op.drop_table(_PLAN_TABLE)


def _require_safe_cross_boundary_downgrade(connection: sa.Connection) -> None:
    """Run lower-revision loss guards before this revision performs any DDL."""

    target = context.get_revision_argument()
    if target == down_revision:
        return
    if connection.execute(sa.text("SELECT 1 FROM snapshot_references LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while durable Snapshot reference facts exist"
        )
    if target == "5d8c1a7e3b24":
        return
    if connection.execute(sa.text("SELECT 1 FROM audit_preflight_plans LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while durable Audit Preflight Plan facts exist"
        )
    if connection.execute(
        sa.text(
            "SELECT 1 FROM audit_preflight_jobs "
            "WHERE plan_issuance_schema_version = "
            "'riftx.audit-preflight-plan-issuance/v1' LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "cannot downgrade while durable Audit Preflight facts exist: "
            "Plan-eligible Jobs remain"
        )
    if target == "2b7d9e4a6c10":
        return
    for table_name in (
        "audit_preflight_stop_receipts",
        "audit_preflight_exit_receipts",
        "audit_preflight_results",
        "audit_preflight_job_requests",
        "audit_preflight_jobs",
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError(
                "cannot downgrade while durable Audit Preflight facts exist"
            )


@contextmanager
def _serialized_schema_change() -> Iterator[None]:
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
                    "static effect authority migration introduced foreign-key violations: "
                    f"{violations!r}"
                )
            connection.exec_driver_sql("COMMIT")
            transaction_started = False
        except BaseException:
            if transaction_started:
                connection.exec_driver_sql("ROLLBACK")
            raise
