import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    offline_downgrade_alembic,
    run_alembic,
)
from tests.integration.persistence.test_task_graph_migration import insert_run, insert_task

PREVIOUS_REVISION = "8b1d3f5a7c20"
CLOSURE_MAPPING_REVISION = "3c6e8a1f2b40"


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row[1]
        for row in connection.execute("PRAGMA table_info(task_evidence_requirements)").fetchall()
    }


def test_closure_mapping_migration_preserves_explicit_criterion_links(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "closure-mapping.db"
    run_alembic(database_path, PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert "success_criterion_index" not in _columns(connection)

    run_alembic(database_path, CLOSURE_MAPPING_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert "success_criterion_index" in _columns(connection)
        insert_run(connection, "run-1")
        now = "2026-08-06 00:00:00+00:00"
        connection.execute(
            "UPDATE runs SET success_criteria_json = ? WHERE id = 'run-1'",
            ('[{"description":"Preserve evidence","required":true}]',),
        )
        connection.execute(
            "INSERT INTO task_graphs (run_id, version, created_at, updated_at) "
            "VALUES ('run-1', 1, ?, ?)",
            (now, now),
        )
        insert_task(connection, "run-1", "task-1", 1)
        connection.execute(
            "INSERT INTO task_evidence_requirements "
            "(id, run_id, task_id, evidence_type, description, minimum_count, "
            "success_criterion_index, evidence_refs_json, created_at, updated_at) "
            "VALUES ('requirement-1', 'run-1', 'task-1', 'artifact', "
            "'Preserve evidence', 1, 0, '[]', ?, ?)",
            (now, now),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "UPDATE task_evidence_requirements SET success_criterion_index = -1 "
                "WHERE id = 'requirement-1'"
            )
        connection.commit()

    with pytest.raises(RuntimeError, match="Success Criterion mappings exist"):
        downgrade_alembic(database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE task_evidence_requirements SET success_criterion_index = NULL")
        connection.commit()
    downgrade_alembic(database_path, PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert "success_criterion_index" not in _columns(connection)


def test_closure_mapping_offline_downgrade_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="offline downgrade"):
        offline_downgrade_alembic(f"{CLOSURE_MAPPING_REVISION}:{PREVIOUS_REVISION}")
