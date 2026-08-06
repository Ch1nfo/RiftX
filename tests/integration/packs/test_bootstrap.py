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
        "authn-authz-audit",
        "code-audit-foundation",
        "credential-handling",
        "dependency-and-supply-chain",
        "deserialization-audit",
        "entrypoint-discovery",
        "evidence-and-reporting",
        "file-upload-and-path-audit",
        "finding-verification",
        "injection-audit",
        "negative-results",
        "passive-recon",
        "pentest-foundation",
        "repository-mapping",
        "scope-and-safety",
        "secret-and-config-audit",
        "service-enumeration",
        "ssrf-and-outbound-request-audit",
        "variant-analysis",
        "vulnerability-verification",
        "web-attack-surface",
        "web-request-analysis",
    ]
    techniques = await TechniqueContextManager(repository).list_techniques(
        session_id="session-1"
    )
    assert [technique.id for technique in techniques] == [
        "authn-authz-audit.technique",
        "code-audit-foundation.technique",
        "credential-handling.technique",
        "dependency-and-supply-chain.technique",
        "deserialization-audit.technique",
        "entrypoint-discovery.technique",
        "evidence-and-reporting.technique",
        "file-upload-and-path-audit.technique",
        "finding-verification.technique",
        "injection-audit.technique",
        "negative-results.technique",
        "passive-recon.technique",
        "pentest-foundation.technique",
        "repository-mapping.technique",
        "scope-and-safety.technique",
        "secret-and-config-audit.technique",
        "service-enumeration.technique",
        "ssrf-and-outbound-request-audit.technique",
        "variant-analysis.technique",
        "vulnerability-verification.technique",
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
        "capabilities": 66,
        "capability_versions": 66,
        "capability_packs": 22,
        "capability_pack_members": 66,
        "capability_pack_installs": 22,
        "capability_pack_locks": 66,
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
