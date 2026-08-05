"""add atomic Code Audit draft request facts

Revision ID: 7c4e1a9b2d06
Revises: 3b7f1d9e5a02
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "7c4e1a9b2d06"
down_revision: str | Sequence[str] | None = "3b7f1d9e5a02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLIENT_REQUEST_TABLE = "audit_client_requests"


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


def upgrade() -> None:
    _require_no_legacy_audits()
    op.create_table(
        _CLIENT_REQUEST_TABLE,
        sa.Column("client_request_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("request_schema_version", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("engagement_id", sa.String(length=64), nullable=False),
        sa.Column("contract_id", sa.String(length=128), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("temporal_workflow_id", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _canonical_uuid_check("client_request_id"),
            name="ck_audit_client_requests_canonical_id",
        ),
        sa.CheckConstraint(
            "operation = 'create_draft'",
            name="ck_audit_client_requests_operation",
        ),
        sa.CheckConstraint(
            "request_schema_version = 'riftx.audit-create-draft-request/v1'",
            name="ck_audit_client_requests_schema",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("request_digest"),
            name="ck_audit_client_requests_request_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("contract_digest"),
            name="ck_audit_client_requests_contract_digest",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id", "run_id", "contract_digest", "temporal_workflow_id"],
            [
                "audit_scans.id",
                "audit_scans.run_id",
                "audit_scans.contract_digest",
                "audit_scans.temporal_workflow_id",
            ],
            name="fk_audit_client_requests_scan_start_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_client_requests_scan_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "engagement_id"],
            ["audit_projects.id", "audit_projects.engagement_id"],
            name="fk_audit_client_requests_project_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["contract_id", "audit_id", "contract_digest"],
            [
                "audit_contracts.contract_id",
                "audit_contracts.audit_id",
                "audit_contracts.contract_digest",
            ],
            name="fk_audit_client_requests_contract_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("client_request_id"),
        sa.UniqueConstraint("audit_id", name="uq_audit_client_requests_audit"),
        sa.UniqueConstraint("run_id", name="uq_audit_client_requests_run"),
        sa.UniqueConstraint("contract_id", name="uq_audit_client_requests_contract"),
    )
    op.create_index(
        "ix_audit_client_requests_project_created_id",
        _CLIENT_REQUEST_TABLE,
        ["project_id", "created_at", "client_request_id"],
        unique=False,
    )


def _require_no_legacy_audits() -> None:
    context = op.get_context()
    if context.as_sql:
        if context.dialect.name != "postgresql":
            raise RuntimeError(
                "Code Audit request upgrade cannot prove legacy Audit compatibility "
                f"offline for database dialect {context.dialect.name!r}"
            )
        op.execute(sa.text('LOCK TABLE "audit_scans" IN ACCESS EXCLUSIVE MODE'))
        op.execute(
            sa.text(
                "DO $riftx$ BEGIN "
                "IF EXISTS (SELECT 1 FROM audit_scans LIMIT 1) THEN "
                "RAISE EXCEPTION 'cannot add Code Audit request persistence while "
                "legacy Audit facts exist'; "
                "END IF; END $riftx$"
            )
        )
        return

    connection = op.get_bind()
    _serialize_legacy_audit_check(connection)
    if connection.execute(sa.text('SELECT 1 FROM "audit_scans" LIMIT 1')).first() is not None:
        raise RuntimeError(
            "cannot add Code Audit request persistence while legacy Audit facts exist; "
            "an explicit compatibility migration is required"
        )


def _serialize_legacy_audit_check(connection: Connection) -> None:
    if not connection.in_transaction():
        raise RuntimeError(
            "Code Audit request upgrade requires one active transaction for "
            "serialization, verification, and DDL"
        )
    dialect_name = connection.dialect.name
    if dialect_name == "postgresql":
        connection.exec_driver_sql('LOCK TABLE "audit_scans" IN ACCESS EXCLUSIVE MODE')
        return
    if dialect_name == "sqlite":
        connection.exec_driver_sql(
            'UPDATE "audit_scans" SET "state_version" = "state_version" WHERE 1 = 0'
        )
        return
    raise RuntimeError(
        "Code Audit request upgrade cannot safely serialize legacy Audit writes for "
        f"database dialect {dialect_name!r}"
    )


def downgrade() -> None:
    _require_empty_client_requests()
    op.drop_index(
        "ix_audit_client_requests_project_created_id",
        table_name=_CLIENT_REQUEST_TABLE,
    )
    op.drop_table(_CLIENT_REQUEST_TABLE)


def _require_empty_client_requests() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "Code Audit request downgrade requires an online database to prove "
            "that no idempotency facts would be lost"
        )
    connection = op.get_bind()
    _serialize_downgrade(connection)
    if (
        connection.execute(sa.text(f'SELECT 1 FROM "{_CLIENT_REQUEST_TABLE}" LIMIT 1')).first()
        is not None
    ):
        raise RuntimeError(
            "cannot downgrade Code Audit request persistence while client-request facts exist"
        )


def _serialize_downgrade(connection: Connection) -> None:
    if not connection.in_transaction():
        raise RuntimeError(
            "Code Audit request downgrade requires one active transaction for "
            "serialization, verification, and DDL"
        )
    dialect_name = connection.dialect.name
    if dialect_name == "postgresql":
        connection.exec_driver_sql(f'LOCK TABLE "{_CLIENT_REQUEST_TABLE}" IN ACCESS EXCLUSIVE MODE')
        return
    if dialect_name == "sqlite":
        connection.exec_driver_sql(
            f'UPDATE "{_CLIENT_REQUEST_TABLE}" SET "operation" = "operation" WHERE 1 = 0'
        )
        return
    raise RuntimeError(
        "Code Audit request downgrade cannot safely serialize writes for "
        f"database dialect {dialect_name!r}"
    )
