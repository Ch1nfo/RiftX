"""Async database engine and session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .orm import Base


class Database:
    """Own the SQLAlchemy engine and async session factory."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            hide_parameters=True,
        )
        if self.engine.url.get_backend_name() == "sqlite":
            event.listen(self.engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def create_schema(self) -> None:
        """Create metadata directly for tests and embedded bootstrap flows."""

        _load_additive_metadata_models()
        async with self.engine.begin() as connection:
            await connection.run_sync(_require_safe_metadata_bootstrap)
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(_quarantine_unbound_runner_commands)

    async def drop_schema(self) -> None:
        _load_additive_metadata_models()
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _require_safe_metadata_bootstrap(connection: Connection) -> None:
    """Keep metadata bootstrap from acting as an incremental migration system.

    A database carrying ``alembic_version`` is migration-managed; if any
    metadata table is missing, only Alembic may advance it. Embedded databases
    without Alembic state still enforce the guarded AUD-103 legacy-Audit proof
    before additive request-table DDL.
    """

    table_names = set(inspect(connection).get_table_names())
    missing_metadata_tables = set(Base.metadata.tables).difference(table_names)
    audit_scans_exist = "audit_scans" in table_names
    request_table_exists = "audit_client_requests" in table_names
    runner_commands_exist = "runner_commands" in table_names
    runner_ownership_tables = {
        "runner_effect_bindings",
        "runner_command_ownerships",
        "runner_stop_receipts",
        "runner_stop_projections",
    }

    if "alembic_version" in table_names and missing_metadata_tables:
        if audit_scans_exist and not request_table_exists:
            _lock_legacy_audit_scans(connection)
            _require_no_legacy_audits(connection)
        raise RuntimeError(
            "database schema is behind RiftX metadata; apply all Alembic migrations "
            "before starting RiftX"
        )

    if runner_commands_exist:
        command_columns = {
            item["name"] for item in inspect(connection).get_columns("runner_commands")
        }
        credential_columns = {
            item["name"] for item in inspect(connection).get_columns("runner_credentials")
        }
        execution_columns = {
            item["name"] for item in inspect(connection).get_columns("executions")
        }
        required_execution_binding_columns = {
            "runner_command_id",
            "runner_effect_binding_id",
            "runner_binding_digest",
            "runner_envelope_digest",
        }
        if (
            "state_version" not in command_columns
            or "protocol_capabilities_json" not in credential_columns
            or not required_execution_binding_columns.issubset(execution_columns)
            or not runner_ownership_tables.issubset(table_names)
        ):
            _lock_legacy_runner_commands(connection)
            raise RuntimeError(
                "database Runner schema predates immutable command ownership; apply all "
                "Alembic migrations before starting RiftX"
            )

    if not audit_scans_exist or request_table_exists:
        return

    _lock_legacy_audit_scans(connection)
    _require_no_legacy_audits(connection)


def _lock_legacy_audit_scans(connection: Connection) -> None:
    dialect_name = connection.dialect.name
    if dialect_name == "postgresql":
        connection.exec_driver_sql('LOCK TABLE "audit_scans" IN ACCESS EXCLUSIVE MODE')
    elif dialect_name == "sqlite":
        connection.exec_driver_sql(
            'UPDATE "audit_scans" SET "state_version" = "state_version" WHERE 1 = 0'
        )
    else:
        raise RuntimeError(
            "Code Audit request bootstrap cannot safely serialize legacy Audit "
            f"writes for database dialect {dialect_name!r}"
        )


def _require_no_legacy_audits(connection: Connection) -> None:
    if connection.execute(text('SELECT 1 FROM "audit_scans" LIMIT 1')).first() is not None:
        raise RuntimeError(
            "cannot add Code Audit request persistence while legacy Audit facts exist; "
            "an explicit compatibility migration is required"
        )


def _lock_legacy_runner_commands(connection: Connection) -> None:
    dialect_name = connection.dialect.name
    if dialect_name == "postgresql":
        connection.exec_driver_sql('LOCK TABLE "runner_commands" IN ACCESS EXCLUSIVE MODE')
    elif dialect_name == "sqlite":
        connection.exec_driver_sql(
            'UPDATE "runner_commands" SET "updated_at" = "updated_at" WHERE 1 = 0'
        )
    else:
        raise RuntimeError(
            "Runner ownership bootstrap cannot safely serialize legacy writes for "
            f"database dialect {dialect_name!r}"
        )


def _quarantine_unbound_runner_commands(connection: Connection) -> None:
    """Seed quarantine only for current-schema rows that lack an owner envelope."""

    table_names = set(inspect(connection).get_table_names())
    if not {"runner_commands", "runner_command_ownerships"}.issubset(table_names):
        return
    _lock_legacy_runner_commands(connection)
    connection.execute(
        text(
            "INSERT INTO runner_command_ownerships "
            "(command_id, verification_state, schema_version, effect_binding_id, operation, "
            "operation_family, payload_digest, output_contract_json, output_contract_digest, "
            "envelope_digest, quarantine_reason, quarantined_at, reconciliation_state, "
            "replacement_command_id, created_at) "
            "SELECT command.id, 'quarantined', NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
            "NULL, 'metadata_bootstrap_ownership_missing', CURRENT_TIMESTAMP, 'untouched', "
            "NULL, command.created_at FROM runner_commands AS command "
            "LEFT JOIN runner_command_ownerships AS ownership "
            "ON ownership.command_id = command.id WHERE ownership.command_id IS NULL"
        )
    )
    if connection.dialect.name == "sqlite":
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                "Runner ownership metadata bootstrap introduced foreign-key violations: "
                f"{violations!r}"
            )


def _load_additive_metadata_models() -> None:
    """Register isolated persistence modules on the shared metadata root."""

    from .audit_preflight import AuditPreflightJobRecord  # noqa: PLC0415
    from .workflow_signals import WorkflowSignalIntentRecord  # noqa: PLC0415

    assert AuditPreflightJobRecord.__tablename__ in Base.metadata.tables
    assert WorkflowSignalIntentRecord.__tablename__ in Base.metadata.tables
