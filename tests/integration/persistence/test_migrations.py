import logging
import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine
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


def offline_downgrade_alembic(revision_range: str) -> None:
    config = Config("alembic.ini")
    old_url = os.environ.get("RIFTX_DATABASE_URL")
    os.environ["RIFTX_DATABASE_URL"] = "postgresql+asyncpg://riftx@localhost/riftx"
    try:
        command.downgrade(config, revision_range, sql=True)
    finally:
        if old_url is None:
            os.environ.pop("RIFTX_DATABASE_URL", None)
        else:
            os.environ["RIFTX_DATABASE_URL"] = old_url


def _run_alembic_with_sqlite_foreign_keys(
    database_path: Path,
    revision: str,
    *,
    downgrade: bool = False,
) -> None:
    def enable_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    event.listen(Engine, "connect", enable_foreign_keys)
    try:
        if downgrade:
            downgrade_alembic(database_path, revision)
        else:
            run_alembic(database_path, revision)
    finally:
        event.remove(Engine, "connect", enable_foreign_keys)


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


def _insert_pre_run_kind_graph(database_path: Path) -> None:
    now = "2026-08-02 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO engagements "
            "(id, name, description, authorization_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("engagement-run-kind", "Run kind migration", "", None, now, now),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, engagement_id, node_id, objective, success_criteria_json, "
            "entry_points_json, scope_json, status, approval_mode, model_profile, "
            "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-run-kind",
                "engagement-run-kind",
                "local",
                "Existing general Run",
                "[]",
                "[]",
                "{}",
                "created",
                "balanced",
                None,
                "/tmp/legacy-run-kind",
                "workflow-legacy-run-kind",
                now,
                None,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO run_events "
            "(id, run_id, sequence, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "legacy-run-kind-event",
                "legacy-run-kind",
                1,
                "run.created",
                '{"status":"created"}',
                now,
            ),
        )
        connection.commit()


def test_run_kind_migration_backfills_without_default_and_preserves_fk_graph(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "run-kind-upgrade.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, "f7a9c1d3e526")
    _insert_pre_run_kind_graph(database_path)

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        kind = connection.execute(
            "SELECT kind FROM runs WHERE id = 'legacy-run-kind'"
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT count(*) FROM run_events WHERE run_id = 'legacy-run-kind'"
        ).fetchone()[0]
        columns = {
            row[1]: {"not_null": row[3], "default": row[4]}
            for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(runs)").fetchall()
        }
        create_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()[0]

        assert kind == "general"
        assert event_count == 1
        assert columns["kind"] == {"not_null": 1, "default": None}
        assert "ix_runs_kind" in indexes
        assert "ck_runs_kind" in create_sql

        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            connection.execute(
                "INSERT INTO runs "
                "(id, engagement_id, node_id, objective, success_criteria_json, "
                "entry_points_json, scope_json, status, approval_mode, workspace_path, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "missing-run-kind",
                    "engagement-run-kind",
                    "local",
                    "Must not inherit a kind",
                    "[]",
                    "[]",
                    "{}",
                    "created",
                    "balanced",
                    "/tmp/missing-run-kind",
                    "2026-08-02 00:00:00+00:00",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="ck_runs_kind"):
            connection.execute(
                "INSERT INTO runs "
                "(id, engagement_id, kind, node_id, objective, success_criteria_json, "
                "entry_points_json, scope_json, status, approval_mode, workspace_path, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "unknown-run-kind",
                    "engagement-run-kind",
                    "unknown",
                    "local",
                    "Must reject an unknown kind",
                    "[]",
                    "[]",
                    "{}",
                    "created",
                    "balanced",
                    "/tmp/unknown-run-kind",
                    "2026-08-02 00:00:00+00:00",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="ck_runs_kind"):
            connection.execute(
                "UPDATE runs SET kind = 'agent' WHERE id = 'legacy-run-kind'"
            )
        assert connection.execute(
            "SELECT kind FROM runs WHERE id = 'legacy-run-kind'"
        ).fetchone()[0] == "general"

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        "f7a9c1d3e526",
        downgrade=True,
    )
    with sqlite3.connect(database_path) as connection:
        assert "kind" not in {
            row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert connection.execute(
            "SELECT count(*) FROM runs WHERE id = 'legacy-run-kind'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM run_events WHERE run_id = 'legacy-run-kind'"
        ).fetchone()[0] == 1

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT kind FROM runs WHERE id = 'legacy-run-kind'"
        ).fetchone()[0] == "general"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_run_kind_migration_rejects_lossy_code_audit_downgrade_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "run-kind-downgrade.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    now = "2026-08-02 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO engagements "
            "(id, name, description, authorization_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("engagement-audit", "Audit", "", None, now, now),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, engagement_id, kind, node_id, objective, success_criteria_json, "
            "entry_points_json, scope_json, status, approval_mode, workspace_path, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "code-audit-run",
                "engagement-audit",
                "code_audit",
                "local",
                "Protected Audit Run",
                "[]",
                "[]",
                "{}",
                "created",
                "balanced",
                "/tmp/code-audit-run",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO run_events "
            "(id, run_id, sequence, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("audit-event", "code-audit-run", 1, "run.created", "{}", now),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="cannot downgrade Run kind"):
        _run_alembic_with_sqlite_foreign_keys(
            database_path,
            "f7a9c1d3e526",
            downgrade=True,
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "0d3a8b7c4e21"
        )
        assert connection.execute(
            "SELECT kind FROM runs WHERE id = 'code-audit-run'"
        ).fetchone()[0] == "code_audit"
        assert connection.execute(
            "SELECT count(*) FROM run_events WHERE run_id = 'code-audit-run'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_run_kind_offline_downgrade_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="requires an online database"):
        offline_downgrade_alembic("0d3a8b7c4e21:f7a9c1d3e526")


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
            row[1]: row[3] for row in connection.execute("PRAGMA table_info(executions)").fetchall()
        }
    assert upgraded["physical_stop_confirmed_at"] == 0

    downgrade_alembic(database_path, "f6a1d9c3e805")
    with sqlite3.connect(database_path) as connection:
        downgraded = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
    assert "physical_stop_confirmed_at" not in downgraded


def test_execution_launch_fingerprint_migration_preserves_legacy_null_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "execution-launch-fingerprint.db"
    run_alembic(database_path, "e6f8a0b2c415")
    now = "2026-08-01 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO engagements "
            "(id, name, description, authorization_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("engagement-fingerprint", "Fingerprint", "", None, now, now),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, engagement_id, node_id, objective, success_criteria_json, "
            "entry_points_json, scope_json, status, approval_mode, model_profile, "
            "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-fingerprint",
                "engagement-fingerprint",
                "local",
                "Fingerprint migration",
                "[]",
                "[]",
                "{}",
                "running",
                "balanced",
                None,
                "/tmp/run-fingerprint",
                None,
                now,
                now,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO executions "
            "(id, execution_key, run_id, session_id, tool_call_id, attempt_group, "
            "node_id, executor_type, argv_json, cwd, env_diff_json, status, "
            "stdout_path, stderr_path, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-fingerprint-execution",
                "legacy-fingerprint-key",
                "run-fingerprint",
                None,
                None,
                "initial",
                "local",
                "process",
                "[]",
                "/tmp/run-fingerprint",
                "{}",
                "created",
                "/tmp/fingerprint.stdout",
                "/tmp/fingerprint.stderr",
                now,
            ),
        )
        connection.commit()
    with sqlite3.connect(database_path) as connection:
        before = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
    assert "launch_fingerprint" not in before

    run_alembic(database_path, "f7a9c1d3e526")
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        upgraded = {
            row[1]: (row[2], row[3])
            for row in connection.execute("PRAGMA table_info(executions)").fetchall()
        }
        legacy_value = connection.execute(
            "SELECT launch_fingerprint FROM executions WHERE id = 'legacy-fingerprint-execution'"
        ).fetchone()
    assert version == ("f7a9c1d3e526",)
    assert upgraded["launch_fingerprint"] == ("VARCHAR(80)", 0)
    assert legacy_value == (None,)

    downgrade_alembic(database_path, "e6f8a0b2c415")
    with sqlite3.connect(database_path) as connection:
        downgraded = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
        preserved = connection.execute(
            "SELECT execution_key FROM executions WHERE id = 'legacy-fingerprint-execution'"
        ).fetchone()
    assert "launch_fingerprint" not in downgraded
    assert preserved == ("legacy-fingerprint-key",)

    run_alembic(database_path, "f7a9c1d3e526")
    with sqlite3.connect(database_path) as connection:
        reupgraded = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
        legacy_value = connection.execute(
            "SELECT launch_fingerprint FROM executions WHERE id = 'legacy-fingerprint-execution'"
        ).fetchone()
    assert "launch_fingerprint" in reupgraded
    assert legacy_value == (None,)


def _seed_action_migration_rows(database_path: Path) -> None:
    now = "2026-08-01 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO engagements "
            "(id, name, description, authorization_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("engagement-action", "Action", "", None, now, now),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, engagement_id, node_id, objective, success_criteria_json, "
            "entry_points_json, scope_json, status, approval_mode, model_profile, "
            "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-action",
                "engagement-action",
                "local",
                "Action migration",
                "[]",
                "[]",
                "{}",
                "running",
                "balanced",
                None,
                "/tmp/run-action",
                None,
                now,
                now,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO agent_sessions "
            "(id, run_id, parent_session_id, agent_type, model_profile, status, "
            "latest_checkpoint_id, provider_state_id, turn_count, model_call_count, "
            "tool_call_count, created_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "session-action",
                "run-action",
                None,
                "primary",
                "default",
                "active",
                None,
                None,
                0,
                0,
                0,
                now,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO agent_cycles "
            "(id, run_id, session_id, sequence, status, yield_reason, waiting_object_id, "
            "checkpoint_id, model_call_count, tool_call_count, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "cycle-action",
                "run-action",
                "session-action",
                1,
                "running",
                None,
                None,
                None,
                0,
                0,
                now,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO agent_steps "
            "(id, cycle_id, sequence, step_type, status, input_refs_json, output_refs_json, "
            "started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "step-action",
                "cycle-action",
                1,
                "tool_proposal",
                "completed",
                "[]",
                "[]",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO executions "
            "(id, execution_key, run_id, session_id, tool_call_id, attempt_group, node_id, "
            "executor_type, argv_json, cwd, env_diff_json, status, stdout_path, stderr_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "execution-action",
                "execution-action-key",
                "run-action",
                "session-action",
                None,
                "initial",
                "local",
                "process",
                "[]",
                "/tmp/run-action",
                "{}",
                "created",
                "/tmp/stdout.log",
                "/tmp/stderr.log",
            ),
        )
        connection.execute(
            "INSERT INTO tool_call_intents "
            "(id, run_id, session_id, cycle_id, step_id, tool_id, skill_id, arguments_json, "
            "command_preview, reason, target_summary, approval_level, status, engine_call_id, "
            "execution_spec_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "intent-action",
                "run-action",
                "session-action",
                "cycle-action",
                "step-action",
                "python",
                None,
                "{}",
                "",
                "migration approval",
                None,
                "sensitive",
                "awaiting_approval",
                "engine-action",
                None,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO runtime_approval_requests "
            "(id, run_id, session_id, cycle_id, tool_call_intent_id, "
            "context_compilation_id, working_memory_version, provider_state_id, status, "
            "decision, feedback, decided_by, created_at, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "approval-action",
                "run-action",
                "session-action",
                "cycle-action",
                "intent-action",
                None,
                None,
                None,
                "pending",
                None,
                None,
                None,
                now,
                None,
            ),
        )
        connection.commit()


def _insert_wide_action_migration_rows(database_path: Path) -> str:
    intent_id = "intent-" + "a" * 70
    assert len(intent_id) == 77
    now = "2026-08-01 00:00:01+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO tool_call_intents "
            "(id, run_id, session_id, cycle_id, step_id, tool_id, skill_id, arguments_json, "
            "command_preview, reason, target_summary, approval_level, status, engine_call_id, "
            "execution_spec_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                intent_id,
                "run-action",
                "session-action",
                "cycle-action",
                "step-action",
                "python",
                None,
                "{}",
                "",
                "wide migration ID",
                None,
                "sensitive",
                "proposed",
                "engine-wide",
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO runtime_approval_requests "
            "(id, run_id, session_id, cycle_id, tool_call_intent_id, "
            "context_compilation_id, working_memory_version, provider_state_id, status, "
            "decision, feedback, decided_by, created_at, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "approval-wide",
                "run-action",
                "session-action",
                "cycle-action",
                intent_id,
                None,
                None,
                None,
                "pending",
                None,
                None,
                None,
                now,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO executions "
            "(id, execution_key, run_id, session_id, tool_call_id, attempt_group, node_id, "
            "executor_type, argv_json, cwd, env_diff_json, status, stdout_path, stderr_path, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "execution-wide",
                "execution-wide-key",
                "run-action",
                "session-action",
                intent_id,
                "initial",
                "local",
                "process",
                "[]",
                "/tmp/run-action",
                "{}",
                "created",
                "/tmp/wide.stdout.log",
                "/tmp/wide.stderr.log",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO target_http_requests "
            "(id, execution_key, run_id, session_id, tool_call_id, node_id, method, url, "
            "request_json, result_json, request_artifact_id, response_artifact_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "target-wide",
                "target-wide-key",
                "run-action",
                "session-action",
                intent_id,
                "local",
                "GET",
                "https://example.invalid/",
                "{}",
                "{}",
                None,
                None,
                now,
            ),
        )
        connection.commit()
    return intent_id


def test_action_read_foundation_migration_preserves_unknown_legacy_order(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "action-read-foundation.db"
    run_alembic(database_path, "fa72b4c8d901")
    _seed_action_migration_rows(database_path)

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    wide_intent_id = _insert_wide_action_migration_rows(database_path)

    with sqlite3.connect(database_path) as connection:
        column_types = {
            table: {
                row[1]: row[2]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for table in (
                "tool_call_intents",
                "runtime_approval_requests",
                "executions",
                "target_http_requests",
                "approvals",
            )
        }
        legacy_created_at = connection.execute(
            "SELECT created_at FROM executions WHERE id = 'execution-action'"
        ).fetchone()[0]
        intent_index = tuple(
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(ix_tool_call_intents_run_created_id)"
            ).fetchall()
        )
        execution_index = tuple(
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(ix_executions_run_tool_created_id)"
            ).fetchall()
        )
        preserved_approval = connection.execute(
            "SELECT tool_call_intent_id FROM runtime_approval_requests WHERE id = 'approval-action'"
        ).fetchone()
        wide_references = (
            connection.execute(
                "SELECT id FROM tool_call_intents WHERE id = ?", (wide_intent_id,)
            ).fetchone(),
            connection.execute(
                "SELECT tool_call_intent_id FROM runtime_approval_requests "
                "WHERE id = 'approval-wide'"
            ).fetchone(),
            connection.execute(
                "SELECT tool_call_id FROM executions WHERE id = 'execution-wide'"
            ).fetchone(),
            connection.execute(
                "SELECT tool_call_id FROM target_http_requests WHERE id = 'target-wide'"
            ).fetchone(),
        )
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert column_types["tool_call_intents"]["id"] == "VARCHAR(128)"
    assert column_types["runtime_approval_requests"]["tool_call_intent_id"] == "VARCHAR(128)"
    assert column_types["executions"]["tool_call_id"] == "VARCHAR(128)"
    assert column_types["target_http_requests"]["tool_call_id"] == "VARCHAR(128)"
    assert column_types["approvals"]["tool_call_id"] == "VARCHAR(64)"
    assert legacy_created_at is None
    assert intent_index == ("run_id", "created_at", "id")
    assert execution_index == ("run_id", "tool_call_id", "created_at", "id")
    assert preserved_approval == ("intent-action",)
    assert wide_references == ((wide_intent_id,),) * 4
    assert foreign_key_violations == []


def test_action_read_foundation_downgrade_preserves_runtime_approval_fk(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "action-read-foundation-downgrade.db"
    run_alembic(database_path, "fa72b4c8d901")
    _seed_action_migration_rows(database_path)
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        "fa72b4c8d901",
        downgrade=True,
    )

    with sqlite3.connect(database_path) as connection:
        preserved = connection.execute(
            "SELECT id, tool_call_intent_id, status FROM runtime_approval_requests "
            "WHERE id = 'approval-action'"
        ).fetchone()
        intent = connection.execute(
            "SELECT id FROM tool_call_intents WHERE id = 'intent-action'"
        ).fetchone()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert preserved == ("approval-action", "intent-action", "pending")
    assert intent == ("intent-action",)
    assert violations == []


def test_action_read_foundation_downgrade_refuses_long_intent_ids_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "action-read-downgrade.db"
    run_alembic(database_path, "fa72b4c8d901")
    _seed_action_migration_rows(database_path)
    run_alembic(database_path, "head")
    long_intent_id = "tool-call:v1:" + "a" * 64
    now = "2026-08-01 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO tool_call_intents "
            "(id, run_id, session_id, cycle_id, step_id, tool_id, skill_id, arguments_json, "
            "command_preview, reason, target_summary, approval_level, status, engine_call_id, "
            "execution_spec_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                long_intent_id,
                "run-action",
                "session-action",
                "cycle-action",
                "step-action",
                "python",
                None,
                "{}",
                "",
                "",
                None,
                "sensitive",
                "proposed",
                "engine-action",
                None,
                now,
                now,
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="cannot downgrade.*longer than 64"):
        downgrade_alembic(database_path, "fa72b4c8d901")

    with sqlite3.connect(database_path) as connection:
        execution_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(executions)").fetchall()
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert "created_at" in execution_columns
    assert revision != "fa72b4c8d901"


def test_action_read_foundation_downgrade_requires_online_width_preflight() -> None:
    with pytest.raises(RuntimeError, match="online.*width preflight"):
        offline_downgrade_alembic("b2c4d6e8f001:fa72b4c8d901")
