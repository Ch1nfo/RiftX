import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    offline_downgrade_alembic,
    run_alembic,
    sqlite_tables,
)

PREVIOUS_REVISION = "6e4a2c9f1b30"
CAPABILITY_REVISION = "7f2c8a1d4e90"
CAPABILITY_TABLES = {
    "capabilities",
    "capability_versions",
    "capability_dependencies",
    "capability_permissions",
    "capability_evidence_contracts",
    "capability_candidates",
    "capability_promotion_runs",
    "capability_evaluation_results",
    "capability_packs",
    "capability_pack_members",
    "capability_pack_installs",
    "capability_pack_locks",
}


def test_capability_migration_separates_candidates_and_guards_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "capabilities.db"
    run_alembic(database_path, PREVIOUS_REVISION)
    assert not CAPABILITY_TABLES & sqlite_tables(database_path)

    run_alembic(database_path, CAPABILITY_REVISION)
    assert CAPABILITY_TABLES <= sqlite_tables(database_path)
    now = "2026-08-05 11:00:00+00:00"
    statement = (
        "INSERT INTO capability_candidates "
        "(id, capability_id, proposed_version, kind, status, manifest_json, "
        "candidate_digest, provenance_json, proposed_by, source_run_id, "
        "promoted_version_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    values = (
        "candidate-migration",
        "web.request-analysis",
        "1.0.0",
        "technique",
        "draft",
        "{}",
        "a" * 64,
        "{}",
        "operator-1",
        None,
        None,
        now,
        now,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(statement, values)
        connection.commit()
        assert connection.execute(
            "SELECT count(*) FROM capability_versions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM capability_candidates"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="ck_capability_candidates_status"):
            connection.execute(
                statement,
                ("candidate-invalid", *values[1:4], "active", *values[5:]),
            )

    with pytest.raises(RuntimeError, match="durable Capability catalog facts exist"):
        downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert CAPABILITY_TABLES <= sqlite_tables(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM capability_candidates")
        connection.commit()
    downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert not CAPABILITY_TABLES & sqlite_tables(database_path)


def test_capability_offline_downgrade_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="offline downgrade"):
        offline_downgrade_alembic(f"{CAPABILITY_REVISION}:{PREVIOUS_REVISION}")
