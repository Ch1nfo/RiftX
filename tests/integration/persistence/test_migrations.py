import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from tests.unit.persistence.test_schema import EXPECTED_TABLES


def run_alembic(database_path: Path, revision: str) -> None:
    config = Config("alembic.ini")
    old_url = os.environ.get("RIFTX_DATABASE_URL")
    os.environ["RIFTX_DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    try:
        command.upgrade(config, revision) if revision != "base" else command.downgrade(
            config, revision
        )
    finally:
        if old_url is None:
            os.environ.pop("RIFTX_DATABASE_URL", None)
        else:
            os.environ["RIFTX_DATABASE_URL"] = old_url


def sqlite_tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {row[0] for row in rows}


def test_initial_migration_upgrades_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"

    run_alembic(database_path, "head")
    assert sqlite_tables(database_path) == EXPECTED_TABLES

    run_alembic(database_path, "base")
    assert sqlite_tables(database_path) == {"alembic_version"}


def test_m7_migration_backfills_existing_approval_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "existing.db"
    run_alembic(database_path, "2f14cbcea74b")
    now = "2026-07-29 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO engagements "
            "(id, name, description, authorization_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("engagement-1", "Existing", "", None, now, now),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, engagement_id, node_id, objective, success_criteria_json, "
            "entry_points_json, scope_json, status, approval_mode, workspace_path, "
            "temporal_workflow_id, created_at, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                "engagement-1",
                "local",
                "Existing run",
                "[]",
                "[]",
                "{}",
                "waiting_approval",
                "balanced",
                "/tmp/run-1",
                "workflow-1",
                now,
                None,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO tool_calls "
            "(id, run_id, agent_step_id, tool_id, skill_id, arguments_json, "
            "approval_status, execution_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("call-1", "run-1", "step-1", "tool-1", None, "{}", "pending", None, now),
        )
        connection.execute(
            "INSERT INTO approvals "
            "(id, run_id, tool_call_id, status, reason, decided_by, created_at, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("approval-1", "run-1", "call-1", "pending", "Existing", None, now, None),
        )
        connection.commit()

    run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        sdk_call_id = connection.execute(
            "SELECT sdk_call_id FROM tool_calls WHERE id = 'call-1'"
        ).fetchone()[0]
        command_json, env_diff_json = connection.execute(
            "SELECT command_json, env_diff_json FROM approvals WHERE id = 'approval-1'"
        ).fetchone()
        tool_columns = {
            row[1]: row[3] for row in connection.execute("PRAGMA table_info(tool_calls)").fetchall()
        }
        approval_columns = {
            row[1]: row[3] for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
        }
    assert sdk_call_id == "call-1"
    assert command_json == "[]"
    assert env_diff_json == "{}"
    assert tool_columns["sdk_call_id"] == 1
    assert approval_columns["command_json"] == 1
    assert approval_columns["env_diff_json"] == 1


def test_m9_migration_backfills_existing_node_lifecycle_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "existing-node.db"
    run_alembic(database_path, "6b5e4f7a8c91")
    now = "2026-07-29 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO nodes "
            "(id, name, platform, architecture, status, labels_json, last_seen_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("node-1", "Existing Node", "linux", "x86_64", "online", "{}", now, now),
        )
        connection.commit()

    run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        runner_version, capabilities_json, updated_at = connection.execute(
            "SELECT runner_version, capabilities_json, updated_at FROM nodes WHERE id = 'node-1'"
        ).fetchone()
        columns = {
            row[1]: row[3] for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
        }
    assert runner_version == "unknown"
    assert capabilities_json == "[]"
    assert updated_at == now
    assert columns["runner_version"] == 1
    assert columns["capabilities_json"] == 1
    assert columns["updated_at"] == 1
