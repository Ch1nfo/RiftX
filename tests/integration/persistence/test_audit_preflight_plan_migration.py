from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
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
from tests.integration.persistence.test_audit_preflight_repository import (
    NOW,
    _pending_job,
    _running_job,
    _successful_finish,
)
from tests.integration.persistence.test_migrations import (
    _run_alembic_with_sqlite_foreign_keys,
    downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

from riftx.application.errors import RepositoryConflictError
from riftx.application.ports.audit_preflight import (
    AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION,
)
from riftx.domain.audit_preflight import PreflightRequest
from riftx.domain.audit_preflight_plan import AuditPreflightPlan, AuditPreflightTokenCodec
from riftx.persistence import Database
from riftx.persistence.audit_preflight import SQLAlchemyAuditPreflightRepository
from riftx.persistence.audit_preflight_plan import SQLAlchemyAuditPreflightPlanRepository
from riftx.persistence.orm import Base

BASE_REVISION = "2b7d9e4a6c10"
PLAN_REVISION = "5d8c1a7e3b24"
HEAD_REVISION = "d0b4e6f8a102"
PLAN_TABLE = "audit_preflight_plans"
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
    historical = _pending_job(job_id="historical-preflight-job")
    _insert_historical_pending_job(database_path, historical)

    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT plan_issuance_schema_version FROM audit_preflight_jobs "
            "WHERE id = ?",
            (historical.job_id,),
        ).fetchone() == (None,)

    async def exercise() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            jobs = SQLAlchemyAuditPreflightRepository(database.session_factory)
            plans = SQLAlchemyAuditPreflightPlanRepository(database.session_factory)
            running = await _running_job(jobs, historical)
            succeeded, result, receipt = _successful_finish(running)
            succeeded = await jobs.compare_and_set(
                previous=running,
                updated=succeeded,
                result=result,
                exit_receipt=receipt,
            )
            request = PreflightRequest.model_validate_json(succeeded.restricted_request_json)
            issue = AuditPreflightPlan.from_succeeded(
                job=succeeded,
                result=result,
                restricted_request=request,
                token_codec=AuditPreflightTokenCodec(
                    key_id="preflight-key-1",
                    key=b"K" * 32,
                    nonce_factory=lambda size: b"H" * size,
                ),
                plan_id="historical-plan-forbidden",
                created_at=NOW + timedelta(minutes=4),
                expires_at=NOW + timedelta(minutes=20),
            )
            with pytest.raises(RepositoryConflictError, match="not eligible"):
                await plans.create(issue.plan)

            current = _pending_job(
                job_id="current-preflight-job",
                request=PreflightRequest.model_validate(
                    {
                        **request.model_dump(mode="python"),
                        "client_request_id": "523e4567-e89b-42d3-a456-426614174000",
                    }
                ),
            )
            await jobs.create(current)
        finally:
            await database.dispose()

    asyncio.run(exercise())
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT plan_issuance_schema_version FROM audit_preflight_jobs "
            "WHERE id = ?",
            (historical.job_id,),
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT plan_issuance_schema_version FROM audit_preflight_jobs "
            "WHERE id = ?",
            ("current-preflight-job",),
        ).fetchone() == (AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION,)


def test_historical_only_database_can_downgrade_without_losing_job(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "plan-history-downgrade.db"
    run_alembic(database_path, BASE_REVISION)
    historical = _pending_job(job_id="historical-downgrade-job")
    _insert_historical_pending_job(database_path, historical)
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
        ).fetchone() == (historical.job_id,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_plan_eligible_job_blocks_downgrade_before_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "plan-eligible-downgrade.db"
    run_alembic(database_path, "head")

    async def seed() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            jobs = SQLAlchemyAuditPreflightRepository(database.session_factory)
            await jobs.create(_pending_job(job_id="eligible-downgrade-job"))
        finally:
            await database.dispose()

    asyncio.run(seed())
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


def _insert_historical_pending_job(database_path: Path, job: object) -> None:
    values = job.model_dump(mode="python")  # type: ignore[attr-defined]

    def sqlite_datetime(value: object) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")  # type: ignore[attr-defined]

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO audit_preflight_jobs "
            "(id, schema_version, client_request_id, operator_principal_id, "
            "authorization_scope_digest, request_schema_version, request_digest, "
            "source_node_id, source_root_identity_digest, backend_id, image_digest, "
            "policy_digest, canonical_empty_context_id, canonical_empty_context_digest, "
            "status, effect_owner_digest, attempt, expires_at, created_at, updated_at, "
            "state_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                values["job_id"],
                values["schema_version"],
                values["client_request_id"],
                values["operator_principal_id"],
                values["authorization_scope_digest"],
                values["request_schema_version"],
                values["request_digest"],
                values["source_node_id"],
                values["source_root_identity_digest"],
                values["backend_id"],
                values["image_digest"],
                values["policy_digest"],
                values["canonical_empty_context_id"],
                values["canonical_empty_context_digest"],
                values["status"].value,
                values["effect_owner_digest"],
                values["attempt"],
                sqlite_datetime(values["expires_at"]),
                sqlite_datetime(values["created_at"]),
                sqlite_datetime(values["updated_at"]),
                values["state_version"],
            ),
        )
        connection.execute(
            "INSERT INTO audit_preflight_job_requests "
            "(job_id, schema_version, canonical_json, request_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                values["job_id"],
                values["request_schema_version"],
                values["restricted_request_json"],
                values["request_digest"],
                sqlite_datetime(values["created_at"]),
            ),
        )
        connection.commit()
