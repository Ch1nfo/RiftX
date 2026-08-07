from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import ForeignKeyConstraint, create_engine, inspect
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

from riftx.persistence.orm import Base

BASE_REVISION = "8a1f3c5e7b90"
STATIC_EFFECT_REVISION = "9c2e4f6a8b10"
STATIC_EFFECT_TABLES = {
    "audit_static_effect_plans",
    "snapshot_mount_leases",
    "snapshot_mount_pins",
    "snapshot_mount_stop_proofs",
}


def test_static_effect_upgrade_is_portable_and_matches_orm(tmp_path: Path) -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{STATIC_EFFECT_REVISION}",
    )
    for table_name in STATIC_EFFECT_TABLES:
        assert f"CREATE TABLE {table_name}" in sql
    assert "GLOB" not in sql

    database_path = tmp_path / "static-effect-migration.db"
    run_alembic(database_path, BASE_REVISION)
    assert STATIC_EFFECT_TABLES.isdisjoint(sqlite_tables(database_path))
    run_alembic(database_path, STATIC_EFFECT_REVISION)
    assert STATIC_EFFECT_TABLES <= sqlite_tables(database_path)

    engine = create_engine(f"sqlite:///{database_path}")
    migrated = inspect(engine)
    for table_name in STATIC_EFFECT_TABLES:
        orm_table = Base.metadata.tables[table_name]
        assert {column["name"] for column in migrated.get_columns(table_name)} == {
            column.name for column in orm_table.columns
        }
        migrated_fks = {
            tuple(foreign_key["constrained_columns"]): tuple(
                foreign_key["referred_columns"]
            )
            for foreign_key in migrated.get_foreign_keys(table_name)
        }
        orm_fks = {
            tuple(constraint.column_keys): tuple(
                element.column.name for element in constraint.elements
            )
            for constraint in orm_table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        assert migrated_fks == orm_fks
    engine.dispose()


def test_empty_static_effect_upgrade_downgrades_cleanly(tmp_path: Path) -> None:
    database_path = tmp_path / "static-effect-empty.db"
    run_alembic(database_path, STATIC_EFFECT_REVISION)

    downgrade_alembic(database_path, BASE_REVISION)

    assert STATIC_EFFECT_TABLES.isdisjoint(sqlite_tables(database_path))


def test_static_effect_fact_blocks_lossy_downgrade_before_any_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "static-effect-block.db"
    run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO audit_static_effect_plans "
            "(id, schema_version, canonical_json, plan_digest, project_id, audit_id, "
            "run_id, snapshot_id, snapshot_reference_role, snapshot_digest, "
            "manifest_digest, operation_family, node_id, backend_id, backend_digest, "
            "created_by_policy, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            (
                "historical-static-effect-plan",
                "riftx.audit-static-effect-plan/v1",
                "{}",
                "1" * 64,
                "historical-project",
                "historical-audit",
                "historical-run",
                "historical-snapshot",
                "primary",
                "2" * 64,
                "3" * 64,
                "snapshot_mount",
                "local",
                "private_materialization",
                "4" * 64,
                "riftx_policy",
                "2026-08-04 12:00:00.000000",
            ),
        )
        connection.commit()
    with pytest.raises(
        RuntimeError,
        match="durable static effect authority facts exist",
    ):
        downgrade_alembic(database_path, BASE_REVISION)
    assert STATIC_EFFECT_TABLES <= sqlite_tables(database_path)
