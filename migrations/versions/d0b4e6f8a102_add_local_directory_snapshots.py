"""add ordinary local-directory SourceSnapshot support

Revision ID: d0b4e6f8a102
Revises: 9c2e4f6a8b10
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import context, op

revision: str = "d0b4e6f8a102"
down_revision: str | Sequence[str] | None = "9c2e4f6a8b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _optional_git_object_id_check(column: str) -> str:
    sha1_remainder = column
    sha256_remainder = column
    for character in "0123456789abcdef":
        sha1_remainder = f"replace({sha1_remainder}, '{character}', '')"
        sha256_remainder = f"replace({sha256_remainder}, '{character}', '')"
    return (
        f"{column} IS NULL OR "
        f"(length({column}) = 40 AND length({sha1_remainder}) = 0) OR "
        f"(length({column}) = 64 AND length({sha256_remainder}) = 0)"
    )


def _git_object_id_check(column: str) -> str:
    return f"{column} IS NOT NULL AND ({_optional_git_object_id_check(column)})"


def upgrade() -> None:
    _reject_sqlite_offline()
    with _serialized_schema_change():
        with op.batch_alter_table("audit_projects") as batch:
            batch.drop_constraint("ck_audit_projects_vcs_kind", type_="check")
            batch.create_check_constraint(
                "ck_audit_projects_vcs_kind",
                "vcs_kind IN ('directory', 'git')",
            )
        with op.batch_alter_table("source_snapshots") as batch:
            batch.drop_constraint("ck_source_snapshots_source_kind", type_="check")
            batch.drop_constraint(
                "ck_source_snapshots_working_tree_digest",
                type_="check",
            )
            batch.drop_constraint("ck_source_snapshots_commit_sha", type_="check")
            batch.alter_column(
                "commit_sha",
                existing_type=sa.String(length=128),
                nullable=True,
            )
            batch.create_check_constraint(
                "ck_source_snapshots_source_kind",
                "source_kind IN ('directory', 'revision', 'working_tree')",
            )
            batch.create_check_constraint(
                "ck_source_snapshots_working_tree_digest",
                "(source_kind IN ('directory', 'revision') "
                "AND working_tree_digest IS NULL) OR "
                "(source_kind = 'working_tree' AND working_tree_digest IS NOT NULL)",
            )
            batch.create_check_constraint(
                "ck_source_snapshots_commit_presence",
                "(source_kind = 'directory' AND commit_sha IS NULL) OR "
                "(source_kind IN ('revision', 'working_tree') AND commit_sha IS NOT NULL)",
            )
            batch.create_check_constraint(
                "ck_source_snapshots_commit_sha",
                _optional_git_object_id_check("commit_sha"),
            )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot prove that local-directory facts are absent"
        )
    with _serialized_schema_change():
        connection = op.get_bind()
        _require_safe_cross_boundary_downgrade(connection)
        if connection.execute(
            sa.text("SELECT 1 FROM source_snapshots WHERE source_kind = 'directory' LIMIT 1")
        ).first():
            raise RuntimeError(
                "cannot downgrade while local-directory SourceSnapshot facts exist"
            )
        if connection.execute(
            sa.text("SELECT 1 FROM audit_projects WHERE vcs_kind = 'directory' LIMIT 1")
        ).first():
            raise RuntimeError(
                "cannot downgrade while local-directory AuditProject facts exist"
            )
        with op.batch_alter_table("source_snapshots") as batch:
            batch.drop_constraint("ck_source_snapshots_commit_sha", type_="check")
            batch.drop_constraint("ck_source_snapshots_commit_presence", type_="check")
            batch.drop_constraint(
                "ck_source_snapshots_working_tree_digest",
                type_="check",
            )
            batch.drop_constraint("ck_source_snapshots_source_kind", type_="check")
            batch.alter_column(
                "commit_sha",
                existing_type=sa.String(length=128),
                nullable=False,
            )
            batch.create_check_constraint(
                "ck_source_snapshots_source_kind",
                "source_kind IN ('revision', 'working_tree')",
            )
            batch.create_check_constraint(
                "ck_source_snapshots_working_tree_digest",
                "(source_kind = 'revision' AND working_tree_digest IS NULL) OR "
                "(source_kind = 'working_tree' AND working_tree_digest IS NOT NULL)",
            )
            batch.create_check_constraint(
                "ck_source_snapshots_commit_sha",
                _git_object_id_check("commit_sha"),
            )
        with op.batch_alter_table("audit_projects") as batch:
            batch.drop_constraint("ck_audit_projects_vcs_kind", type_="check")
            batch.create_check_constraint(
                "ck_audit_projects_vcs_kind",
                "vcs_kind = 'git'",
            )


def _require_safe_cross_boundary_downgrade(connection: sa.Connection) -> None:
    """Run lower-revision loss guards before this revision performs any DDL."""

    target = context.get_revision_argument()
    if target == down_revision:
        return
    for table_name in (
        "audit_static_effect_plans",
        "snapshot_mount_leases",
        "snapshot_mount_pins",
        "snapshot_mount_stop_proofs",
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError(
                "cannot downgrade while durable static effect authority facts exist"
            )
    if target == "8a1f3c5e7b90":
        return
    if connection.execute(sa.text("SELECT 1 FROM snapshot_references LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while durable Snapshot reference facts exist")
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
    capability = connection.execute(
        sa.text(
            "SELECT 1 FROM runner_credentials "
            "WHERE CAST(protocol_capabilities_json AS TEXT) "
            "LIKE '%\"preflight\\_job\\_owner\\_v1\"%' ESCAPE '\\' LIMIT 1"
        )
    ).first()
    if capability is not None:
        raise RuntimeError(
            "cannot downgrade while Audit Preflight Runner capability facts exist"
        )
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
    if target == "4f9a6c1d2e30":
        return
    if connection.execute(
        sa.text("SELECT 1 FROM workflow_signal_intents LIMIT 1")
    ).first():
        raise RuntimeError(
            "cannot downgrade while durable Workflow signal intents exist"
        )
    if target == "8d7c2e4f1a90":
        return
    _require_safe_runner_ownership_downgrade(connection)


def _require_safe_runner_ownership_downgrade(connection: sa.Connection) -> None:
    unsafe = connection.execute(
        sa.text(
            "SELECT 1 FROM runner_command_ownerships WHERE "
            "verification_state <> 'quarantined' OR schema_version IS NOT NULL OR "
            "effect_binding_id IS NOT NULL OR operation IS NOT NULL OR "
            "operation_family IS NOT NULL OR payload_digest IS NOT NULL OR "
            "output_contract_json IS NOT NULL OR output_contract_digest IS NOT NULL OR "
            "envelope_digest IS NOT NULL OR reconciliation_state <> 'untouched' OR "
            "replacement_command_id IS NOT NULL LIMIT 1"
        )
    ).first()
    if unsafe is not None:
        raise RuntimeError(
            "cannot downgrade Runner ownership after verified or reconciled ownership facts exist"
        )
    if connection.execute(
        sa.text("SELECT 1 FROM runner_commands WHERE state_version <> 0 LIMIT 1")
    ).first():
        raise RuntimeError(
            "cannot downgrade Runner ownership while post-migration command state exists"
        )
    if connection.execute(
        sa.text(
            "SELECT 1 FROM runner_credentials "
            "WHERE protocol_capabilities_json IS NOT NULL "
            "AND CAST(protocol_capabilities_json AS TEXT) NOT IN ('[]', 'null') LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "cannot downgrade Runner ownership while protocol capability facts exist"
        )
    for table_name in (
        "runner_effect_bindings",
        "runner_stop_receipts",
        "runner_stop_projections",
    ):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first():
            raise RuntimeError(
                "cannot downgrade Runner ownership while effect or stop proof facts exist"
            )
    if connection.execute(
        sa.text(
            "SELECT 1 FROM executions "
            "WHERE audit_id IS NOT NULL OR plan_digest IS NOT NULL OR "
            "runner_command_id IS NOT NULL OR runner_effect_binding_id IS NOT NULL OR "
            "runner_binding_digest IS NOT NULL OR runner_envelope_digest IS NOT NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "cannot downgrade Runner ownership while Audit Execution bindings exist"
        )


def _reject_sqlite_offline() -> None:
    if context.is_offline_mode() and op.get_context().dialect.name == "sqlite":
        raise RuntimeError("SQLite local-directory migration requires an online database")


@contextmanager
def _serialized_schema_change() -> Iterator[None]:
    migration_context = op.get_context()
    if migration_context.dialect.name != "sqlite" or migration_context.as_sql:
        yield
        return
    connection = op.get_bind()
    with migration_context.autocommit_block():
        transaction_started = False
        foreign_keys_enabled = bool(
            connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
        )
        try:
            if foreign_keys_enabled:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            transaction_started = True
            yield
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "local-directory schema change introduced foreign-key violations"
                )
            connection.exec_driver_sql("COMMIT")
            transaction_started = False
            if foreign_keys_enabled:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                if not connection.exec_driver_sql("PRAGMA foreign_keys").scalar():
                    raise RuntimeError("failed to restore SQLite foreign-key enforcement")
        except BaseException:
            if transaction_started:
                connection.exec_driver_sql("ROLLBACK")
                transaction_started = False
            if foreign_keys_enabled:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            raise
        finally:
            if transaction_started:
                connection.exec_driver_sql("ROLLBACK")
            if foreign_keys_enabled and not connection.exec_driver_sql(
                "PRAGMA foreign_keys"
            ).scalar():
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
