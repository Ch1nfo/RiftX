from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_audit_migration import (
    NOW,
    _insert_engagement,
    _insert_project,
    _insert_snapshot,
)
from tests.integration.persistence.test_migrations import (
    _run_alembic_with_sqlite_foreign_keys,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

PREVIOUS_REVISION = "9c2e4f6a8b10"
DIRECTORY_REVISION = "d0b4e6f8a102"


def _insert_directory_facts(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO audit_projects "
        "(id, engagement_id, display_name, vcs_kind, repository_identity_digest, "
        "default_branch, state_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "project-directory",
            "engagement-directory",
            "Directory project",
            "directory",
            "d" * 64,
            None,
            1,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        "INSERT INTO source_snapshots "
        "(id, project_id, source_kind, parent_snapshot_id, base_tree_digest, "
        "patch_digest, commit_sha, base_commit_sha, working_tree_digest, tree_digest, "
        "capture_policy_digest, materializer_schema_version, snapshot_digest, "
        "snapshot_store_version, content_storage_key, manifest_storage_key, "
        "manifest_digest, file_count, total_bytes, created_at, sealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "snapshot-directory",
            "project-directory",
            "directory",
            None,
            None,
            None,
            None,
            None,
            None,
            "1" * 64,
            "2" * 64,
            "riftx.local-directory-materializer/v1",
            "3" * 64,
            "riftx.snapshot-store/v1",
            "snapshot-cas:content:" + "4" * 64,
            "snapshot-cas:content:" + "5" * 64,
            "6" * 64,
            0,
            0,
            NOW,
            NOW,
        ),
    )
    connection.commit()


def test_local_directory_snapshot_migration_preserves_git_rows_and_guards_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "local-directory-migration.db"
    _run_alembic_with_sqlite_foreign_keys(database_path, PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_engagement(connection, "engagement-git")
        _insert_project(
            connection,
            project_id="project-git",
            engagement_id="engagement-git",
            digest_character="a",
        )
        _insert_snapshot(
            connection,
            snapshot_id="snapshot-git",
            project_id="project-git",
            digest_character="b",
        )
        connection.commit()

    _run_alembic_with_sqlite_foreign_keys(database_path, DIRECTORY_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_engagement(connection, "engagement-directory")
        _insert_directory_facts(connection)
        commit_column = next(
            row
            for row in connection.execute("PRAGMA table_info(source_snapshots)")
            if row[1] == "commit_sha"
        )
        git_row = connection.execute(
            "SELECT source_kind, commit_sha FROM source_snapshots "
            "WHERE id = 'snapshot-git'"
        ).fetchone()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert commit_column[3] == 0
    assert git_row == ("revision", "a" * 40)
    assert violations == []

    with pytest.raises(RuntimeError, match="local-directory SourceSnapshot"):
        _run_alembic_with_sqlite_foreign_keys(
            database_path,
            PREVIOUS_REVISION,
            downgrade=True,
        )

    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM source_snapshots WHERE id = 'snapshot-directory'")
        connection.execute("DELETE FROM audit_projects WHERE id = 'project-directory'")
        connection.commit()
    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        PREVIOUS_REVISION,
        downgrade=True,
    )
    with sqlite3.connect(database_path) as connection:
        commit_column = next(
            row
            for row in connection.execute("PRAGMA table_info(source_snapshots)")
            if row[1] == "commit_sha"
        )
        assert connection.execute(
            "SELECT source_kind, commit_sha FROM source_snapshots "
            "WHERE id = 'snapshot-git'"
        ).fetchone() == ("revision", "a" * 40)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert commit_column[3] == 1


def test_local_directory_snapshot_migration_compiles_for_postgresql_offline() -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{PREVIOUS_REVISION}:{DIRECTORY_REVISION}",
    )
    assert "ALTER COLUMN commit_sha DROP NOT NULL" in sql
    assert "directory" in sql
