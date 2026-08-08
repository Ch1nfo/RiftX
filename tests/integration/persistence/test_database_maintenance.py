"""Real Alembic coverage for SQLite Doctor repair."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

import riftx.database_maintenance as maintenance
from riftx.database_maintenance import (
    DatabaseRepairError,
    SQLiteMigrationStatus,
    inspect_sqlite_migration,
    repair_sqlite_database,
)
from riftx.diagnostics import ALEMBIC_HEAD_REVISION

PARENT_REVISION = "8b1d3f5a7c20"


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _upgrade_to(path: Path, revision: str) -> None:
    config = Config("alembic.ini")
    config.attributes["riftx_database_url"] = _url(path)
    command.upgrade(config, revision)


def _revision(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def test_database_repair_backs_up_parent_revision_and_upgrades_to_head(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "riftx.db"
    _upgrade_to(database_path, PARENT_REVISION)

    result = repair_sqlite_database(_url(database_path), cwd=tmp_path)

    assert result.previous_revisions == (PARENT_REVISION,)
    assert result.backup_path is not None
    assert _revision(result.backup_path) == PARENT_REVISION
    assert _revision(database_path) == ALEMBIC_HEAD_REVISION
    state = inspect_sqlite_migration(_url(database_path), cwd=tmp_path)
    assert state is not None and state.status is SQLiteMigrationStatus.READY


def test_database_repair_restores_backup_after_partial_migration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "riftx.db"
    _upgrade_to(database_path, PARENT_REVISION)

    def fail_after_write(connection: Connection, *_args: object, **_kwargs: object) -> None:
        connection.exec_driver_sql("DROP TABLE alembic_version")
        connection.commit()
        raise RuntimeError("forced migration failure")

    monkeypatch.setattr(maintenance, "_run_upgrade", fail_after_write)

    with pytest.raises(DatabaseRepairError, match="restored") as captured:
        repair_sqlite_database(_url(database_path), cwd=tmp_path)

    assert captured.value.rollback_complete
    assert captured.value.backup_path is not None
    assert _revision(database_path) == PARENT_REVISION
    assert _revision(captured.value.backup_path) == PARENT_REVISION
