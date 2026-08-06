"""Authoritative database and Official Pack diagnostic tests."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from riftx.diagnostics import ALEMBIC_HEAD_REVISION, SystemDiagnosticsService
from riftx.packs import OfficialPackCatalog, bootstrap_official_packs
from riftx.persistence import Database, SQLAlchemyCapabilityRepository


async def test_system_diagnostics_reports_migration_and_official_pack_state(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await bootstrap_official_packs(
        SQLAlchemyCapabilityRepository(database.session_factory),
        OfficialPackCatalog(),
    )
    service = SystemDiagnosticsService(database.session_factory)

    unmanaged = await service.snapshot()
    assert unmanaged.database.status == "unmanaged"
    assert unmanaged.official_packs.status == "ready"
    assert unmanaged.official_packs.installed_pack_count == 22
    assert unmanaged.official_packs.active_lock_count == 66

    async with database.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": ALEMBIC_HEAD_REVISION},
        )

    ready = await service.snapshot()
    assert ready.database.status == "ready"
    assert ready.database.current_revisions == (ALEMBIC_HEAD_REVISION,)
    await database.dispose()


async def test_system_diagnostics_detects_missing_official_pack_installs(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()

    snapshot = await SystemDiagnosticsService(database.session_factory).snapshot()

    assert snapshot.official_packs.status == "drifted"
    assert snapshot.official_packs.installed_pack_count == 0
    assert snapshot.official_packs.issues
    await database.dispose()


def test_embedded_alembic_head_matches_migration_graph() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == [ALEMBIC_HEAD_REVISION]
