import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    offline_downgrade_alembic,
    run_alembic,
    sqlite_tables,
)

PREVIOUS_REVISION = "6c8e4a2f1b70"
TASK_GRAPH_REVISION = "4d7f1a8c2e90"
TASK_GRAPH_TABLES = {
    "task_graphs",
    "tasks",
    "task_dependencies",
    "task_attempts",
    "task_budgets",
    "task_evidence_requirements",
}


def insert_run(connection: sqlite3.Connection, run_id: str) -> None:
    now = "2026-08-06 00:00:00+00:00"
    connection.execute(
        "INSERT OR IGNORE INTO engagements "
        "(id, name, description, authorization_reference, created_at, updated_at) "
        "VALUES ('engagement-task-graph', 'Task Graph', '', NULL, ?, ?)",
        (now, now),
    )
    connection.execute(
        "INSERT INTO runs "
        "(id, engagement_id, kind, node_id, objective, success_criteria_json, "
        "entry_points_json, scope_json, status, approval_mode, model_profile, "
        "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
        "VALUES (?, 'engagement-task-graph', 'general', 'local', 'Task Graph', '[]', "
        "'[]', '{}', 'created', 'balanced', NULL, ?, NULL, ?, NULL, NULL)",
        (run_id, f"/tmp/{run_id}", now),
    )


def insert_task(
    connection: sqlite3.Connection,
    run_id: str,
    task_id: str,
    sequence: int,
) -> None:
    now = "2026-08-06 00:00:00+00:00"
    connection.execute(
        "INSERT INTO tasks "
        "(id, run_id, parent_task_id, sequence, title, description, status, "
        "input_scope_json, expected_output_schema_json, required_capability_ids_json, "
        "workspace_owner, session_owner_id, stop_condition, completion_summary, "
        "blocked_reason, reopen_history_json, version, created_at, updated_at, completed_at) "
        "VALUES (?, ?, NULL, ?, ?, '', 'pending', '{}', '{}', '[]', NULL, NULL, NULL, "
        "NULL, NULL, '[]', 1, ?, ?, NULL)",
        (task_id, run_id, sequence, task_id, now, now),
    )


def test_task_graph_migration_enforces_run_ownership_and_guards_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task-graph-migration.db"
    run_alembic(database_path, PREVIOUS_REVISION)
    assert not TASK_GRAPH_TABLES & sqlite_tables(database_path)

    run_alembic(database_path, TASK_GRAPH_REVISION)
    assert TASK_GRAPH_TABLES <= sqlite_tables(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        insert_run(connection, "run-1")
        insert_run(connection, "run-2")
        now = "2026-08-06 00:00:00+00:00"
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            insert_task(connection, "run-1", "orphan-task", 1)
        connection.execute(
            "INSERT INTO task_graphs (run_id, version, created_at, updated_at) "
            "VALUES ('run-1', 1, ?, ?), ('run-2', 1, ?, ?)",
            (now, now, now, now),
        )
        insert_task(connection, "run-1", "task-1", 1)
        insert_task(connection, "run-2", "task-2", 1)
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO task_dependencies "
                "(run_id, task_id, depends_on_task_id, created_at) "
                "VALUES ('run-1', 'task-1', 'task-2', ?)",
                (now,),
            )

    with pytest.raises(RuntimeError, match="durable Task Graph facts exist"):
        downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert TASK_GRAPH_TABLES <= sqlite_tables(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for table_name in (
            "task_evidence_requirements",
            "task_budgets",
            "task_attempts",
            "task_dependencies",
            "tasks",
            "task_graphs",
        ):
            connection.execute(f'DELETE FROM "{table_name}"')
        connection.commit()
    downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert not TASK_GRAPH_TABLES & sqlite_tables(database_path)


def test_task_graph_offline_downgrade_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="offline downgrade"):
        offline_downgrade_alembic(f"{TASK_GRAPH_REVISION}:{PREVIOUS_REVISION}")
