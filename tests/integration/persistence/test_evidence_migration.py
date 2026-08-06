import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    offline_downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_task_graph_migration import insert_run

PREVIOUS_REVISION = "4d7f1a8c2e90"
EVIDENCE_REVISION = "5e8a2c4d7f10"
EVIDENCE_TABLE = "evidence_ledger"


def insert_evidence(connection: sqlite3.Connection, evidence_id: str) -> None:
    connection.execute(
        "INSERT INTO evidence_ledger "
        "(id, schema_version, run_id, session_id, task_id, kind, source_uri, "
        "digest, ledger_digest, creator_type, created_by, trust_class, scope_json, "
        "redaction_status, redaction_policy_ref, replay_json, locator_json, "
        "artifact_id, created_at) VALUES "
        "(?, 'riftx.evidence/v1', 'run-1', NULL, NULL, 'execution_output', ?, "
        "?, ?, 'tool', 'run_shell', 'untrusted_tool_output', ?, 'metadata_only', "
        "NULL, ?, ?, NULL, '2026-08-06 00:00:00+00:00')",
        (
            evidence_id,
            f"execution://{evidence_id}/stdout",
            "a" * 64,
            "b" * 64,
            '{"engagement_id":"engagement-task-graph","run_id":"run-1"}',
            '{"strategy":"source_lookup","replayable":true}',
            f'{{"locator_type":"source","uri":"execution://{evidence_id}/stdout"}}',
        ),
    )


def test_evidence_migration_enforces_integrity_and_guards_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "evidence-migration.db"
    run_alembic(database_path, PREVIOUS_REVISION)
    assert EVIDENCE_TABLE not in sqlite_tables(database_path)

    run_alembic(database_path, EVIDENCE_REVISION)
    assert EVIDENCE_TABLE in sqlite_tables(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        insert_run(connection, "run-1")
        insert_evidence(connection, "evidence-1")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "UPDATE evidence_ledger SET digest = ? WHERE id = 'evidence-1'",
                ("A" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "UPDATE evidence_ledger SET run_id = 'missing' WHERE id = 'evidence-1'"
            )
        connection.commit()

    with pytest.raises(RuntimeError, match="durable Evidence facts exist"):
        downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert EVIDENCE_TABLE in sqlite_tables(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM evidence_ledger")
        connection.commit()
    downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert EVIDENCE_TABLE not in sqlite_tables(database_path)


def test_evidence_offline_downgrade_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="offline downgrade"):
        offline_downgrade_alembic(f"{EVIDENCE_REVISION}:{PREVIOUS_REVISION}")
