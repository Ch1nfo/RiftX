from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_migrations import (
    _run_alembic_with_sqlite_foreign_keys,
    _seed_action_migration_rows,
    run_alembic,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

BASE_REVISION = "c4d6e8f0a213"
CLAIM_REVISION = "d5e7f9a1b304"
SEED_REVISION = "b2c4d6e8f001"


def test_claim_migration_preserves_sqlite_graph_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "tool-call-claim.db"
    run_alembic(database_path, SEED_REVISION)
    _seed_action_migration_rows(database_path)
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: (row[2], row[3], row[4])
            for row in connection.execute("PRAGMA table_info(tool_call_intents)").fetchall()
        }
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(tool_call_intents)")}
        claim = connection.execute(
            "SELECT claimed_execution_key, claimed_attempt_group "
            "FROM tool_call_intents WHERE id = 'intent-action'"
        ).fetchone()
        approval = connection.execute(
            "SELECT tool_call_intent_id FROM runtime_approval_requests WHERE id = 'approval-action'"
        ).fetchone()
        create_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tool_call_intents'"
        ).fetchone()[0]
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE tool_call_intents SET claimed_execution_key = 'partial' "
                "WHERE id = 'intent-action'"
            )

    assert columns["claimed_execution_key"] == ("VARCHAR(255)", 0, None)
    assert columns["claimed_attempt_group"] == ("VARCHAR(64)", 0, None)
    assert "ix_tool_call_intents_execution_claim" in indexes
    assert "ck_tool_call_intents_execution_claim_pair" in create_sql
    assert claim == (None, None)
    assert approval == ("intent-action",)
    assert violations == []

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )
    with sqlite3.connect(database_path) as connection:
        downgraded_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_call_intents)")
        }
        approval_after_downgrade = connection.execute(
            "SELECT tool_call_intent_id FROM runtime_approval_requests WHERE id = 'approval-action'"
        ).fetchone()
        downgrade_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert "claimed_execution_key" not in downgraded_columns
    assert "claimed_attempt_group" not in downgraded_columns
    assert approval_after_downgrade == ("intent-action",)
    assert downgrade_violations == []


def test_claim_migration_compiles_for_postgresql_offline() -> None:
    url = "postgresql+asyncpg://riftx@localhost/riftx"
    upgrade_sql = _offline_sql(url, f"{BASE_REVISION}:{CLAIM_REVISION}")
    downgrade_sql = _offline_sql(
        url,
        f"{CLAIM_REVISION}:{BASE_REVISION}",
        downgrade=True,
    )

    assert "ADD COLUMN claimed_execution_key VARCHAR(255)" in upgrade_sql
    assert "ADD COLUMN claimed_attempt_group VARCHAR(64)" in upgrade_sql
    assert "ck_tool_call_intents_execution_claim_pair" in upgrade_sql
    assert "CREATE INDEX ix_tool_call_intents_execution_claim" in upgrade_sql
    assert "DEFAULT" not in upgrade_sql
    assert "DROP INDEX ix_tool_call_intents_execution_claim" in downgrade_sql
    assert downgrade_sql.count("DROP COLUMN") == 2


def test_claim_migration_rejects_sqlite_offline_batch_ddl() -> None:
    with pytest.raises(RuntimeError, match="SQLite Tool Call execution claim migration"):
        _offline_sql(
            "sqlite+aiosqlite:///offline.db",
            f"{BASE_REVISION}:{CLAIM_REVISION}",
        )
