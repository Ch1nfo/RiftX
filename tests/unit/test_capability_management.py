from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from riftx.capability_management import (
    CapabilityManagementError,
    inspect_local_capability_state,
)
from riftx.config import RiftXConfig
from riftx.database_maintenance import repair_sqlite_database
from riftx.packs import bootstrap_official_packs
from riftx.persistence import Database, SQLAlchemyCapabilityRepository


def _config(database_path: Path) -> RiftXConfig:
    return RiftXConfig.model_validate(
        {"database": {"url": f"sqlite+aiosqlite:///{database_path}"}}
    )


def _bootstrap(config: RiftXConfig, tmp_path: Path) -> None:
    repair_sqlite_database(config.database.url, cwd=tmp_path)

    async def operation() -> None:
        database = Database(config.database.url)
        try:
            await bootstrap_official_packs(
                SQLAlchemyCapabilityRepository(database.session_factory)
            )
        finally:
            await database.dispose()

    asyncio.run(operation())


def test_local_capability_state_lists_authoritative_official_inventory(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "riftx.db")
    _bootstrap(config, tmp_path)

    state = inspect_local_capability_state(config, cwd=tmp_path)

    assert state.verification_status == "ready"
    assert len(state.capabilities) == 30
    assert len(state.packs) == 10
    assert all(item.status == "active" for item in state.capabilities)
    assert all(item.persistence_status == "ready" for item in state.packs)
    assert {item.pack_id for item in state.packs} >= {
        "pentest-foundation",
        "scope-and-safety",
    }


def test_local_capability_state_refuses_missing_database_without_creating_it(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "missing.db"

    with pytest.raises(CapabilityManagementError, match="not initialized"):
        inspect_local_capability_state(_config(database_path), cwd=tmp_path)

    assert not database_path.exists()


def test_local_capability_state_reports_pack_lock_drift(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    config = _config(database_path)
    _bootstrap(config, tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM capability_pack_locks WHERE id = ("
            "SELECT capability_pack_locks.id FROM capability_pack_locks "
            "JOIN capability_pack_installs "
            "ON capability_pack_installs.id = capability_pack_locks.owner_id "
            "WHERE capability_pack_installs.pack_id = 'pentest-foundation' "
            "AND capability_pack_locks.released_at IS NULL LIMIT 1)"
        )

    state = inspect_local_capability_state(config, cwd=tmp_path)

    assert state.verification_status == "drifted"
    assert "lock_set_drift:pentest-foundation" in state.issues
    assert next(
        item for item in state.packs if item.pack_id == "pentest-foundation"
    ).persistence_status == "drifted"
