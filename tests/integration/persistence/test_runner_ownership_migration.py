from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from tests.integration.persistence.test_audit_migration import (
    _insert_engagement,
    _insert_run,
)
from tests.integration.persistence.test_migrations import (
    _run_alembic_with_sqlite_foreign_keys,
    sqlite_tables,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

from riftx.domain import ExecutionStatus, RunnerPrincipal
from riftx.domain.base import utc_now
from riftx.persistence.database import Database
from riftx.persistence.repositories import SQLAlchemyRunnerCommandRepository

EARLIEST_REVISION = "2f14cbcea74b"
BASE_REVISION = "91e6f4a2c8b7"
RUNNER_REVISION = "8d7c2e4f1a90"
HEAD_REVISION = "d0b4e6f8a102"
NOW = "2026-08-03 12:00:00+00:00"
LEASE_EXPIRES_AT = "2026-08-03 12:05:00+00:00"
GRAPH_RUN_ID = "runner-migration-run"
GRAPH_EXECUTION_ID = "runner-migration-execution"
GRAPH_ARTIFACT_ID = "runner-migration-artifact"

RUNNER_TABLES = {
    "runner_effect_bindings",
    "runner_command_ownerships",
    "runner_stop_receipts",
    "runner_stop_projections",
}
RUNNER_MIGRATION = run_path(
    str(
        Path(__file__).parents[3]
        / "migrations/versions/8d7c2e4f1a90_add_runner_command_ownership.py"
    )
)


def test_runner_ownership_upgrade_compiles_for_postgresql_offline() -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{RUNNER_REVISION}",
    )

    assert "LOCK TABLE runner_commands, executions IN ACCESS EXCLUSIVE MODE" in sql
    for table_name in RUNNER_TABLES:
        assert f"CREATE TABLE {table_name}" in sql
    assert "INSERT INTO runner_command_ownerships" in sql


def test_runner_ownership_downgrade_refuses_postgresql_offline() -> None:
    with pytest.raises(RuntimeError, match="requires an online database"):
        _offline_sql(
            "postgresql+asyncpg://riftx@localhost/riftx",
            f"{RUNNER_REVISION}:{BASE_REVISION}",
            downgrade=True,
        )


def test_postgresql_online_downgrade_locks_all_runner_fact_tables_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []

    class StopAfterFirstRead(RuntimeError):
        pass

    class RecordingConnection:
        dialect = postgresql.dialect()

        @staticmethod
        def exec_driver_sql(statement: str) -> None:
            events.append(("lock", statement))

        @staticmethod
        def execute(statement: object) -> None:
            events.append(("read", str(statement)))
            raise StopAfterFirstRead

    migration_op = RUNNER_MIGRATION["op"]
    monkeypatch.setattr(
        migration_op,
        "get_context",
        lambda: SimpleNamespace(as_sql=False, dialect=postgresql.dialect()),
    )
    monkeypatch.setattr(migration_op, "get_bind", RecordingConnection)

    with pytest.raises(StopAfterFirstRead):
        RUNNER_MIGRATION["downgrade"]()

    assert events == [
        (
            "lock",
            "LOCK TABLE runner_commands, executions, runner_credentials, "
            "runner_command_ownerships, runner_effect_bindings, runner_stop_receipts, "
            "runner_stop_projections IN ACCESS EXCLUSIVE MODE",
        ),
        (
            "read",
            "SELECT 1 FROM runner_command_ownerships WHERE verification_state <> "
            "'quarantined' OR schema_version IS NOT NULL OR effect_binding_id IS NOT NULL "
            "OR operation IS NOT NULL OR operation_family IS NOT NULL OR payload_digest IS "
            "NOT NULL OR output_contract_json IS NOT NULL OR output_contract_digest IS NOT "
            "NULL OR envelope_digest IS NOT NULL OR reconciliation_state <> 'untouched' OR "
            "replacement_command_id IS NOT NULL LIMIT 1",
        ),
    ]


def _legacy_payload(command_id: str) -> dict[str, object]:
    """Return deliberately plausible ownership claims that migration must ignore."""

    return {
        "command_id": command_id,
        "run_id": "payload-run",
        "run_kind": "code_audit",
        "audit_id": "payload-audit",
        "plan_digest": "a" * 64,
        "execution_id": "payload-execution",
        "effect_binding_id": "payload-binding",
        "operation_family": "execution",
        "resource_kind": "execution",
        "resource_id": "payload-execution",
        "binding_digest": "b" * 64,
        "envelope_digest": "c" * 64,
    }


def _insert_node(connection: sqlite3.Connection, *, current_schema: bool = False) -> None:
    connection.execute(
        "INSERT INTO nodes "
        "(id, name, platform, architecture, runner_version, status, capabilities_json, "
        "labels_json, current_runner_instance_id, current_runner_epoch, last_seen_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "runner-node",
            "Legacy Runner",
            "linux",
            "x86_64",
            "2.9.0",
            "online",
            "[]",
            "{}",
            "runner-instance",
            7,
            NOW,
            NOW,
            NOW,
        ),
    )
    credential_columns = (
        "runner_instance_id, node_id, runner_epoch, token_hash, token_prefix, "
        + ("protocol_capabilities_json, " if current_schema else "")
        + "created_at, rotated_at, revoked_at"
    )
    credential_values: tuple[object, ...] = (
        "runner-instance",
        "runner-node",
        7,
        "d" * 64,
        "legacy",
        *(["[]"] if current_schema else []),
        NOW,
        NOW,
        None,
    )
    placeholders = ", ".join("?" for _ in credential_values)
    connection.execute(
        f"INSERT INTO runner_credentials ({credential_columns}) VALUES ({placeholders})",
        credential_values,
    )


def _insert_runner_command(
    connection: sqlite3.Connection,
    *,
    command_id: str,
    status: str,
    current_schema: bool = False,
) -> None:
    lease_id = "lease-legacy" if status == "leased" else None
    lease_expires_at = LEASE_EXPIRES_AT if status == "leased" else None
    terminal = status in {"completed", "failed"}
    result = {"exit_code": 0, "owner": "payload-must-not-be-trusted"} if terminal else {}
    error = "legacy terminal error" if status == "failed" else ""
    columns = (
        "id, node_id, kind, idempotency_key, target_runner_instance_id, "
        "target_runner_epoch, payload_json, status, attempts, lease_id, lease_expires_at, "
        "result_json, error, "
        + ("state_version, " if current_schema else "")
        + "created_at, updated_at, completed_at"
    )
    values: tuple[object, ...] = (
        command_id,
        "runner-node",
        "cancel",
        f"legacy:{command_id}",
        "runner-instance",
        7,
        json.dumps(_legacy_payload(command_id), sort_keys=True),
        status,
        1 if status != "pending" else 0,
        lease_id,
        lease_expires_at,
        json.dumps(result, sort_keys=True),
        error,
        *([0] if current_schema else []),
        NOW,
        NOW,
        NOW if terminal else None,
    )
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO runner_commands ({columns}) VALUES ({placeholders})",
        values,
    )


def _seed_legacy_commands(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_node(connection)
        _insert_runner_command(connection, command_id="pending-command", status="pending")
        _insert_runner_command(connection, command_id="leased-command", status="leased")
        _insert_runner_command(connection, command_id="terminal-command", status="completed")
        connection.commit()


def _assert_foreign_keys_clean(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _seed_execution_artifact_graph(database_path: Path) -> None:
    provenance = json.dumps(
        {
            "schema_version": "riftx.artifact-ingest-provenance/v1",
            "method": "legacy_migrated",
            "producer_node_id": None,
            "producer_execution_id": None,
        },
        sort_keys=True,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_engagement(connection, "runner-migration-engagement")
        _insert_run(
            connection,
            run_id=GRAPH_RUN_ID,
            engagement_id="runner-migration-engagement",
            kind="general",
            workflow_id="runner-migration-workflow",
        )
        connection.execute(
            "INSERT INTO executions "
            "(id, execution_key, run_id, node_id, executor_type, argv_json, cwd, "
            "env_diff_json, status, stdout_path, stderr_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                GRAPH_EXECUTION_ID,
                "execution:v1:runner-migration",
                GRAPH_RUN_ID,
                "runner-migration-node",
                "process",
                '["python", "audit.py"]',
                "/tmp/runner-migration",
                '{"RIFTX_TEST":"1"}',
                "running",
                "/tmp/runner-migration.stdout",
                "/tmp/runner-migration.stderr",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO artifacts "
            "(id, run_id, execution_id, audit_id, access_class, content_trust, "
            "name, path, storage_key, ingest_provenance_json, mime_type, sha256, "
            "size, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                GRAPH_ARTIFACT_ID,
                GRAPH_RUN_ID,
                GRAPH_EXECUTION_ID,
                None,
                "public_export",
                "untrusted_tool_output",
                "runner-result.json",
                "/tmp/runner-result.json",
                f"runs/{GRAPH_RUN_ID}/artifacts/{GRAPH_ARTIFACT_ID}/runner-result.json",
                provenance,
                "application/json",
                "e" * 64,
                41,
                "Runner migration FK regression fixture",
                NOW,
            ),
        )
        connection.commit()
        _assert_execution_artifact_graph(connection)


def _execution_artifact_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    execution = connection.execute(
        "SELECT id, execution_key, run_id, node_id, executor_type, argv_json, cwd, "
        "env_diff_json, status, stdout_path, stderr_path, created_at, updated_at "
        "FROM executions WHERE id = ?",
        (GRAPH_EXECUTION_ID,),
    ).fetchone()
    artifact = connection.execute(
        "SELECT id, run_id, execution_id, audit_id, access_class, content_trust, "
        "name, path, storage_key, ingest_provenance_json, mime_type, sha256, size, "
        "description, created_at FROM artifacts WHERE id = ?",
        (GRAPH_ARTIFACT_ID,),
    ).fetchone()
    assert execution is not None
    assert artifact is not None
    return execution, (*artifact[:9], json.loads(artifact[9]), *artifact[10:])


def _assert_execution_artifact_graph(connection: sqlite3.Connection) -> None:
    execution, artifact = _execution_artifact_snapshot(connection)
    assert execution[0] == GRAPH_EXECUTION_ID
    assert artifact[0:3] == (GRAPH_ARTIFACT_ID, GRAPH_RUN_ID, GRAPH_EXECUTION_ID)
    artifact_foreign_keys = connection.execute("PRAGMA foreign_key_list(artifacts)").fetchall()
    assert any(
        row[2] == "executions"
        and row[3] == "execution_id"
        and row[4] == "id"
        and row[6] == "RESTRICT"
        for row in artifact_foreign_keys
    )
    _assert_foreign_keys_clean(connection)


def test_runner_ownership_upgrade_quarantines_every_legacy_state_without_payload_inference(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-upgrade.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    _seed_legacy_commands(database_path)

    _run_alembic_with_sqlite_foreign_keys(database_path, RUNNER_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ownership_rows = connection.execute(
            "SELECT command_id, verification_state, schema_version, effect_binding_id, "
            "operation, operation_family, payload_digest, output_contract_json, "
            "output_contract_digest, envelope_digest, quarantine_reason, "
            "reconciliation_state, replacement_command_id, created_at "
            "FROM runner_command_ownerships ORDER BY command_id"
        ).fetchall()
        command_rows = connection.execute(
            "SELECT id, status, lease_id, result_json, state_version "
            "FROM runner_commands ORDER BY id"
        ).fetchall()
        credential = connection.execute(
            "SELECT protocol_capabilities_json FROM runner_credentials "
            "WHERE runner_instance_id = 'runner-instance'"
        ).fetchone()

        assert [row[0] for row in ownership_rows] == [
            "leased-command",
            "pending-command",
            "terminal-command",
        ]
        for row in ownership_rows:
            assert row[1] == "quarantined"
            # No value embedded in kind, payload, target, lease, result or paths is
            # authoritative enough to populate an immutable ownership envelope.
            assert row[2:10] == (None,) * 8
            assert row[10:13] == ("legacy_ownership_missing", "untouched", None)
            assert row[13] == NOW

        assert command_rows[0][1:3] == ("leased", "lease-legacy")
        assert command_rows[1][1:3] == ("pending", None)
        assert command_rows[2][1] == "completed"
        assert json.loads(command_rows[2][3]) == {
            "exit_code": 0,
            "owner": "payload-must-not-be-trusted",
        }
        assert {row[4] for row in command_rows} == {0}
        assert json.loads(credential[0]) == []
        _assert_foreign_keys_clean(connection)

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("DELETE FROM runner_commands WHERE id = 'pending-command'")
        connection.rollback()

    assert RUNNER_TABLES.issubset(sqlite_tables(database_path))


def test_runner_ownership_head_upgrade_and_downgrade_preserve_execution_artifact_fk(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-execution-artifact-round-trip.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    _seed_execution_artifact_graph(database_path)
    with sqlite3.connect(database_path) as connection:
        expected_graph = _execution_artifact_snapshot(connection)

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert _execution_artifact_snapshot(connection) == expected_graph
        _assert_execution_artifact_graph(connection)
        assert {
            "runner_command_id",
            "runner_effect_binding_id",
            "runner_binding_digest",
            "runner_envelope_digest",
        } <= {row[1] for row in connection.execute("PRAGMA table_info(executions)")}

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert _execution_artifact_snapshot(connection) == expected_graph
        _assert_execution_artifact_graph(connection)
        assert {
            "audit_id",
            "plan_digest",
            "runner_command_id",
            "runner_effect_binding_id",
            "runner_binding_digest",
            "runner_envelope_digest",
        }.isdisjoint({row[1] for row in connection.execute("PRAGMA table_info(executions)")})


def test_runner_ownership_upgrade_fault_rolls_back_execution_rebuild_and_can_retry(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-execution-artifact-upgrade-fault.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    _seed_execution_artifact_graph(database_path)
    with sqlite3.connect(database_path) as connection:
        expected_graph = _execution_artifact_snapshot(connection)

    fault_injected = False
    foreign_keys_restored = False

    def fail_after_partial_runner_ddl(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal fault_injected, foreign_keys_restored
        normalized = " ".join(statement.replace('"', "").split()).lower()
        if normalized == "pragma foreign_keys=on":
            foreign_keys_restored = True
        if normalized.startswith("create table runner_effect_bindings"):
            fault_injected = True
            raise RuntimeError("injected Runner ownership upgrade fault")

    event.listen(Engine, "before_cursor_execute", fail_after_partial_runner_ddl)
    try:
        with pytest.raises(RuntimeError, match="injected Runner ownership upgrade fault"):
            _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_partial_runner_ddl)
    assert fault_injected
    assert foreign_keys_restored

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert _execution_artifact_snapshot(connection) == expected_graph
        _assert_execution_artifact_graph(connection)
        assert {
            "audit_id",
            "plan_digest",
            "runner_command_id",
            "runner_effect_binding_id",
            "runner_binding_digest",
            "runner_envelope_digest",
        }.isdisjoint({row[1] for row in connection.execute("PRAGMA table_info(executions)")})
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '_alembic_tmp_%'"
            ).fetchall()
            == []
        )
    assert RUNNER_TABLES.isdisjoint(sqlite_tables(database_path))

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert _execution_artifact_snapshot(connection) == expected_graph
        _assert_execution_artifact_graph(connection)


def test_runner_ownership_downgrade_fault_rolls_back_execution_rebuild_and_can_retry(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-execution-artifact-downgrade-fault.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    _seed_execution_artifact_graph(database_path)
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        RUNNER_REVISION,
        downgrade=True,
    )
    with sqlite3.connect(database_path) as connection:
        expected_graph = _execution_artifact_snapshot(connection)

    fault_injected = False
    foreign_keys_restored = False

    def fail_after_partial_runner_ddl(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal fault_injected, foreign_keys_restored
        normalized = " ".join(statement.replace('"', "").split()).lower()
        if normalized == "pragma foreign_keys=on":
            foreign_keys_restored = True
        if normalized == "drop table runner_effect_bindings":
            fault_injected = True
            raise RuntimeError("injected Runner ownership downgrade fault")

    event.listen(Engine, "before_cursor_execute", fail_after_partial_runner_ddl)
    try:
        with pytest.raises(RuntimeError, match="injected Runner ownership downgrade fault"):
            _run_alembic_with_sqlite_foreign_keys(
                database_path,
                BASE_REVISION,
                downgrade=True,
            )
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_partial_runner_ddl)
    assert fault_injected
    assert foreign_keys_restored

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            RUNNER_REVISION,
        )
        assert _execution_artifact_snapshot(connection) == expected_graph
        _assert_execution_artifact_graph(connection)
        assert {
            "runner_command_id",
            "runner_effect_binding_id",
            "runner_binding_digest",
            "runner_envelope_digest",
        } <= {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '_alembic_tmp_%'"
            ).fetchall()
            == []
        )
    assert RUNNER_TABLES.issubset(sqlite_tables(database_path))

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert _execution_artifact_snapshot(connection) == expected_graph
        _assert_execution_artifact_graph(connection)


def test_runner_ownership_downgrade_preserves_untouched_legacy_quarantine(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-safe-downgrade.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    _seed_legacy_commands(database_path)
    _run_alembic_with_sqlite_foreign_keys(database_path, RUNNER_REVISION)

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )

    with sqlite3.connect(database_path) as connection:
        command_rows = connection.execute(
            "SELECT id, status, lease_id, result_json FROM runner_commands ORDER BY id"
        ).fetchall()
        command_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runner_commands)")
        }
        credential_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runner_credentials)")
        }
        execution_columns = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert [row[:3] for row in command_rows] == [
            ("leased-command", "leased", "lease-legacy"),
            ("pending-command", "pending", None),
            ("terminal-command", "completed", None),
        ]
        assert json.loads(command_rows[2][3])["owner"] == "payload-must-not-be-trusted"
        assert "state_version" not in command_columns
        assert "protocol_capabilities_json" not in credential_columns
        assert {
            "audit_id",
            "plan_digest",
            "runner_command_id",
            "runner_effect_binding_id",
            "runner_binding_digest",
            "runner_envelope_digest",
        }.isdisjoint(execution_columns)
        _assert_foreign_keys_clean(connection)

    assert RUNNER_TABLES.isdisjoint(sqlite_tables(database_path))


def test_runner_safe_downgrade_reupgrades_to_head_and_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-downgrade-reupgrade.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    _seed_legacy_commands(database_path)
    _run_alembic_with_sqlite_foreign_keys(database_path, RUNNER_REVISION)
    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    async def reopen() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            await database.create_schema()
        finally:
            await database.dispose()

    asyncio.run(reopen())
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert connection.execute(
            "SELECT command_id, verification_state, quarantine_reason "
            "FROM runner_command_ownerships ORDER BY command_id"
        ).fetchall() == [
            ("leased-command", "quarantined", "legacy_ownership_missing"),
            ("pending-command", "quarantined", "legacy_ownership_missing"),
            ("terminal-command", "quarantined", "legacy_ownership_missing"),
        ]
        _assert_foreign_keys_clean(connection)
    assert RUNNER_TABLES.issubset(sqlite_tables(database_path))


def test_runner_ownership_downgrade_refuses_reconciled_quarantine_without_data_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-unsafe-downgrade.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    _seed_legacy_commands(database_path)
    _run_alembic_with_sqlite_foreign_keys(database_path, RUNNER_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE runner_command_ownerships SET reconciliation_state = 'manual' "
            "WHERE command_id = 'leased-command'"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="verified or reconciled ownership facts exist"):
        _run_alembic_with_sqlite_foreign_keys(
            database_path,
            BASE_REVISION,
            downgrade=True,
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            RUNNER_REVISION,
        )
        assert connection.execute(
            "SELECT verification_state, quarantine_reason, reconciliation_state "
            "FROM runner_command_ownerships WHERE command_id = 'leased-command'"
        ).fetchone() == (
            "quarantined",
            "legacy_ownership_missing",
            "manual",
        )
        assert connection.execute(
            "SELECT status, lease_id FROM runner_commands WHERE id = 'leased-command'"
        ).fetchone() == ("leased", "lease-legacy")
        _assert_foreign_keys_clean(connection)


def test_runner_ownership_downgrade_refuses_protocol_capability_without_data_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-capability-downgrade.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    _seed_legacy_commands(database_path)
    _run_alembic_with_sqlite_foreign_keys(database_path, RUNNER_REVISION)
    capabilities = ["runner_command_ownership_v1"]
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE runner_credentials SET protocol_capabilities_json = ? "
            "WHERE runner_instance_id = 'runner-instance'",
            (json.dumps(capabilities),),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="protocol capability facts exist"):
        _run_alembic_with_sqlite_foreign_keys(
            database_path,
            BASE_REVISION,
            downgrade=True,
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            RUNNER_REVISION,
        )
        assert json.loads(
            connection.execute(
                "SELECT protocol_capabilities_json FROM runner_credentials "
                "WHERE runner_instance_id = 'runner-instance'"
            ).fetchone()[0]
        ) == capabilities
        assert connection.execute(
            "SELECT verification_state, reconciliation_state "
            "FROM runner_command_ownerships WHERE command_id = 'leased-command'"
        ).fetchone() == ("quarantined", "untouched")
        assert connection.execute(
            "SELECT status, lease_id FROM runner_commands WHERE id = 'leased-command'"
        ).fetchone() == ("leased", "lease-legacy")
        assert {
            row[1] for row in connection.execute("PRAGMA table_info(runner_credentials)")
        } >= {"protocol_capabilities_json"}
        _assert_foreign_keys_clean(connection)

    assert RUNNER_TABLES.issubset(sqlite_tables(database_path))


def test_runner_ownership_downgrade_refuses_recorded_legacy_stop_ack_without_data_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-legacy-ack-downgrade.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, BASE_REVISION)
    _seed_legacy_commands(database_path)
    _run_alembic_with_sqlite_foreign_keys(database_path, RUNNER_REVISION)
    ack = {
        "execution_id": "legacy-local-execution",
        "local_execution_id": "legacy-local-execution",
        "execution_key": "legacy-local-key",
        "owner": {"instance_id": "runner-instance", "epoch": 7},
        "status": ExecutionStatus.CANCELLED.value,
        "physical_stop_confirmed": True,
    }

    async def record_ack() -> None:
        database = Database(f"sqlite+aiosqlite:///{database_path}")
        try:
            repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
            recorded = await repository.record_legacy_stop_ack(
                "leased-command",
                principal=RunnerPrincipal(instance_id="runner-instance", epoch=7),
                lease_id="lease-legacy",
                expected_state_version=0,
                ack=ack,
                received_at=utc_now(),
            )
            assert recorded.state_version == 1
        finally:
            await database.dispose()

    asyncio.run(record_ack())

    with pytest.raises(RuntimeError, match="post-migration command state exists"):
        _run_alembic_with_sqlite_foreign_keys(
            database_path,
            BASE_REVISION,
            downgrade=True,
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            RUNNER_REVISION,
        )
        row = connection.execute(
            "SELECT state_version, status, lease_id, result_json FROM runner_commands "
            "WHERE id = 'leased-command'"
        ).fetchone()
        assert row[:3] == (1, "leased", "lease-legacy")
        result = json.loads(row[3])
        evidence = result["_riftx_legacy_stop_ack_evidence"]
        assert evidence["ack"] == ack
        assert evidence["recorded_from_state_version"] == 0
        assert connection.execute(
            "SELECT verification_state, reconciliation_state "
            "FROM runner_command_ownerships WHERE command_id = 'leased-command'"
        ).fetchone() == ("quarantined", "untouched")
        _assert_foreign_keys_clean(connection)

    assert RUNNER_TABLES.issubset(sqlite_tables(database_path))


def test_runner_migration_chain_upgrades_earliest_revision_to_head_and_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runner-ownership-full-chain.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, EARLIEST_REVISION)
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    async def reopen_twice() -> None:
        for _ in range(2):
            database = Database(f"sqlite+aiosqlite:///{database_path}")
            try:
                await database.create_schema()
            finally:
                await database.dispose()

    asyncio.run(reopen_twice())

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert {row[1] for row in connection.execute("PRAGMA table_info(executions)")} >= {
            "runner_command_id",
            "runner_effect_binding_id",
            "runner_binding_digest",
            "runner_envelope_digest",
        }
        _assert_foreign_keys_clean(connection)

    assert RUNNER_TABLES.issubset(sqlite_tables(database_path))


def test_runner_metadata_bootstrap_refuses_legacy_schema_and_quarantines_current_rows(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "runner-ownership-legacy-bootstrap.db"
    _run_alembic_with_sqlite_foreign_keys(legacy_path, BASE_REVISION)
    _seed_legacy_commands(legacy_path)

    async def reject_managed_legacy_bootstrap() -> None:
        database = Database(f"sqlite+aiosqlite:///{legacy_path}")
        try:
            with pytest.raises(RuntimeError, match="apply all Alembic migrations"):
                await database.create_schema()
        finally:
            await database.dispose()

    asyncio.run(reject_managed_legacy_bootstrap())
    assert RUNNER_TABLES.isdisjoint(sqlite_tables(legacy_path))
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )

    with sqlite3.connect(legacy_path) as connection:
        connection.execute("DROP TABLE alembic_version")
        connection.commit()

    async def reject_legacy_bootstrap() -> None:
        database = Database(f"sqlite+aiosqlite:///{legacy_path}")
        try:
            with pytest.raises(
                RuntimeError,
                match="Runner schema predates immutable command ownership",
            ):
                await database.create_schema()
        finally:
            await database.dispose()

    asyncio.run(reject_legacy_bootstrap())
    assert RUNNER_TABLES.isdisjoint(sqlite_tables(legacy_path))

    current_path = tmp_path / "runner-ownership-current-bootstrap.db"

    async def bootstrap_current_schema() -> None:
        database = Database(f"sqlite+aiosqlite:///{current_path}")
        try:
            await database.create_schema()
        finally:
            await database.dispose()

        with sqlite3.connect(current_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            _insert_node(connection, current_schema=True)
            _insert_runner_command(
                connection,
                command_id="metadata-unbound-command",
                status="pending",
                current_schema=True,
            )
            connection.commit()

        reopened = Database(f"sqlite+aiosqlite:///{current_path}")
        try:
            await reopened.create_schema()
        finally:
            await reopened.dispose()

    asyncio.run(bootstrap_current_schema())

    with sqlite3.connect(current_path) as connection:
        ownership = connection.execute(
            "SELECT verification_state, schema_version, effect_binding_id, operation, "
            "operation_family, payload_digest, output_contract_json, "
            "output_contract_digest, envelope_digest, quarantine_reason, "
            "reconciliation_state FROM runner_command_ownerships "
            "WHERE command_id = 'metadata-unbound-command'"
        ).fetchone()
        assert ownership[:9] == ("quarantined",) + (None,) * 8
        assert ownership[9:] == (
            "metadata_bootstrap_ownership_missing",
            "untouched",
        )
        _assert_foreign_keys_clean(connection)
