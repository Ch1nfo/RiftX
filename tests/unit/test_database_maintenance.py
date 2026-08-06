"""SQLite migration diagnosis and repair contract tests."""

from __future__ import annotations

import sqlite3
import stat
import tomllib
from pathlib import Path

import pytest
from sqlalchemy import Connection

import riftx.database_maintenance as maintenance
from riftx.database_maintenance import (
    DatabaseRepairError,
    SQLiteBackupError,
    SQLiteMigrationStatus,
    backup_sqlite_database,
    inspect_sqlite_backup_readiness,
    inspect_sqlite_migration,
    repair_sqlite_database,
    restore_sqlite_database_backup,
)


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def test_inspect_sqlite_migration_distinguishes_missing_empty_and_unmanaged(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "riftx.db"

    missing = inspect_sqlite_migration(_url(database_path), cwd=tmp_path)
    database_path.touch()
    empty = inspect_sqlite_migration(_url(database_path), cwd=tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
    unmanaged = inspect_sqlite_migration(_url(database_path), cwd=tmp_path)

    assert missing is not None and missing.status is SQLiteMigrationStatus.MISSING
    assert missing.fixable
    assert empty is not None and empty.status is SQLiteMigrationStatus.EMPTY
    assert empty.fixable
    assert unmanaged is not None and unmanaged.status is SQLiteMigrationStatus.UNMANAGED
    assert not unmanaged.fixable


def test_repair_sqlite_database_removes_new_database_when_migration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "riftx.db"

    def fail_upgrade(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(maintenance, "_run_upgrade", fail_upgrade)

    with pytest.raises(DatabaseRepairError, match="rolled back") as captured:
        repair_sqlite_database(_url(database_path), cwd=tmp_path)

    assert captured.value.rollback_complete
    assert captured.value.backup_path is None
    assert not database_path.exists()
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()


def test_repair_sqlite_database_rejects_unmanaged_existing_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")

    with pytest.raises(DatabaseRepairError, match="unmanaged") as captured:
        repair_sqlite_database(_url(database_path), cwd=tmp_path)

    assert captured.value.backup_path is None
    assert not (tmp_path / "backups").exists()


def test_repair_sqlite_database_restores_when_head_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "riftx.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('old-revision')")
    monkeypatch.setattr(maintenance, "_run_upgrade", lambda *_args, **_kwargs: None)

    with pytest.raises(DatabaseRepairError, match="prior state was restored") as captured:
        repair_sqlite_database(_url(database_path), cwd=tmp_path)

    assert captured.value.rollback_complete
    with sqlite3.connect(database_path) as restored:
        assert restored.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "old-revision",
        )


def test_repair_sqlite_database_backup_is_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "riftx.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('old-revision')")

    def mark_ready(connection: Connection, *_args: object, **_kwargs: object) -> None:
        with sqlite3.connect(database_path, timeout=0.05) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        connection.exec_driver_sql(
            "UPDATE alembic_version SET version_num = ?",
            (maintenance.ALEMBIC_HEAD_REVISION,),
        )
        connection.commit()

    monkeypatch.setattr(maintenance, "_run_upgrade", mark_ready)
    result = repair_sqlite_database(_url(database_path), cwd=tmp_path)

    assert result.backup_path is not None
    assert result.backup_path.is_file()
    assert stat.S_IMODE(result.backup_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.backup_path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "old-revision",
        )


def test_ready_sqlite_backup_can_restore_later_mutation(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    repair_sqlite_database(_url(database_path), cwd=tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE pack_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO pack_marker VALUES ('before')")

    backup = backup_sqlite_database(_url(database_path), cwd=tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE pack_marker SET value = 'after'")
    restore_sqlite_database_backup(backup)

    assert stat.S_IMODE(backup.backup_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.backup_path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(database_path) as restored:
        assert restored.execute("SELECT value FROM pack_marker").fetchone() == ("before",)


def test_sqlite_backup_readiness_is_read_only_and_rejects_unsafe_directory(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "riftx.db"
    repair_sqlite_database(_url(database_path), cwd=tmp_path)

    readiness = inspect_sqlite_backup_readiness(_url(database_path), cwd=tmp_path)

    assert readiness.path == database_path
    assert readiness.backup_directory == tmp_path / "backups"
    assert not readiness.backup_directory.exists()

    readiness.backup_directory.mkdir(mode=0o755)
    readiness.backup_directory.chmod(0o755)
    with pytest.raises(SQLiteBackupError, match="owner-only"):
        inspect_sqlite_backup_readiness(_url(database_path), cwd=tmp_path)
    with pytest.raises(SQLiteBackupError, match="owner-only"):
        backup_sqlite_database(_url(database_path), cwd=tmp_path)


def test_ready_sqlite_restore_rejects_database_identity_drift(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    repair_sqlite_database(_url(database_path), cwd=tmp_path)
    backup = backup_sqlite_database(_url(database_path), cwd=tmp_path)
    replacement = tmp_path / "replacement.db"
    with sqlite3.connect(replacement) as connection:
        connection.execute("CREATE TABLE replacement (id INTEGER PRIMARY KEY)")
    replacement.replace(database_path)

    with pytest.raises(SQLiteBackupError, match="identity changed"):
        restore_sqlite_database_backup(backup)


def test_ready_sqlite_restore_rejects_backup_identity_drift(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    repair_sqlite_database(_url(database_path), cwd=tmp_path)
    backup = backup_sqlite_database(_url(database_path), cwd=tmp_path)
    replacement = tmp_path / "replacement.bak"
    replacement.write_bytes(backup.backup_path.read_bytes())
    replacement.replace(backup.backup_path)

    with pytest.raises(SQLiteBackupError, match="identity changed"):
        restore_sqlite_database_backup(backup)


def test_wheel_configuration_includes_all_alembic_assets() -> None:
    root = Path(__file__).parents[2]
    with (root / "pyproject.toml").open("rb") as handle:
        configuration = tomllib.load(handle)
    data_files = configuration["tool"]["setuptools"]["data-files"]

    assert data_files["share/riftx"] == ["alembic.ini"]
    assert set(data_files["share/riftx/migrations"]) == {
        "migrations/env.py",
        "migrations/script.py.mako",
    }
    assert data_files["share/riftx/migrations/versions"] == [
        "migrations/versions/*.py"
    ]
    assert len(tuple((root / "migrations" / "versions").glob("*.py"))) == 49
