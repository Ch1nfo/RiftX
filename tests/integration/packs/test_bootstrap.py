from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from riftx.application.errors import RepositoryConflictError
from riftx.capabilities import TechniqueContextManager
from riftx.packs import OFFICIAL_PACK_ROOT, OfficialPackCatalog, bootstrap_official_packs
from riftx.persistence import Database, SQLAlchemyCapabilityRepository


@pytest.mark.asyncio
async def test_official_pack_bootstrap_is_idempotent_and_exposes_techniques(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)

    first = await bootstrap_official_packs(repository)
    second = await bootstrap_official_packs(repository)

    assert first == second
    assert [install.pack_id for install in first] == [
        "passive-recon",
        "pentest-foundation",
        "scope-and-safety",
        "service-enumeration",
        "web-attack-surface",
        "web-request-analysis",
    ]
    techniques = await TechniqueContextManager(repository).list_techniques(
        session_id="session-1"
    )
    assert [technique.id for technique in techniques] == [
        "passive-recon.technique",
        "pentest-foundation.technique",
        "scope-and-safety.technique",
        "service-enumeration.technique",
        "web-attack-surface.technique",
        "web-request-analysis.technique",
    ]
    async with database.session_factory() as session:
        counts = {
            table: await session.scalar(text(f"SELECT count(*) FROM {table}"))
            for table in (
                "capabilities",
                "capability_versions",
                "capability_packs",
                "capability_pack_members",
                "capability_pack_installs",
                "capability_pack_locks",
            )
        }
    assert counts == {
        "capabilities": 18,
        "capability_versions": 18,
        "capability_packs": 6,
        "capability_pack_members": 18,
        "capability_pack_installs": 6,
        "capability_pack_locks": 18,
    }
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["manifest", "skill"])
async def test_official_pack_bootstrap_rejects_immutable_bundle_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyCapabilityRepository(database.session_factory)
    await bootstrap_official_packs(repository)
    copied_root = tmp_path / "official"
    shutil.copytree(OFFICIAL_PACK_ROOT, copied_root)

    if drift == "manifest":
        path = copied_root / "pentest-foundation" / "pack.yaml"
        path.write_text(
            path.read_text().replace(
                "Use bounded task, evidence, reasoning, negative-result, and closure loops.",
                "Changed immutable technique content.",
            )
        )
    else:
        path = (
            copied_root
            / "scope-and-safety"
            / "skills"
            / "scope-and-safety"
            / "SKILL.md"
        )
        path.write_text(path.read_text() + "\nChanged immutable skill content.\n")

    with pytest.raises(RepositoryConflictError, match="Version ID already exists"):
        await bootstrap_official_packs(repository, OfficialPackCatalog(copied_root))
    await database.dispose()
