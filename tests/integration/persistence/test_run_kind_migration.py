from __future__ import annotations

import pytest
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

BASE_REVISION = "f7a9c1d3e526"
RUN_KIND_REVISION = "0d3a8b7c4e21"


def test_run_kind_migration_compiles_for_postgresql_offline() -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{RUN_KIND_REVISION}",
    )

    assert "ADD COLUMN kind VARCHAR(32) DEFAULT 'general' NOT NULL" in sql
    assert "ALTER COLUMN kind DROP DEFAULT" in sql
    assert "ck_runs_kind" in sql
    assert "CREATE INDEX ix_runs_kind" in sql


def test_run_kind_migration_rejects_sqlite_offline_batch_ddl() -> None:
    with pytest.raises(RuntimeError, match="SQLite Run kind migration"):
        _offline_sql(
            "sqlite+aiosqlite:///offline.db",
            f"{BASE_REVISION}:{RUN_KIND_REVISION}",
        )
