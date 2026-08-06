from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from riftx.capabilities import (
    CapabilityKind,
    CapabilitySource,
    CapabilityTrustTier,
    CapabilityVersionStatus,
    capability_pack_digest,
)
from riftx.packs import OFFICIAL_PACK_ROOT, OfficialPackCatalog


def test_official_catalog_loads_versioned_evidence_aware_foundation_bundles() -> None:
    first = OfficialPackCatalog().load()
    second = OfficialPackCatalog().load()

    assert [bundle.source.pack_id for bundle in first] == [
        "passive-recon",
        "pentest-foundation",
        "scope-and-safety",
        "service-enumeration",
        "web-attack-surface",
        "web-request-analysis",
    ]
    assert [bundle.pack for bundle in first] == [bundle.pack for bundle in second]
    for bundle in first:
        assert bundle.pack.manifest.source is CapabilitySource.OFFICIAL
        assert bundle.pack.manifest.publisher == "riftx"
        assert bundle.pack.manifest_digest == capability_pack_digest(bundle.pack.manifest)
        assert bundle.negative_cases
        assert bundle.evaluation_cases
        assert f"## {bundle.source.version}" in bundle.changelog
        versions = {
            version.manifest.capability_id: version
            for version in bundle.capability_versions
        }
        assert {member.capability_id for member in bundle.pack.manifest.members} == set(
            versions
        )
        assert {
            version.manifest.kind for version in bundle.capability_versions
        } >= {CapabilityKind.SKILL, CapabilityKind.TECHNIQUE, CapabilityKind.EVAL_CASE}
        assert all(
            version.status is CapabilityVersionStatus.ACTIVE
            and version.manifest.trust_tier is CapabilityTrustTier.OFFICIAL
            and version.manifest.provenance.source is CapabilitySource.OFFICIAL
            for version in bundle.capability_versions
        )
        for document in bundle.skill_documents:
            version = versions[document.id]
            assert document.source is CapabilitySource.OFFICIAL
            assert version.manifest.kind is CapabilityKind.SKILL
            assert version.manifest.provenance.source_digest == document.digest


def test_official_catalog_rejects_missing_negative_cases(tmp_path: Path) -> None:
    root = _copy_pack(tmp_path, "pentest-foundation")
    (root / "pentest-foundation" / "negative_cases.yaml").write_text("[]\n")

    with pytest.raises(ValueError, match="requires negative cases"):
        OfficialPackCatalog(root).load()


def test_official_catalog_rejects_skill_source_spoofing(tmp_path: Path) -> None:
    root = _copy_pack(tmp_path, "scope-and-safety")
    skill_path = root / "scope-and-safety" / "skills" / "scope-and-safety" / "SKILL.md"
    skill_path.write_text(skill_path.read_text().replace("source: official", "source: operator"))

    with pytest.raises(ValueError, match="source=official"):
        OfficialPackCatalog(root).load()


def test_official_catalog_rejects_tool_requirement_drift(tmp_path: Path) -> None:
    root = _copy_pack(tmp_path, "pentest-foundation")
    pack_path = root / "pentest-foundation" / "pack.yaml"
    pack_path.write_text(pack_path.read_text().replace("  - complete_run\n", ""))

    with pytest.raises(ValueError, match="tool requirements"):
        OfficialPackCatalog(root).load()


def test_official_catalog_rejects_unknown_production_tool(tmp_path: Path) -> None:
    root = _copy_pack(tmp_path, "pentest-foundation")
    pack_path = root / "pentest-foundation" / "pack.yaml"
    pack_path.write_text(
        pack_path.read_text().replace("  - complete_run\n", "  - missing_tool\n")
    )

    with pytest.raises(ValueError, match="unknown production Tools: missing_tool"):
        OfficialPackCatalog(root).load()


def _copy_pack(tmp_path: Path, pack_id: str) -> Path:
    root = tmp_path / "official"
    shutil.copytree(OFFICIAL_PACK_ROOT / pack_id, root / pack_id)
    return root
