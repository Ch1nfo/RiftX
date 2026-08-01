import io
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from tests.integration.persistence.test_migrations import (
    _run_alembic_with_sqlite_foreign_keys,
    _seed_action_migration_rows,
    run_alembic,
)

BASE_REVISION = "b2c4d6e8f001"
CLOCK_REVISION = "c4d6e8f0a213"
FALLBACK_UTC = datetime.fromisoformat("1970-01-01 00:00:00.000000+00:00")


def _offline_sql(url: str, revision_range: str, *, downgrade: bool = False) -> str:
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    old_url = os.environ.get("RIFTX_DATABASE_URL")
    os.environ["RIFTX_DATABASE_URL"] = url
    try:
        if downgrade:
            command.downgrade(config, revision_range, sql=True)
        else:
            command.upgrade(config, revision_range, sql=True)
    finally:
        if old_url is None:
            os.environ.pop("RIFTX_DATABASE_URL", None)
        else:
            os.environ["RIFTX_DATABASE_URL"] = old_url
    return output.getvalue()


def _seed_execution_with_lifecycle_max(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO executions "
            "(id, execution_key, run_id, session_id, tool_call_id, attempt_group, node_id, "
            "executor_type, argv_json, cwd, env_diff_json, status, stdout_path, stderr_path, "
            "created_at, process_created_at, started_at, finished_at, "
            "physical_stop_confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                "execution-max",
                "execution-max-key",
                "run-action",
                "session-action",
                "intent-action",
                "initial",
                "local",
                "process",
                "[]",
                "/tmp/run-action",
                "{}",
                "completed",
                "/tmp/max.stdout.log",
                "/tmp/max.stderr.log",
                "2026-08-01 00:00:01+00:00",
                "2026-08-01 00:00:05+00:00",
                None,
                "2026-08-01 00:00:02+00:00",
                "2026-08-01 00:00:03+00:00",
            ),
        )
        connection.commit()


def test_mutation_clock_migration_backfills_and_preserves_foreign_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "mutation-clock.db"
    run_alembic(database_path, BASE_REVISION)
    _seed_action_migration_rows(database_path)
    _seed_execution_with_lifecycle_max(database_path)

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        intent_created_at, intent_updated_at = connection.execute(
            "SELECT created_at, updated_at FROM tool_call_intents WHERE id = 'intent-action'"
        ).fetchone()
        all_null_execution = connection.execute(
            "SELECT updated_at FROM executions WHERE id = 'execution-action'"
        ).fetchone()[0]
        max_execution = connection.execute(
            "SELECT updated_at FROM executions WHERE id = 'execution-max'"
        ).fetchone()[0]
        column_info = {
            table_name: {
                row[1]: (row[3], row[4])
                for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            for table_name in ("tool_call_intents", "executions")
        }
        approval = connection.execute(
            "SELECT tool_call_intent_id FROM runtime_approval_requests WHERE id = 'approval-action'"
        ).fetchone()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert datetime.fromisoformat(intent_updated_at) == datetime.fromisoformat(intent_created_at)
    assert datetime.fromisoformat(all_null_execution) == FALLBACK_UTC
    assert datetime.fromisoformat(max_execution) == datetime.fromisoformat(
        "2026-08-01 00:00:05+00:00"
    )
    assert column_info["tool_call_intents"]["updated_at"] == (1, None)
    assert column_info["executions"]["updated_at"] == (1, None)
    assert approval == ("intent-action",)
    assert violations == []

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )
    with sqlite3.connect(database_path) as connection:
        downgraded_columns = {
            table_name: {
                row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            for table_name in ("tool_call_intents", "executions")
        }
        approval_after_downgrade = connection.execute(
            "SELECT tool_call_intent_id FROM runtime_approval_requests WHERE id = 'approval-action'"
        ).fetchone()
        downgrade_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert "updated_at" not in downgraded_columns["tool_call_intents"]
    assert "updated_at" not in downgraded_columns["executions"]
    assert approval_after_downgrade == ("intent-action",)
    assert downgrade_violations == []


def test_mutation_clock_migration_compiles_for_postgresql_offline() -> None:
    url = "postgresql+asyncpg://riftx@localhost/riftx"
    upgrade_sql = _offline_sql(url, f"{BASE_REVISION}:{CLOCK_REVISION}")
    downgrade_sql = _offline_sql(
        url,
        f"{CLOCK_REVISION}:{BASE_REVISION}",
        downgrade=True,
    )

    assert "ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE" in upgrade_sql
    assert "UPDATE tool_call_intents SET updated_at=tool_call_intents.created_at" in upgrade_sql
    assert "UPDATE executions SET updated_at=coalesce" in upgrade_sql
    assert "1970-01-01 00:00:00.000000+00:00" in upgrade_sql
    assert "ALTER COLUMN updated_at SET NOT NULL" in upgrade_sql
    assert "CURRENT_TIMESTAMP" not in upgrade_sql
    assert downgrade_sql.count("DROP COLUMN updated_at") == 2


def test_mutation_clock_migration_rejects_sqlite_offline_batch_ddl() -> None:
    with pytest.raises(RuntimeError, match="SQLite.*requires an online database"):
        _offline_sql(
            "sqlite+aiosqlite:///offline.db",
            f"{BASE_REVISION}:{CLOCK_REVISION}",
        )
