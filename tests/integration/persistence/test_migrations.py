import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from tests.unit.persistence.test_schema import EXPECTED_TABLES


def run_alembic(database_path: Path, revision: str) -> None:
    config = Config("alembic.ini")
    old_url = os.environ.get("RIFTX_DATABASE_URL")
    os.environ["RIFTX_DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    try:
        command.upgrade(config, revision) if revision != "base" else command.downgrade(
            config, revision
        )
    finally:
        if old_url is None:
            os.environ.pop("RIFTX_DATABASE_URL", None)
        else:
            os.environ["RIFTX_DATABASE_URL"] = old_url


def sqlite_tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {row[0] for row in rows}


def test_initial_migration_upgrades_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"

    run_alembic(database_path, "head")
    assert sqlite_tables(database_path) == EXPECTED_TABLES

    run_alembic(database_path, "base")
    assert sqlite_tables(database_path) == {"alembic_version"}
