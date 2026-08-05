import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    run_alembic,
    sqlite_tables,
)

PREVIOUS_REVISION = "d0b4e6f8a102"
LOCAL_JOB_REVISION = "6e4a2c9f1b30"


def test_local_audit_job_migration_upgrades_enforces_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "local-audit-job.db"
    run_alembic(database_path, PREVIOUS_REVISION)
    assert "local_audit_jobs" not in sqlite_tables(database_path)

    run_alembic(database_path, LOCAL_JOB_REVISION)
    assert "local_audit_jobs" in sqlite_tables(database_path)
    now = "2026-08-05 00:00:00+00:00"
    values = (
        "audit-migration",
        "/tmp/source",
        "[]",
        "[]",
        "draft",
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        0,
        0,
        0,
        "[]",
        None,
        None,
        1,
        now,
        now,
        None,
        None,
        None,
    )
    statement = (
        "INSERT INTO local_audit_jobs "
        "(id, source_path, include_paths_json, exclude_paths_json, status, "
        "cancel_requested, failure_code, source_identity_digest, snapshot_digest, "
        "manifest_digest, inventory_digest, detector_run_digest, report_digest, "
        "total_files, scanned_files, finding_count, findings_json, json_report, "
        "markdown_report, state_version, created_at, updated_at, queued_at, started_at, "
        "finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(statement, values)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="ck_local_audit_jobs_status"):
            connection.execute(statement, ("audit-invalid", *values[1:4], "running", *values[5:]))

    with pytest.raises(RuntimeError, match="durable local Audit Job facts exist"):
        downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert "local_audit_jobs" in sqlite_tables(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM local_audit_jobs")
        connection.commit()

    downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert "local_audit_jobs" not in sqlite_tables(database_path)
