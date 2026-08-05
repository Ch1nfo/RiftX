"""add durable owner-bound Snapshot references

Revision ID: 8a1f3c5e7b90
Revises: 5d8c1a7e3b24
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import context, op

revision: str = "8a1f3c5e7b90"
down_revision: str | Sequence[str] | None = "5d8c1a7e3b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "snapshot_references"


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
        _TABLE,
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("reference_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.snapshot-reference/v1'",
            name="ck_snapshot_references_schema_version",
        ),
        sa.CheckConstraint(
            "role IN ('primary', 'base', 'baseline', 'finding_evidence', "
            "'retest_parent', 'distribution_revision')",
            name="ck_snapshot_references_role",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("reference_digest"),
            name="ck_snapshot_references_digest",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_snapshot_references_audit_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_snapshot_references_snapshot_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "audit_id",
            "snapshot_id",
            "role",
            name="pk_snapshot_references",
        ),
    )
    op.create_index(
        "ix_snapshot_references_snapshot_project_created",
        _TABLE,
        ["snapshot_id", "project_id", "created_at"],
    )
    op.create_index(
        "ix_snapshot_references_audit_created",
        _TABLE,
        ["audit_id", "created_at"],
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot prove that Snapshot reference facts are empty"
        )
    with _serialized_schema_change():
        _downgrade()


def _downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "LOCK TABLE snapshot_references, audit_preflight_plans, "
            "audit_preflight_jobs IN ACCESS EXCLUSIVE MODE"
        )
    elif connection.dialect.name != "sqlite":
        raise RuntimeError(
            "Snapshot reference downgrade cannot safely serialize dialect "
            f"{connection.dialect.name!r}"
        )
    # Preserve the repository's existing cross-migration no-partial-DDL
    # guarantee. If this command is crossing the AUD-201 boundary, run the
    # next migration's irreversible-fact guards before dropping this table.
    if context.get_revision_argument() != down_revision:
        if connection.execute(
            sa.text('SELECT 1 FROM "audit_preflight_plans" LIMIT 1')
        ).first():
            raise RuntimeError(
                "cannot downgrade while durable Audit Preflight Plan facts exist"
            )
        if connection.execute(
            sa.text(
                'SELECT 1 FROM "audit_preflight_jobs" '
                "WHERE plan_issuance_schema_version = "
                "'riftx.audit-preflight-plan-issuance/v1' LIMIT 1"
            )
        ).first():
            raise RuntimeError(
                "cannot downgrade while durable Audit Preflight facts exist: "
                "Plan-eligible Jobs remain"
            )
    if connection.execute(sa.text(f'SELECT 1 FROM "{_TABLE}" LIMIT 1')).first():
        raise RuntimeError(
            "cannot downgrade while durable Snapshot reference facts exist"
        )
    op.drop_index("ix_snapshot_references_audit_created", table_name=_TABLE)
    op.drop_index(
        "ix_snapshot_references_snapshot_project_created",
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)


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
                    "Snapshot reference schema change introduced foreign-key violations"
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
