"""partition Artifact access and persist immutable storage provenance

Revision ID: 91e6f4a2c8b7
Revises: 7c4e1a9b2d06
Create Date: 2026-08-03
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "91e6f4a2c8b7"
down_revision: str | Sequence[str] | None = "7c4e1a9b2d06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "artifacts"
_ACCESS_INDEX = "ix_artifacts_public_run_created_id"
_AUDIT_INDEX = "ix_artifacts_audit_run_execution_created_id"
_FOREIGN_KEY = "fk_artifacts_audit"
_EXECUTION_FOREIGN_KEY = "fk_artifacts_execution"
_SQLITE_LEGACY_EXECUTION_FOREIGN_KEY = "fk_artifacts_execution_id_executions"
_POSTGRESQL_LEGACY_EXECUTION_FOREIGN_KEY = "artifacts_execution_id_fkey"
_BATCH_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}
_CHECKS = (
    "ck_artifacts_access_class",
    "ck_artifacts_content_trust",
    "ck_artifacts_owner_access",
    "ck_artifacts_canonical_storage_key",
    "ck_artifacts_safe_storage_components",
    "ck_artifacts_safe_mime_type",
    "ck_artifacts_sha256",
    "ck_artifacts_nonnegative_size",
)
_PROVENANCE_SCHEMA = "riftx.artifact-ingest-provenance/v1"
_LEGACY_PROVENANCE = {
    "schema_version": _PROVENANCE_SCHEMA,
    "method": "legacy_migrated",
    "producer_node_id": None,
    "producer_execution_id": None,
}
_LEGACY_PROVENANCE_JSON = json.dumps(
    _LEGACY_PROVENANCE,
    separators=(",", ":"),
    ensure_ascii=True,
)


def upgrade() -> None:
    with _serialized_artifact_schema_change():
        _require_compatible_legacy_artifacts()
        op.add_column(
            _TABLE,
            sa.Column("audit_id", sa.String(length=128), nullable=True),
        )
        op.add_column(
            _TABLE,
            sa.Column("access_class", sa.String(length=32), nullable=True),
        )
        op.add_column(
            _TABLE,
            sa.Column("content_trust", sa.String(length=32), nullable=True),
        )
        op.add_column(
            _TABLE,
            sa.Column("storage_key", sa.Text(), nullable=True),
        )
        op.add_column(
            _TABLE,
            sa.Column("ingest_provenance_json", sa.JSON(), nullable=True),
        )
        _backfill_legacy_artifacts()
        _install_artifact_contract()


def _backfill_legacy_artifacts() -> None:
    provenance = _LEGACY_PROVENANCE_JSON.replace("'", "''").replace(":", r"\:")
    op.execute(
        sa.text(
            "UPDATE artifacts SET "
            "audit_id = NULL, "
            "access_class = 'public_export', "
            "content_trust = 'untrusted_tool_output', "
            "storage_key = 'runs/' || run_id || '/artifacts/' || id || '/' || name, "
            f"ingest_provenance_json = '{provenance}'"
        )
    )


def _install_artifact_contract() -> None:
    dialect_name = op.get_context().dialect.name
    with op.batch_alter_table(
        _TABLE,
        naming_convention=_BATCH_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.alter_column(
            "access_class",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "content_trust",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "storage_key",
            existing_type=sa.Text(),
            nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "ingest_provenance_json",
            existing_type=sa.JSON(),
            nullable=False,
            server_default=None,
        )
        batch_op.create_foreign_key(
            _FOREIGN_KEY,
            "audit_scans",
            ["audit_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.drop_constraint(
            _legacy_execution_foreign_key_name(dialect_name),
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            _EXECUTION_FOREIGN_KEY,
            "executions",
            ["execution_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_artifacts_access_class",
            "access_class IN ('public_export', 'audit_internal', 'restricted_sensitive')",
        )
        batch_op.create_check_constraint(
            "ck_artifacts_content_trust",
            "content_trust IN ('generated', 'untrusted_source', 'untrusted_tool_output')",
        )
        batch_op.create_check_constraint(
            "ck_artifacts_owner_access",
            "audit_id IS NOT NULL OR access_class = 'public_export'",
        )
        batch_op.create_check_constraint(
            "ck_artifacts_canonical_storage_key",
            "storage_key = 'runs/' || run_id || '/artifacts/' || id || '/' || name",
        )
        batch_op.create_check_constraint(
            "ck_artifacts_safe_storage_components",
            _safe_storage_components_sql(dialect_name),
        )
        batch_op.create_check_constraint(
            "ck_artifacts_safe_mime_type",
            _safe_mime_type_sql(dialect_name),
        )
        batch_op.create_check_constraint(
            "ck_artifacts_sha256",
            _lower_hex_digest_check("sha256"),
        )
        batch_op.create_check_constraint(
            "ck_artifacts_nonnegative_size",
            "size >= 0",
        )
        batch_op.create_index(
            _ACCESS_INDEX,
            ["run_id", "access_class", "created_at", "id"],
            unique=False,
        )
        batch_op.create_index(
            _AUDIT_INDEX,
            ["audit_id", "run_id", "execution_id", "created_at", "id"],
            unique=False,
        )


def _require_compatible_legacy_artifacts() -> None:
    context = op.get_context()
    if context.as_sql:
        if context.dialect.name != "postgresql":
            raise RuntimeError(
                "Artifact access upgrade requires an online database for dialect "
                f"{context.dialect.name!r}"
            )
        op.execute(sa.text('LOCK TABLE "artifacts" IN ACCESS EXCLUSIVE MODE'))
        safe_components = _safe_storage_components_sql("postgresql")
        safe_mime_type = _safe_mime_type_sql("postgresql")
        op.execute(
            sa.text(
                "DO $riftx$ BEGIN "
                "IF EXISTS (SELECT 1 FROM artifacts AS a JOIN runs AS r "
                "ON r.id = a.run_id WHERE r.kind = 'code_audit' LIMIT 1) THEN "
                "RAISE EXCEPTION 'cannot classify legacy Code Audit Artifacts'; "
                "END IF; "
                "IF EXISTS (SELECT 1 FROM artifacts AS a LEFT JOIN executions AS e "
                "ON e.id = a.execution_id WHERE a.execution_id IS NOT NULL AND "
                "(e.id IS NULL OR e.run_id <> a.run_id) LIMIT 1) THEN "
                "RAISE EXCEPTION 'cannot preserve cross-Run Artifact Execution owner'; "
                "END IF; "
                f"IF EXISTS (SELECT 1 FROM artifacts WHERE NOT ({safe_components}) LIMIT 1) "
                "THEN RAISE EXCEPTION 'cannot canonicalize unsafe legacy Artifact'; "
                "END IF; "
                f"IF EXISTS (SELECT 1 FROM artifacts WHERE NOT ({safe_mime_type}) LIMIT 1) "
                "THEN RAISE EXCEPTION 'cannot preserve unsafe legacy Artifact MIME type'; "
                "END IF; END $riftx$"
            )
        )
        return

    connection = op.get_bind()
    _serialize_artifact_writes(connection, operation="upgrade")
    code_audit_artifact = connection.execute(
        sa.text(
            "SELECT 1 FROM artifacts AS a JOIN runs AS r ON r.id = a.run_id "
            "WHERE r.kind = 'code_audit' LIMIT 1"
        )
    ).first()
    if code_audit_artifact is not None:
        raise RuntimeError(
            "cannot classify legacy Artifacts belonging to code_audit Runs; "
            "explicit owner repair is required"
        )

    cross_run_execution = connection.execute(
        sa.text(
            "SELECT 1 FROM artifacts AS a LEFT JOIN executions AS e "
            "ON e.id = a.execution_id WHERE a.execution_id IS NOT NULL "
            "AND (e.id IS NULL OR e.run_id <> a.run_id) LIMIT 1"
        )
    ).first()
    if cross_run_execution is not None:
        raise RuntimeError("cannot preserve a legacy Artifact with a cross-Run Execution owner")

    rows = connection.execute(sa.text("SELECT id, run_id, name, mime_type FROM artifacts"))
    for row in rows.mappings():
        try:
            _validate_component(row["run_id"], max_length=64)
            _validate_component(row["id"], max_length=64)
            _validate_component(row["name"], max_length=255)
            _validate_mime_type(row["mime_type"])
        except (TypeError, ValueError):
            raise RuntimeError("cannot preserve unsafe legacy Artifact metadata") from None


def downgrade() -> None:
    with _serialized_artifact_schema_change():
        _require_lossless_legacy_projection()
        dialect_name = op.get_context().dialect.name
        with op.batch_alter_table(
            _TABLE,
            naming_convention=_BATCH_NAMING_CONVENTION,
        ) as batch_op:
            batch_op.drop_index(_AUDIT_INDEX)
            batch_op.drop_index(_ACCESS_INDEX)
            for check_name in _CHECKS:
                batch_op.drop_constraint(check_name, type_="check")
            batch_op.drop_constraint(_FOREIGN_KEY, type_="foreignkey")
            batch_op.drop_constraint(
                _EXECUTION_FOREIGN_KEY,
                type_="foreignkey",
            )
            batch_op.create_foreign_key(
                _legacy_execution_foreign_key_name(dialect_name),
                "executions",
                ["execution_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.drop_column("ingest_provenance_json")
            batch_op.drop_column("storage_key")
            batch_op.drop_column("content_trust")
            batch_op.drop_column("access_class")
            batch_op.drop_column("audit_id")


def _require_lossless_legacy_projection() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "Artifact access downgrade requires an online database to prove "
            "that no security metadata would be lost"
        )

    connection = op.get_bind()
    _serialize_artifact_writes(connection, operation="downgrade")
    cross_run_execution = connection.execute(
        sa.text(
            "SELECT 1 FROM artifacts AS a LEFT JOIN executions AS e "
            "ON e.id = a.execution_id WHERE a.execution_id IS NOT NULL "
            "AND (e.id IS NULL OR e.run_id <> a.run_id) LIMIT 1"
        )
    ).first()
    if cross_run_execution is not None:
        raise RuntimeError(
            "cannot downgrade Artifact access persistence while cross-Run "
            "Execution ownership exists"
        )
    rows = connection.execute(
        sa.text(
            "SELECT id, run_id, name, mime_type, audit_id, access_class, "
            "content_trust, storage_key, ingest_provenance_json FROM artifacts"
        )
    )
    for row in rows.mappings():
        try:
            artifact_id = _validate_component(row["id"], max_length=64)
            run_id = _validate_component(row["run_id"], max_length=64)
            name = _validate_component(row["name"], max_length=255)
            _validate_mime_type(row["mime_type"])
            provenance = _decode_provenance(row["ingest_provenance_json"])
            compatible = (
                row["audit_id"] is None
                and row["access_class"] == "public_export"
                and row["content_trust"] == "untrusted_tool_output"
                and row["storage_key"] == f"runs/{run_id}/artifacts/{artifact_id}/{name}"
                and provenance == _LEGACY_PROVENANCE
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            compatible = False
        if not compatible:
            raise RuntimeError(
                "cannot downgrade Artifact access persistence while classified, "
                "non-legacy, or corrupt Artifact rows exist"
            )


def _decode_provenance(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _validate_component(value: object, *, max_length: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length or value in {".", ".."}:
        raise ValueError("unsafe Artifact component")
    if "/" in value or "\\" in value:
        raise ValueError("unsafe Artifact component")
    if any(not 0x20 <= ord(character) <= 0x7E for character in value):
        raise ValueError("unsafe Artifact component")
    return value


def _validate_mime_type(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 255
        or value != value.strip()
        or any(not 0x20 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("unsafe Artifact MIME type")
    return value


def _safe_storage_components_sql(dialect_name: str) -> str:
    if dialect_name == "sqlite":

        def safe(column: str, max_length: int) -> str:
            return (
                f"length({column}) BETWEEN 1 AND {max_length} "
                f"AND {column} NOT IN ('.', '..') "
                f"AND instr({column}, '/') = 0 AND instr({column}, '\\') = 0 "
                f"AND instr({column}, char(0)) = 0 "
                f"AND {column} NOT GLOB '*[^ -~]*'"
            )
    elif dialect_name == "postgresql":

        def safe(column: str, max_length: int) -> str:
            return (
                f"length({column}) BETWEEN 1 AND {max_length} "
                f"AND {column} NOT IN ('.', '..') "
                f"AND position('/' in {column}) = 0 "
                f"AND position(chr(92) in {column}) = 0 "
                f"AND {column} !~ '[^ -~]'"
            )
    else:
        raise RuntimeError(
            "Artifact access migration cannot install portable storage checks for "
            f"database dialect {dialect_name!r}"
        )
    return " AND ".join(
        f"({safe(column, max_length)})"
        for column, max_length in (("run_id", 64), ("id", 64), ("name", 255))
    )


def _safe_mime_type_sql(dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return (
            "length(mime_type) BETWEEN 1 AND 255 "
            "AND mime_type = trim(mime_type) "
            "AND instr(mime_type, char(0)) = 0 "
            "AND mime_type NOT GLOB '*[^ -~]*'"
        )
    if dialect_name == "postgresql":
        return (
            "length(mime_type) BETWEEN 1 AND 255 "
            "AND mime_type = btrim(mime_type) "
            "AND mime_type !~ '[^ -~]'"
        )
    raise RuntimeError(
        "Artifact access migration cannot install a portable MIME safety check for "
        f"database dialect {dialect_name!r}"
    )


def _legacy_execution_foreign_key_name(dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return _SQLITE_LEGACY_EXECUTION_FOREIGN_KEY
    if dialect_name == "postgresql":
        return _POSTGRESQL_LEGACY_EXECUTION_FOREIGN_KEY
    raise RuntimeError(
        "Artifact access migration cannot identify the legacy execution foreign key "
        f"for database dialect {dialect_name!r}"
    )


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def _serialize_artifact_writes(connection: Connection, *, operation: str) -> None:
    if not connection.in_transaction():
        raise RuntimeError(
            f"Artifact access {operation} requires one active transaction for "
            "serialization, verification, and DDL"
        )
    dialect_name = connection.dialect.name
    if dialect_name == "postgresql":
        connection.exec_driver_sql('LOCK TABLE "artifacts" IN ACCESS EXCLUSIVE MODE')
        return
    if dialect_name == "sqlite":
        connection.exec_driver_sql('UPDATE "artifacts" SET "id" = "id" WHERE 1 = 0')
        return
    raise RuntimeError(
        f"Artifact access {operation} cannot safely serialize writes for database "
        f"dialect {dialect_name!r}"
    )


@contextmanager
def _serialized_artifact_schema_change() -> Iterator[None]:
    """Keep Artifact verification and DDL in one serialized transaction.

    PostgreSQL already holds the Alembic transaction and acquires its table lock
    in ``_serialize_artifact_writes``. SQLite needs a different sequence:
    foreign keys must be disabled outside a transaction, after which one explicit
    ``BEGIN EXCLUSIVE`` owns compatibility verification, backfill/batch DDL, and
    the final foreign-key check. Raw COMMIT/ROLLBACK is intentional here because
    Alembic's autocommit block otherwise commits every SQLite DDL statement
    independently and would reopen the verification-to-DDL race.
    """

    context = op.get_context()
    if context.dialect.name != "sqlite":
        yield
        return
    if context.as_sql:
        raise RuntimeError(
            "SQLite Artifact access migration requires an online database for "
            "batch reflection and foreign-key verification"
        )

    connection = op.get_bind()
    with context.autocommit_block():
        foreign_keys_enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        transaction_started = False
        try:
            if foreign_keys_enabled:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
                raise RuntimeError("could not suspend SQLite foreign key enforcement")

            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            transaction_started = True
            yield
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "Artifact access migration produced SQLite foreign key violations"
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
            if foreign_keys_enabled:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            restored = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
            if restored is not foreign_keys_enabled:
                raise RuntimeError("could not restore SQLite foreign key enforcement")
