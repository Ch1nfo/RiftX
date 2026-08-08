"""Atomic Official Pack persistence repair tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from riftx.application.errors import RepositoryError
from riftx.diagnostics import SystemDiagnosticsService
from riftx.packs import bootstrap_official_packs
from riftx.persistence import Database, SQLAlchemyCapabilityRepository


@pytest.mark.asyncio
async def test_official_pack_repair_normalizes_install_and_preserves_lock_history(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)
    installs = await bootstrap_official_packs(repository)
    install = installs[0]

    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE capability_pack_installs SET status = 'disabled', "
                "pack_digest = :digest, disabled_at = installed_at "
                "WHERE id = :install_id"
            ),
            {"digest": "0" * 64, "install_id": install.install_id},
        )
        await connection.execute(
            text(
                "UPDATE capability_pack_locks SET capability_digest = :digest "
                "WHERE id = (SELECT id FROM capability_pack_locks "
                "WHERE owner_id = :install_id AND released_at IS NULL LIMIT 1)"
            ),
            {"digest": "0" * 64, "install_id": install.install_id},
        )

    repaired = await bootstrap_official_packs(repository)
    snapshot = await SystemDiagnosticsService(database.session_factory).snapshot()
    async with database.session_factory() as session:
        lock_counts = tuple(
            (
                await session.execute(
                    text(
                        "SELECT count(*), "
                        "sum(CASE WHEN released_at IS NULL THEN 1 ELSE 0 END) "
                        "FROM capability_pack_locks WHERE owner_id = :install_id"
                    ),
                    {"install_id": install.install_id},
                )
            ).one()
        )

    assert repaired[0].status.value == "installed"
    assert repaired[0].state_version == install.state_version + 1
    assert snapshot.official_packs.status == "ready"
    assert lock_counts == (6, 3)
    await database.dispose()


@pytest.mark.asyncio
async def test_official_pack_repair_rejects_unexpected_install_without_partial_commit(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)
    installs = await bootstrap_official_packs(repository)
    missing = installs[0]
    source = installs[1]

    async with database.engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM capability_pack_locks WHERE owner_id = :install_id"),
            {"install_id": missing.install_id},
        )
        await connection.execute(
            text("DELETE FROM capability_pack_installs WHERE id = :install_id"),
            {"install_id": missing.install_id},
        )
        await connection.execute(
            text(
                "INSERT INTO capability_pack_installs "
                "(id, scope_type, scope_id, pack_id, pack_version_id, pack_version, "
                "pack_digest, status, state_version, previous_pack_version_id, "
                "installed_at, updated_at, disabled_at) "
                "SELECT 'unexpected-install', scope_type, scope_id, 'unexpected-pack', "
                "pack_version_id, pack_version, pack_digest, status, state_version, "
                "previous_pack_version_id, installed_at, updated_at, disabled_at "
                "FROM capability_pack_installs WHERE id = :source_id"
            ),
            {"source_id": source.install_id},
        )

    with pytest.raises(RepositoryError, match="unexpected"):
        await bootstrap_official_packs(repository)

    async with database.session_factory() as session:
        install_ids = set(
            await session.scalars(
                text(
                    "SELECT id FROM capability_pack_installs "
                    "WHERE scope_type = 'official' AND scope_id = 'riftx'"
                )
            )
        )
    assert missing.install_id not in install_ids
    assert "unexpected-install" in install_ids
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "assignment"),
    [
        ("capability_versions", "manifest_digest = '" + "0" * 64 + "'"),
        ("capability_pack_members", "capability_digest = '" + "0" * 64 + "'"),
    ],
)
async def test_official_pack_repair_rejects_immutable_drift_and_rolls_back(
    tmp_path: Path,
    table: str,
    assignment: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)
    installs = await bootstrap_official_packs(repository)
    missing = installs[0]

    async with database.engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM capability_pack_locks WHERE owner_id = :install_id"),
            {"install_id": missing.install_id},
        )
        await connection.execute(
            text("DELETE FROM capability_pack_installs WHERE id = :install_id"),
            {"install_id": missing.install_id},
        )
        await connection.execute(
            text(f"UPDATE {table} SET {assignment} WHERE rowid = (SELECT max(rowid) FROM {table})")
        )

    with pytest.raises(RepositoryError):
        await bootstrap_official_packs(repository)

    async with database.session_factory() as session:
        restored = await session.scalar(
            text("SELECT count(*) FROM capability_pack_installs WHERE id = :install_id"),
            {"install_id": missing.install_id},
        )
    assert restored == 0
    await database.dispose()
