import logging
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


def downgrade_alembic(database_path: Path, revision: str) -> None:
    config = Config("alembic.ini")
    old_url = os.environ.get("RIFTX_DATABASE_URL")
    os.environ["RIFTX_DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    try:
        command.downgrade(config, revision)
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


def test_migration_logging_keeps_existing_application_loggers_enabled(
    tmp_path: Path,
) -> None:
    application_logger = logging.getLogger("riftx.runner.daemon")
    original_disabled = application_logger.disabled
    application_logger.disabled = False
    try:
        run_alembic(tmp_path / "logger-state.db", "head")
        assert application_logger.disabled is False
    finally:
        application_logger.disabled = original_disabled


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


def test_runner_owner_fencing_migration_backfills_existing_control_state(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-owner-fencing.db"
    run_alembic(database_path, "f1c7a9e3d502")
    now = "2026-08-01 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO nodes "
            "(id, name, platform, architecture, runner_version, status, capabilities_json, "
            "labels_json, last_seen_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "node-1",
                "Existing Runner",
                "linux",
                "x86_64",
                "1.0.0",
                "online",
                "[]",
                "{}",
                now,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO nodes "
            "(id, name, platform, architecture, runner_version, status, capabilities_json, "
            "labels_json, last_seen_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "node-without-credential",
                "Unregistered Runner",
                "linux",
                "x86_64",
                "1.0.0",
                "unknown",
                "[]",
                "{}",
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO engagements "
            "(id, name, description, authorization_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("engagement-owner-fencing", "Existing", "", None, now, now),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, engagement_id, node_id, objective, success_criteria_json, "
            "entry_points_json, scope_json, status, approval_mode, model_profile, "
            "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-owner-fencing",
                "engagement-owner-fencing",
                "node-1",
                "Existing run",
                "[]",
                "[]",
                "{}",
                "running",
                "balanced",
                None,
                "/tmp/run-owner-fencing",
                "workflow-owner-fencing",
                now,
                now,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO executions "
            "(id, execution_key, run_id, node_id, executor_type, argv_json, cwd, "
            "env_diff_json, status, stdout_path, stderr_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "execution-owner-fencing",
                "execution-owner-fencing-key",
                "run-owner-fencing",
                "node-1",
                "process",
                "[]",
                "/tmp/run-owner-fencing",
                "{}",
                "running",
                "/tmp/stdout.log",
                "/tmp/stderr.log",
            ),
        )
        connection.execute(
            "INSERT INTO runner_credentials "
            "(node_id, token_hash, token_prefix, created_at, rotated_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("node-1", "a" * 64, "existing", now, now, None),
        )
        connection.execute(
            "INSERT INTO runner_commands "
            "(id, node_id, kind, idempotency_key, payload_json, status, attempts, "
            "lease_id, lease_expires_at, result_json, error, created_at, updated_at, "
            "completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "command-1",
                "node-1",
                "cancel",
                "cancel:existing",
                "{}",
                "pending",
                0,
                None,
                None,
                "{}",
                "",
                now,
                now,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO runner_commands "
            "(id, node_id, kind, idempotency_key, payload_json, status, attempts, "
            "lease_id, lease_expires_at, result_json, error, created_at, updated_at, "
            "completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "command-without-credential",
                "node-without-credential",
                "cancel",
                "cancel:unbound",
                "{}",
                "pending",
                0,
                None,
                None,
                "{}",
                "",
                now,
                now,
                None,
            ),
        )
        connection.commit()

    run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        instance_id, epoch, token_hash = connection.execute(
            "SELECT runner_instance_id, runner_epoch, token_hash "
            "FROM runner_credentials WHERE node_id = 'node-1'"
        ).fetchone()
        node_owner = connection.execute(
            "SELECT current_runner_instance_id, current_runner_epoch FROM nodes WHERE id = 'node-1'"
        ).fetchone()
        command_target = connection.execute(
            "SELECT target_runner_instance_id, target_runner_epoch "
            "FROM runner_commands WHERE id = 'command-1'"
        ).fetchone()
        unowned_node = connection.execute(
            "SELECT current_runner_instance_id, current_runner_epoch "
            "FROM nodes WHERE id = 'node-without-credential'"
        ).fetchone()
        unbound_command = connection.execute(
            "SELECT target_runner_instance_id, target_runner_epoch "
            "FROM runner_commands WHERE id = 'command-without-credential'"
        ).fetchone()
        execution_owner = connection.execute(
            "SELECT owner_runner_instance_id, owner_runner_epoch "
            "FROM executions WHERE id = 'execution-owner-fencing'"
        ).fetchone()
        execution_columns = {
            row[1]: row[3] for row in connection.execute("PRAGMA table_info(executions)").fetchall()
        }
        credential_primary_key = {
            row[1]: row[5]
            for row in connection.execute("PRAGMA table_info(runner_credentials)").fetchall()
        }

    assert instance_id
    assert epoch == 1
    assert token_hash == "a" * 64
    assert node_owner == (instance_id, 1)
    assert command_target == (instance_id, 1)
    assert unowned_node == (None, 0)
    assert unbound_command == (None, None)
    # A migration cannot safely guess which generation owns an in-flight
    # process. Phase 2 binds all newly admitted remote executions explicitly.
    assert execution_owner == (None, None)
    assert execution_columns["owner_runner_instance_id"] == 0
    assert execution_columns["owner_runner_epoch"] == 0
    assert credential_primary_key["runner_instance_id"] == 1
    assert credential_primary_key["node_id"] == 0

    downgrade_alembic(database_path, "f1c7a9e3d502")
    with sqlite3.connect(database_path) as connection:
        legacy_credential = connection.execute(
            "SELECT node_id, token_hash FROM runner_credentials"
        ).fetchone()
        command_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runner_commands)")
        }
        node_columns = {row[1] for row in connection.execute("PRAGMA table_info(nodes)")}
    assert legacy_credential == ("node-1", "a" * 64)
    assert "target_runner_instance_id" not in command_columns
    assert "current_runner_instance_id" not in node_columns


def test_execution_stop_proof_migration_upgrades_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "execution-stop-proof.db"
    run_alembic(database_path, "f6a1d9c3e805")
    with sqlite3.connect(database_path) as connection:
        before = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
    assert "physical_stop_confirmed_at" not in before

    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        upgraded = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(executions)").fetchall()
        }
    assert upgraded["physical_stop_confirmed_at"] == 0

    downgrade_alembic(database_path, "f6a1d9c3e805")
    with sqlite3.connect(database_path) as connection:
        downgraded = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
    assert "physical_stop_confirmed_at" not in downgraded
