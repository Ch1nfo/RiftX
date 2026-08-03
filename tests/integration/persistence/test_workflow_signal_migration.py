from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

from riftx.persistence.database import Database

BASE_REVISION = "8d7c2e4f1a90"
SIGNAL_REVISION = "4f9a6c1d2e30"
NOW = "2026-08-03 12:00:00+00:00"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _insert_general_owner(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO engagements "
        "(id, name, description, authorization_reference, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("engagement-1", "Signals", "", None, NOW, NOW),
    )
    connection.execute(
        "INSERT INTO runs "
        "(id, engagement_id, kind, node_id, objective, success_criteria_json, "
        "entry_points_json, scope_json, status, approval_mode, model_profile, "
        "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run-1",
            "engagement-1",
            "general",
            "local",
            "Deliver durable signal",
            "[]",
            "[]",
            "{}",
            "created",
            "balanced",
            None,
            "/tmp/riftx/run-1",
            "riftx-run-run-1",
            NOW,
            None,
            None,
        ),
    )
    connection.commit()


def _valid_row(*, intent_id: str = "intent-1") -> tuple[object, ...]:
    return (
        intent_id,
        "riftx.workflow-signal-intent/v1",
        "general_run",
        "general_run:run-1",
        "run-1",
        "general",
        None,
        "riftx.general-run-workflow/v1",
        "riftx-run-run-1",
        "execution_completed",
        "execution_terminal",
        "execution-1",
        2,
        _digest("identity-1"),
        '{"execution_id":"execution-1"}',
        _digest("payload-1"),
        "pending",
        None,
        None,
        0,
        NOW,
        None,
        None,
        NOW,
        NOW,
        None,
        1,
    )


_INSERT = (
    "INSERT INTO workflow_signal_intents "
    "(id, schema_version, owner_kind, owner_identity, run_id, run_kind, audit_id, "
    "workflow_protocol_version, workflow_id, signal_kind, source_event_kind, "
    "source_event_id, source_state_version, identity_digest, payload_json, "
    "payload_digest, delivery_state, lease_owner, lease_expires_at, attempt, "
    "next_attempt_at, delivery_receipt_digest, last_error_code, created_at, "
    "updated_at, delivered_at, state_version) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    "?, ?, ?, ?, ?)"
)


def test_workflow_signal_migration_enforces_owner_source_and_downgrade_safety(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow-signal-migration.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_general_owner(connection)

    run_alembic(database_path, SIGNAL_REVISION)
    assert "workflow_signal_intents" in sqlite_tables(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(_INSERT, _valid_row())
        connection.commit()

        invalid_owner = list(_valid_row(intent_id="invalid-owner"))
        invalid_owner[2] = "code_audit"
        invalid_owner[3] = "code_audit:audit-1"
        invalid_owner[5] = "code_audit"
        invalid_owner[7] = "riftx.general-run-workflow/v1"
        with pytest.raises(sqlite3.IntegrityError, match="owner_binding"):
            connection.execute(_INSERT, invalid_owner)
        connection.rollback()

        stop_ack = list(_valid_row(intent_id="stop-ack"))
        stop_ack[10] = "runner_stop_ack"
        stop_ack[13] = _digest("stop-ack-identity")
        with pytest.raises(sqlite3.IntegrityError, match="source_kind"):
            connection.execute(_INSERT, stop_ack)
        connection.rollback()

        duplicate_source = list(_valid_row(intent_id="duplicate-source"))
        duplicate_source[13] = _digest("different-identity-digest")
        duplicate_source[14] = '{"execution_id":"different"}'
        duplicate_source[15] = _digest("different-payload")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(_INSERT, duplicate_source)
        connection.rollback()

        superseded_without_reason = list(_valid_row(intent_id="superseded-no-reason"))
        superseded_without_reason[11] = "execution-superseded"
        superseded_without_reason[13] = _digest("superseded-identity")
        superseded_without_reason[16] = "superseded"
        superseded_without_reason[19] = 1
        superseded_without_reason[20] = None
        with pytest.raises(sqlite3.IntegrityError, match="error_state"):
            connection.execute(_INSERT, superseded_without_reason)
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("DELETE FROM runs WHERE id = 'run-1'")
        connection.rollback()

    with pytest.raises(RuntimeError, match="durable Workflow signal intents exist"):
        downgrade_alembic(database_path, BASE_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM workflow_signal_intents")
        connection.commit()
    downgrade_alembic(database_path, BASE_REVISION)
    assert "workflow_signal_intents" not in sqlite_tables(database_path)


def test_workflow_signal_migration_metadata_matches_postgresql_contract() -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        "8d7c2e4f1a90:4f9a6c1d2e30",
    )
    assert "CREATE TABLE workflow_signal_intents" in sql
    assert "ck_workflow_signal_intents_owner_binding" in sql
    assert "uq_workflow_signal_intents_source_identity" in sql


async def test_metadata_bootstrap_registers_outbox_and_refuses_partial_upgrade(
    tmp_path: Path,
) -> None:
    fresh_path = tmp_path / "fresh-metadata.db"
    fresh = Database(f"sqlite+aiosqlite:///{fresh_path}")
    await fresh.create_schema()
    await fresh.dispose()
    assert "workflow_signal_intents" in sqlite_tables(fresh_path)

    managed_path = tmp_path / "managed-behind.db"
    await asyncio.to_thread(run_alembic, managed_path, BASE_REVISION)
    managed = Database(f"sqlite+aiosqlite:///{managed_path}")
    with pytest.raises(RuntimeError, match="apply all Alembic migrations"):
        await managed.create_schema()
    await managed.dispose()

    await asyncio.to_thread(run_alembic, managed_path, SIGNAL_REVISION)
    current = Database(f"sqlite+aiosqlite:///{managed_path}")
    await current.create_schema()
    await current.dispose()
