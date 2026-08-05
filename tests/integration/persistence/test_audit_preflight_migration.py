from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from tests.integration.persistence.test_audit_preflight_repository import _pending_job
from tests.integration.persistence.test_migrations import (
    _run_alembic_with_sqlite_foreign_keys,
    downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql
from tests.integration.persistence.test_runner_ownership_migration import _insert_node

from riftx.persistence import Database
from riftx.persistence.audit_preflight import SQLAlchemyAuditPreflightRepository
from riftx.persistence.orm import Base

BASE_REVISION = "4f9a6c1d2e30"
PREFLIGHT_REVISION = "2b7d9e4a6c10"
HEAD_REVISION = "6c8e4a2f1b70"
EARLIEST_REVISION = "2f14cbcea74b"
PREFLIGHT_TABLES = {
    "audit_preflight_jobs",
    "audit_preflight_job_requests",
    "audit_preflight_results",
    "audit_preflight_exit_receipts",
    "audit_preflight_stop_receipts",
}
MIGRATION = run_path(
    str(Path(__file__).parents[3] / "migrations/versions/2b7d9e4a6c10_add_audit_preflight_jobs.py")
)


def test_preflight_upgrade_compiles_for_postgresql_offline() -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{PREFLIGHT_REVISION}",
    )
    repeated_sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{PREFLIGHT_REVISION}",
    )

    assert repeated_sql == sql
    lock = "LOCK TABLE runner_credentials IN ACCESS EXCLUSIVE MODE"
    assert lock in sql
    assert sql.index(lock) < sql.index("CREATE TABLE audit_preflight_jobs")
    for table_name in PREFLIGHT_TABLES:
        assert f"CREATE TABLE {table_name}" in sql
    assert "state_version BIGINT NOT NULL" in sql
    assert "attempt BIGINT NOT NULL" in sql
    assert "lease_expected_state_version BIGINT" in sql
    assert "preflight_job_owner_v1" not in sql
    assert "audit_preflight_plans" not in sql
    assert "preflight_token" not in sql


def test_postgresql_online_upgrade_locks_capability_table_before_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class RecordingConnection:
        @staticmethod
        def exec_driver_sql(statement: str) -> None:
            statements.append(statement)

    migration_op = MIGRATION["op"]
    monkeypatch.setattr(
        migration_op,
        "get_context",
        lambda: SimpleNamespace(as_sql=False, dialect=postgresql.dialect()),
    )
    monkeypatch.setattr(migration_op, "get_bind", RecordingConnection)

    MIGRATION["_acquire_upgrade_lock"]()

    assert statements == ["LOCK TABLE runner_credentials IN ACCESS EXCLUSIVE MODE"]


def test_preflight_downgrade_refuses_postgresql_offline() -> None:
    with pytest.raises(RuntimeError, match="offline downgrade cannot prove"):
        _offline_sql(
            "postgresql+asyncpg://riftx@localhost/riftx",
            f"{PREFLIGHT_REVISION}:{BASE_REVISION}",
            downgrade=True,
        )


def test_postgresql_downgrade_locks_capability_and_fact_tables_before_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []

    class StopAfterFirstRead(RuntimeError):
        pass

    class RecordingConnection:
        dialect = postgresql.dialect()

        @staticmethod
        def exec_driver_sql(statement: str) -> None:
            events.append(("lock", statement))

        @staticmethod
        def execute(statement: object) -> None:
            events.append(("read", str(statement)))
            raise StopAfterFirstRead

    migration_op = MIGRATION["op"]
    monkeypatch.setattr(migration_op, "get_bind", RecordingConnection)

    with pytest.raises(StopAfterFirstRead):
        MIGRATION["_downgrade"]()

    assert events == [
        (
            "lock",
            "LOCK TABLE runner_credentials, audit_preflight_stop_receipts, "
            "audit_preflight_exit_receipts, audit_preflight_results, "
            "audit_preflight_job_requests, audit_preflight_jobs "
            "IN ACCESS EXCLUSIVE MODE",
        ),
        (
            "read",
            "SELECT 1 FROM runner_credentials WHERE "
            "CAST(protocol_capabilities_json AS TEXT) LIKE "
            "'%\"preflight\\_job\\_owner\\_v1\"%' ESCAPE '\\' LIMIT 1",
        ),
    ]


def test_preflight_migration_round_trips_empty_database(tmp_path: Path) -> None:
    database_path = tmp_path / "preflight-roundtrip.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    assert PREFLIGHT_TABLES.issubset(sqlite_tables(database_path))

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )
    assert PREFLIGHT_TABLES.isdisjoint(sqlite_tables(database_path))

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    assert PREFLIGHT_TABLES.issubset(sqlite_tables(database_path))
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_preflight_nonempty_downgrade_fails_before_ddl_and_preserves_facts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "preflight-nonempty.db"
    run_alembic(database_path, "head")

    async def seed() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            repository = SQLAlchemyAuditPreflightRepository(database.session_factory)
            await repository.create(_pending_job(job_id="preflight-migration-job"))
        finally:
            await database.dispose()

    asyncio.run(seed())

    ddl_statements: list[str] = []

    def record_ddl(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith(("CREATE ", "ALTER ", "DROP ")):
            ddl_statements.append(normalized)

    event.listen(Engine, "before_cursor_execute", record_ddl)
    try:
        with pytest.raises(RuntimeError, match="durable Audit Preflight facts exist"):
            downgrade_alembic(database_path, BASE_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", record_ddl)

    assert ddl_statements == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert connection.execute("SELECT id FROM audit_preflight_jobs").fetchone() == (
            "preflight-migration-job",
        )
        assert connection.execute("SELECT job_id FROM audit_preflight_job_requests").fetchone() == (
            "preflight-migration-job",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert PREFLIGHT_TABLES.issubset(sqlite_tables(database_path))


def test_preflight_receipt_fact_blocks_downgrade_before_any_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "preflight-receipt-downgrade.db"
    run_alembic(database_path, "head")

    async def seed() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            repository = SQLAlchemyAuditPreflightRepository(database.session_factory)
            await repository.create(_pending_job(job_id="preflight-receipt-job"))
        finally:
            await database.dispose()

    asyncio.run(seed())
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO audit_preflight_stop_receipts "
            "(id, job_id, schema_version, disposition, canonical_json, receipt_digest, "
            "received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "preflight-receipt",
                "preflight-receipt-job",
                "riftx.audit-preflight-stop-receipt/v1",
                "never_created",
                "{}",
                "a" * 64,
                "2026-08-04 12:00:00+00:00",
            ),
        )
        connection.commit()

    ddl_statements: list[str] = []

    def record_ddl(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith(("CREATE ", "ALTER ", "DROP ")):
            ddl_statements.append(normalized)

    event.listen(Engine, "before_cursor_execute", record_ddl)
    try:
        with pytest.raises(RuntimeError, match="durable Audit Preflight facts exist"):
            downgrade_alembic(database_path, BASE_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", record_ddl)

    assert ddl_statements == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT id, job_id FROM audit_preflight_stop_receipts"
        ).fetchone() == ("preflight-receipt", "preflight-receipt-job")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_preflight_capability_fact_blocks_empty_table_downgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "preflight-capability.db"
    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        _insert_node(connection, current_schema=True)
        connection.execute(
            "UPDATE runner_credentials SET protocol_capabilities_json = ?",
            ('["preflight_job_owner_v1"]',),
        )
        connection.commit()
    downgrade_alembic(database_path, PREFLIGHT_REVISION)

    ddl_statements: list[str] = []

    def record_ddl(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith(("CREATE ", "ALTER ", "DROP ")):
            ddl_statements.append(normalized)

    event.listen(Engine, "before_cursor_execute", record_ddl)
    try:
        with pytest.raises(RuntimeError, match="Runner capability facts exist"):
            downgrade_alembic(database_path, BASE_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", record_ddl)

    assert ddl_statements == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PREFLIGHT_REVISION,
        )
        assert connection.execute(
            "SELECT protocol_capabilities_json FROM runner_credentials"
        ).fetchone() == ('["preflight_job_owner_v1"]',)


def test_migrated_schema_allows_unattempted_cancelling_but_rejects_active_proof(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "preflight-constraints.db"
    run_alembic(database_path, "head")

    async def seed() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            repository = SQLAlchemyAuditPreflightRepository(database.session_factory)
            await repository.create(_pending_job(job_id="preflight-constraints"))
        finally:
            await database.dispose()

    asyncio.run(seed())

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE audit_preflight_jobs SET status = 'cancelling', state_version = 2 "
            "WHERE id = 'preflight-constraints'"
        )
        connection.commit()
        assert connection.execute(
            "SELECT status, attempt, lease_id, capsule_id FROM audit_preflight_jobs"
        ).fetchone() == ("cancelling", 0, None, None)

        with pytest.raises(sqlite3.IntegrityError, match="stop_proof_status"):
            connection.execute(
                "UPDATE audit_preflight_jobs SET status = 'pending', "
                "never_created_proof_digest = ? WHERE id = 'preflight-constraints'",
                ("a" * 64,),
            )


def test_preflight_migration_schema_matches_orm_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "preflight-parity.db"
    run_alembic(database_path, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        for table_name in PREFLIGHT_TABLES:
            metadata_table = Base.metadata.tables[table_name]
            actual_columns = {
                column["name"]: (
                    column["nullable"],
                    getattr(column["type"], "length", None),
                )
                for column in inspector.get_columns(table_name)
            }
            expected_columns = {
                column.name: (column.nullable, getattr(column.type, "length", None))
                for column in metadata_table.columns
            }
            assert actual_columns == expected_columns
            assert {
                constraint["name"] for constraint in inspector.get_check_constraints(table_name)
            } == {
                constraint.name
                for constraint in metadata_table.constraints
                if isinstance(constraint, CheckConstraint)
            }
            assert {
                (constraint["name"], tuple(constraint["column_names"]))
                for constraint in inspector.get_unique_constraints(table_name)
            } == {
                (constraint.name, tuple(column.name for column in constraint.columns))
                for constraint in metadata_table.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            assert {
                (
                    constraint["name"],
                    tuple(constraint["constrained_columns"]),
                    constraint["referred_table"],
                    tuple(constraint["referred_columns"]),
                    constraint["options"].get("ondelete"),
                )
                for constraint in inspector.get_foreign_keys(table_name)
            } == {
                (
                    constraint.name,
                    tuple(element.parent.name for element in constraint.elements),
                    constraint.referred_table.name,
                    tuple(element.column.name for element in constraint.elements),
                    constraint.ondelete,
                )
                for constraint in metadata_table.constraints
                if isinstance(constraint, ForeignKeyConstraint)
            }
            assert {
                (index["name"], tuple(index["column_names"]))
                for index in inspector.get_indexes(table_name)
            } == {
                (index.name, tuple(column.name for column in index.columns))
                for index in metadata_table.indexes
                if isinstance(index, Index)
            }
    finally:
        engine.dispose()


def test_earliest_upgrade_reopens_at_head_with_clean_foreign_keys(tmp_path: Path) -> None:
    database_path = tmp_path / "preflight-earliest-head.db"
    run_alembic(database_path, EARLIEST_REVISION)
    run_alembic(database_path, "head")

    async def reopen() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            await database.create_schema()
        finally:
            await database.dispose()

    asyncio.run(reopen())
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert PREFLIGHT_TABLES.issubset(
            {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        )


def test_preflight_upgrade_fault_rolls_back_partial_ddl_and_can_retry(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "preflight-upgrade-fault.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    fault_injected = False

    def fail_after_partial_preflight_ddl(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal fault_injected
        normalized = " ".join(statement.replace('"', "").split()).lower()
        if normalized.startswith("create table audit_preflight_results"):
            fault_injected = True
            raise RuntimeError("injected Audit Preflight upgrade fault")

    event.listen(Engine, "before_cursor_execute", fail_after_partial_preflight_ddl)
    try:
        with pytest.raises(RuntimeError, match="injected Audit Preflight upgrade fault"):
            _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_partial_preflight_ddl)

    assert fault_injected
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert PREFLIGHT_TABLES.isdisjoint(sqlite_tables(database_path))

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert PREFLIGHT_TABLES.issubset(sqlite_tables(database_path))


def test_preflight_downgrade_fault_rolls_back_partial_ddl_and_can_retry(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "preflight-downgrade-fault.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    fault_injected = False

    def fail_after_partial_preflight_ddl(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal fault_injected
        normalized = " ".join(statement.replace('"', "").split()).lower()
        if normalized == "drop table audit_preflight_results":
            fault_injected = True
            raise RuntimeError("injected Audit Preflight downgrade fault")

    event.listen(Engine, "before_cursor_execute", fail_after_partial_preflight_ddl)
    try:
        with pytest.raises(RuntimeError, match="injected Audit Preflight downgrade fault"):
            _run_alembic_with_sqlite_foreign_keys(
                database_path,
                BASE_REVISION,
                downgrade=True,
            )
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_partial_preflight_ddl)

    assert fault_injected
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PREFLIGHT_REVISION,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert PREFLIGHT_TABLES.issubset(sqlite_tables(database_path))

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert PREFLIGHT_TABLES.isdisjoint(sqlite_tables(database_path))
