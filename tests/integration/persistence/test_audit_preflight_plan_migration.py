from __future__ import annotations

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
from tests.integration.persistence.test_audit_preflight_migration import (
    _insert_pending_job,
)
from tests.integration.persistence.test_migrations import (
    _run_alembic_with_sqlite_foreign_keys,
    downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

from riftx.persistence.orm import Base

BASE_REVISION = "2b7d9e4a6c10"
PLAN_REVISION = "5d8c1a7e3b24"
HEAD_REVISION = "7b3d1e5f9a24"
PLAN_TABLE = "audit_preflight_plans"
PLAN_ISSUANCE_SCHEMA_VERSION = "riftx.audit-preflight-plan-issuance/v1"
MIGRATION = run_path(
    str(
        Path(__file__).parents[3]
        / "migrations/versions/5d8c1a7e3b24_add_audit_preflight_plans.py"
    )
)


def test_plan_upgrade_compiles_for_postgresql_without_historical_backfill() -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{PLAN_REVISION}",
    )
    repeated = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{PLAN_REVISION}",
    )

    assert repeated == sql
    lock = "LOCK TABLE audit_preflight_jobs IN ACCESS EXCLUSIVE MODE"
    assert lock in sql
    assert sql.index(lock) < sql.index("ALTER TABLE audit_preflight_jobs ADD COLUMN")
    assert sql.index(lock) < sql.index("CREATE TABLE audit_preflight_plans")
    assert "plan_issuance_schema_version VARCHAR(64)" in sql
    assert "UPDATE audit_preflight_jobs" not in sql
    assert "preflight_token" not in sql
    assert "raw_token" not in sql


def test_postgresql_upgrade_locks_jobs_before_publishing_marker_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []

    class StopAfterColumn(RuntimeError):
        pass

    class RecordingConnection:
        @staticmethod
        def exec_driver_sql(statement: str) -> None:
            events.append(("lock", statement))

    migration_op = MIGRATION["op"]
    monkeypatch.setattr(
        migration_op,
        "get_context",
        lambda: SimpleNamespace(as_sql=False, dialect=postgresql.dialect()),
    )
    monkeypatch.setattr(migration_op, "get_bind", RecordingConnection)

    def add_column(table_name: str, _column: object) -> None:
        events.append(("ddl", table_name))
        raise StopAfterColumn

    monkeypatch.setattr(migration_op, "add_column", add_column)
    with pytest.raises(StopAfterColumn):
        MIGRATION["_upgrade"]()

    assert events == [
        ("lock", "LOCK TABLE audit_preflight_jobs IN ACCESS EXCLUSIVE MODE"),
        ("ddl", "audit_preflight_jobs"),
    ]


def test_upgrade_keeps_historical_jobs_ineligible_and_new_jobs_are_marked(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "plan-history.db"
    run_alembic(database_path, BASE_REVISION)
    historical_id = "historical-preflight-job"
    _insert_pending_job(database_path, historical_id)

    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT plan_issuance_schema_version FROM audit_preflight_jobs "
            "WHERE id = ?",
            (historical_id,),
        ).fetchone() == (None,)
    _insert_pending_job(
        database_path,
        "current-preflight-job",
        plan_issuance_schema_version=PLAN_ISSUANCE_SCHEMA_VERSION,
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT plan_issuance_schema_version FROM audit_preflight_jobs "
            "WHERE id = ?",
            (historical_id,),
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT plan_issuance_schema_version FROM audit_preflight_jobs "
            "WHERE id = ?",
            ("current-preflight-job",),
        ).fetchone() == (PLAN_ISSUANCE_SCHEMA_VERSION,)


def test_historical_only_database_can_downgrade_without_losing_job(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "plan-history-downgrade.db"
    run_alembic(database_path, BASE_REVISION)
    historical_id = "historical-downgrade-job"
    _insert_pending_job(database_path, historical_id)
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )
    assert PLAN_TABLE not in sqlite_tables(database_path)
    with sqlite3.connect(database_path) as connection:
        assert "plan_issuance_schema_version" not in {
            row[1] for row in connection.execute("PRAGMA table_info(audit_preflight_jobs)")
        }
        assert connection.execute(
            "SELECT id FROM audit_preflight_jobs"
        ).fetchone() == (historical_id,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_plan_eligible_job_blocks_downgrade_before_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "plan-eligible-downgrade.db"
    run_alembic(database_path, "head")

    _insert_pending_job(
        database_path,
        "eligible-downgrade-job",
        plan_issuance_schema_version=PLAN_ISSUANCE_SCHEMA_VERSION,
    )
    ddl_statements: list[str] = []

    def capture_ddl(
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

    event.listen(Engine, "before_cursor_execute", capture_ddl)
    try:
        with pytest.raises(RuntimeError, match="Plan-eligible"):
            downgrade_alembic(database_path, BASE_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", capture_ddl)

    assert ddl_statements == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert connection.execute(
            "SELECT id FROM audit_preflight_jobs"
        ).fetchone() == ("eligible-downgrade-job",)


def test_plan_migration_schema_matches_orm_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "plan-parity.db"
    run_alembic(database_path, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        metadata_table = Base.metadata.tables[PLAN_TABLE]
        assert {
            column["name"]: (column["nullable"], getattr(column["type"], "length", None))
            for column in inspector.get_columns(PLAN_TABLE)
        } == {
            column.name: (column.nullable, getattr(column.type, "length", None))
            for column in metadata_table.columns
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(PLAN_TABLE)
        } == {
            constraint.name
            for constraint in metadata_table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert {
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(PLAN_TABLE)
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
            for constraint in inspector.get_foreign_keys(PLAN_TABLE)
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
            for index in inspector.get_indexes(PLAN_TABLE)
        } == {
            (index.name, tuple(column.name for column in index.columns))
            for index in metadata_table.indexes
            if isinstance(index, Index)
        }
    finally:
        engine.dispose()


def test_plan_upgrade_fault_rolls_back_marker_column_and_table(tmp_path: Path) -> None:
    database_path = tmp_path / "plan-upgrade-fault.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    fault_injected = False

    def fail_on_plan_table(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal fault_injected
        normalized = " ".join(statement.replace('"', "").split()).lower()
        if normalized.startswith("create table audit_preflight_plans"):
            fault_injected = True
            raise RuntimeError("injected Plan upgrade fault")

    event.listen(Engine, "before_cursor_execute", fail_on_plan_table)
    try:
        with pytest.raises(RuntimeError, match="injected Plan upgrade fault"):
            _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_on_plan_table)

    assert fault_injected
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert "plan_issuance_schema_version" not in {
            row[1] for row in connection.execute("PRAGMA table_info(audit_preflight_jobs)")
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert PLAN_TABLE not in sqlite_tables(database_path)

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    assert PLAN_TABLE in sqlite_tables(database_path)
