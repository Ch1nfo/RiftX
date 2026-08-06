import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_evidence_migration import insert_evidence
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    offline_downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_task_graph_migration import insert_run

PREVIOUS_REVISION = "5e8a2c4d7f10"
REASONING_REVISION = "8b1d3f5a7c20"
REASONING_TABLES = {
    "reasoning_graphs",
    "reasoning_nodes",
    "reasoning_edges",
    "reasoning_node_evidence",
    "reasoning_edge_evidence",
}


def test_reasoning_migration_enforces_evidence_ownership_and_guards_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reasoning-migration.db"
    run_alembic(database_path, PREVIOUS_REVISION)
    assert not REASONING_TABLES & sqlite_tables(database_path)

    run_alembic(database_path, REASONING_REVISION)
    assert REASONING_TABLES <= sqlite_tables(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        insert_run(connection, "run-1")
        insert_run(connection, "run-2")
        insert_evidence(connection, "evidence-1")
        now = "2026-08-06 00:00:00+00:00"
        connection.execute(
            "INSERT INTO reasoning_graphs "
            "(run_id, schema_version, version, created_at, updated_at) "
            "VALUES ('run-1', 'riftx.reasoning-graph/v1', 1, ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO reasoning_nodes "
            "(id, run_id, schema_version, session_id, task_id, kind, status, claim, "
            "structured_data_json, reproduction_contract_json, creator_type, created_by, "
            "version, created_at, updated_at) VALUES "
            "('observation-1', 'run-1', 'riftx.reasoning-graph/v1', NULL, NULL, "
            "'observation', 'recorded', 'Observed service', '{}', NULL, 'parser', "
            "'service-parser', 1, ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO reasoning_node_evidence "
            "(run_id, node_id, evidence_id, ordinal) "
            "VALUES ('run-1', 'observation-1', 'evidence-1', 0)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "UPDATE reasoning_node_evidence SET run_id = 'run-2' "
                "WHERE node_id = 'observation-1'"
            )
        connection.commit()

    with pytest.raises(RuntimeError, match="durable Reasoning Graph facts exist"):
        downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert REASONING_TABLES <= sqlite_tables(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM reasoning_node_evidence")
        connection.execute("DELETE FROM reasoning_nodes")
        connection.execute("DELETE FROM reasoning_graphs")
        connection.commit()
    downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert not REASONING_TABLES & sqlite_tables(database_path)


def test_reasoning_offline_downgrade_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="offline downgrade"):
        offline_downgrade_alembic(f"{REASONING_REVISION}:{PREVIOUS_REVISION}")
