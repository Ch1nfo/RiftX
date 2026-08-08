from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import ForeignKeyConstraint, create_engine, inspect
from tests.integration.persistence._audit_compat import (
    _create_audit,
    _create_engagement,
    _create_project,
    _project,
    _snapshot,
)
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql
from tests.integration.persistence.test_snapshot_references import _reference

from riftx.persistence import (
    Database,
    SQLAlchemySnapshotReferenceRepository,
    SQLAlchemySnapshotRepository,
)
from riftx.persistence.orm import Base

BASE_REVISION = "5d8c1a7e3b24"
REFERENCE_REVISION = "8a1f3c5e7b90"
REFERENCE_TABLE = "snapshot_references"


def test_snapshot_reference_upgrade_is_portable_and_matches_orm(tmp_path: Path) -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{REFERENCE_REVISION}",
    )
    assert "CREATE TABLE snapshot_references" in sql
    assert "GLOB" not in sql
    assert "FOREIGN KEY(audit_id, project_id)" in sql
    assert "FOREIGN KEY(snapshot_id, project_id)" in sql

    database_path = tmp_path / "snapshot-reference-migration.db"
    run_alembic(database_path, BASE_REVISION)
    assert REFERENCE_TABLE not in sqlite_tables(database_path)
    run_alembic(database_path, REFERENCE_REVISION)
    assert REFERENCE_TABLE in sqlite_tables(database_path)

    engine = create_engine(f"sqlite:///{database_path}")
    migrated = inspect(engine)
    orm_table = Base.metadata.tables[REFERENCE_TABLE]
    assert {column["name"] for column in migrated.get_columns(REFERENCE_TABLE)} == {
        column.name for column in orm_table.columns
    }
    migrated_fks = {
        tuple(foreign_key["constrained_columns"]): tuple(
            foreign_key["referred_columns"]
        )
        for foreign_key in migrated.get_foreign_keys(REFERENCE_TABLE)
    }
    orm_fks = {
        tuple(constraint.column_keys): tuple(element.column.name for element in constraint.elements)
        for constraint in orm_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert migrated_fks == orm_fks
    engine.dispose()


def test_empty_snapshot_reference_upgrade_downgrades_cleanly(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot-reference-empty.db"
    run_alembic(database_path, REFERENCE_REVISION)

    downgrade_alembic(database_path, BASE_REVISION)

    assert REFERENCE_TABLE not in sqlite_tables(database_path)


def test_durable_snapshot_reference_blocks_lossy_downgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot-reference-block.db"
    run_alembic(database_path, "head")

    async def seed() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            await _create_engagement(database, "engagement-1")
            await _create_project(database, _project())
            await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
            await _create_audit(database)
            await SQLAlchemySnapshotReferenceRepository(database.session_factory).add(
                _reference()
            )
        finally:
            await database.dispose()

    asyncio.run(seed())
    with pytest.raises(
        RuntimeError,
        match="durable Snapshot reference facts exist",
    ):
        downgrade_alembic(database_path, BASE_REVISION)
    assert REFERENCE_TABLE in sqlite_tables(database_path)
