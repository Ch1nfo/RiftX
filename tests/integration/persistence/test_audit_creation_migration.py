from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    inspect,
)
from tests.integration.persistence.test_audit_migration import (
    AUDIT_REVISION,
    NOW,
    _insert_contract,
    _insert_engagement,
    _insert_project,
    _insert_run,
    _insert_scan,
)
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

from riftx.persistence import Database
from riftx.persistence.orm import Base

REQUEST_REVISION = "7c4e1a9b2d06"
HEAD_REVISION = "6e4a2c9f1b30"
REQUEST_TABLE = "audit_client_requests"


def _insert_request_fixture(connection: sqlite3.Connection) -> None:
    _insert_legacy_audit_fixture(connection)
    connection.execute(
        "INSERT INTO audit_client_requests "
        "(client_request_id, operation, request_schema_version, request_digest, "
        "audit_id, run_id, project_id, engagement_id, contract_id, contract_digest, "
        "temporal_workflow_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "6ed6232a-3fb3-4f93-868f-0be291142f31",
            "create_draft",
            "riftx.audit-create-draft-request/v1",
            "c" * 64,
            "audit-request",
            "run-request",
            "project-request",
            "engagement-request",
            "contract-request",
            "b" * 64,
            "riftx-code-audit-audit-request",
            NOW,
        ),
    )


def _insert_legacy_audit_fixture(connection: sqlite3.Connection) -> None:
    _insert_engagement(connection, "engagement-request")
    _insert_run(
        connection,
        run_id="run-request",
        engagement_id="engagement-request",
        kind="code_audit",
        workflow_id="riftx-code-audit-audit-request",
    )
    _insert_project(
        connection,
        project_id="project-request",
        engagement_id="engagement-request",
        digest_character="a",
    )
    _insert_contract(
        connection,
        contract_id="contract-request",
        audit_id="audit-request",
        digest_character="b",
    )
    _insert_scan(
        connection,
        audit_id="audit-request",
        run_id="run-request",
        engagement_id="engagement-request",
        project_id="project-request",
        contract_id="contract-request",
        contract_digest_character="b",
        workflow_id="riftx-code-audit-audit-request",
    )


def test_request_migration_rejects_legacy_audits_before_creating_the_table(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-request-legacy-upgrade.db"
    run_alembic(database_path, AUDIT_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_audit_fixture(connection)
        connection.commit()

    with pytest.raises(RuntimeError, match="legacy Audit facts exist"):
        run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            AUDIT_REVISION,
        )
        assert connection.execute(
            "SELECT id FROM audit_scans WHERE id = 'audit-request'"
        ).fetchone() == ("audit-request",)
    assert REQUEST_TABLE not in sqlite_tables(database_path)


def test_schema_bootstrap_rejects_legacy_audits_before_creating_the_table(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-request-legacy-bootstrap.db"
    run_alembic(database_path, AUDIT_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_audit_fixture(connection)
        connection.commit()

    async def bootstrap() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            with pytest.raises(RuntimeError, match="legacy Audit facts exist"):
                await database.create_schema()
        finally:
            await database.dispose()

    asyncio.run(bootstrap())

    assert REQUEST_TABLE not in sqlite_tables(database_path)


def test_schema_bootstrap_requires_alembic_for_empty_migration_managed_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-request-empty-bootstrap.db"
    run_alembic(database_path, AUDIT_REVISION)

    async def rejected_bootstrap() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            with pytest.raises(RuntimeError, match="apply all Alembic migrations"):
                await database.create_schema()
        finally:
            await database.dispose()

    asyncio.run(rejected_bootstrap())

    assert REQUEST_TABLE not in sqlite_tables(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            AUDIT_REVISION,
        )

    run_alembic(database_path, "head")

    async def head_bootstrap() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            await database.create_schema()
        finally:
            await database.dispose()

    asyncio.run(head_bootstrap())

    assert REQUEST_TABLE in sqlite_tables(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )


def test_request_migration_round_trips_empty_database(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-request-roundtrip.db"
    run_alembic(database_path, "head")
    assert REQUEST_TABLE in sqlite_tables(database_path)

    downgrade_alembic(database_path, AUDIT_REVISION)
    assert REQUEST_TABLE not in sqlite_tables(database_path)

    run_alembic(database_path, "head")
    assert REQUEST_TABLE in sqlite_tables(database_path)


def test_request_migration_nonempty_downgrade_fails_without_data_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-request-nonempty.db"
    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_request_fixture(connection)
        connection.commit()

    with pytest.raises(RuntimeError, match="client-request facts exist"):
        downgrade_alembic(database_path, AUDIT_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REQUEST_REVISION,
        )
        assert connection.execute(
            "SELECT client_request_id FROM audit_client_requests"
        ).fetchone() == ("6ed6232a-3fb3-4f93-868f-0be291142f31",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_request_migration_compiles_for_postgresql_offline() -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{AUDIT_REVISION}:{REQUEST_REVISION}",
    )

    assert 'LOCK TABLE "audit_scans" IN ACCESS EXCLUSIVE MODE' in sql
    assert "DO $riftx$" in sql
    assert "legacy Audit facts exist" in sql
    assert (
        sql.index('LOCK TABLE "audit_scans" IN ACCESS EXCLUSIVE MODE')
        < sql.index("DO $riftx$")
        < sql.index("CREATE TABLE audit_client_requests")
    )
    assert "fk_audit_client_requests_scan_start_binding" in sql
    assert "fk_audit_client_requests_scan_project" in sql
    assert "fk_audit_client_requests_project_owner" in sql
    assert "fk_audit_client_requests_contract_binding" in sql


def test_request_table_rejects_noncanonical_extra_uuid_hyphen(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-request-canonical-uuid.db"
    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_request_fixture(connection)
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="canonical_id"):
            connection.execute(
                "UPDATE audit_client_requests SET client_request_id = ?",
                ("6ed6232a-3fb3-4f93-868f-0be291142f-1",),
            )


def test_request_migration_schema_matches_orm_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-request-parity.db"
    run_alembic(database_path, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        metadata_table = Base.metadata.tables[REQUEST_TABLE]
        actual_columns = {
            column["name"]: (column["nullable"], getattr(column["type"], "length", None))
            for column in inspector.get_columns(REQUEST_TABLE)
        }
        expected_columns = {
            column.name: (column.nullable, getattr(column.type, "length", None))
            for column in metadata_table.columns
        }
        assert actual_columns == expected_columns
        assert {
            constraint["name"] for constraint in inspector.get_check_constraints(REQUEST_TABLE)
        } == {
            constraint.name
            for constraint in metadata_table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(REQUEST_TABLE)
        } == {
            tuple(column.name for column in constraint.columns)
            for constraint in metadata_table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert {
            (
                tuple(constraint["constrained_columns"]),
                constraint["referred_table"],
                tuple(constraint["referred_columns"]),
                constraint["options"].get("ondelete"),
            )
            for constraint in inspector.get_foreign_keys(REQUEST_TABLE)
        } == {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
                constraint.ondelete,
            )
            for constraint in metadata_table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
    finally:
        engine.dispose()
