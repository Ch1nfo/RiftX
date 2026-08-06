from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from riftx.capabilities import (
    CapabilityEffectClass,
    CapabilityKind,
    CapabilitySource,
    CapabilityTrustTier,
    CapabilityVersionStatus,
    capability_pack_digest,
)
from riftx.domain.enums import ApprovalLevel
from riftx.packs import OFFICIAL_PACK_ROOT, OfficialPackCatalog


def test_official_catalog_loads_versioned_evidence_aware_foundation_bundles() -> None:
    first = OfficialPackCatalog().load()
    second = OfficialPackCatalog().load()

    assert [bundle.source.pack_id for bundle in first] == [
        "authn-authz-audit",
        "code-audit-foundation",
        "credential-handling",
        "dependency-and-supply-chain",
        "entrypoint-discovery",
        "evidence-and-reporting",
        "file-upload-and-path-audit",
        "injection-audit",
        "negative-results",
        "passive-recon",
        "pentest-foundation",
        "repository-mapping",
        "scope-and-safety",
        "secret-and-config-audit",
        "service-enumeration",
        "ssrf-and-outbound-request-audit",
        "vulnerability-verification",
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


def test_code_audit_packs_use_only_production_safe_code_workflows() -> None:
    pack_ids = {
        "authn-authz-audit",
        "code-audit-foundation",
        "dependency-and-supply-chain",
        "entrypoint-discovery",
        "file-upload-and-path-audit",
        "injection-audit",
        "repository-mapping",
        "secret-and-config-audit",
        "ssrf-and-outbound-request-audit",
    }
    bundles = {
        bundle.source.pack_id: bundle
        for bundle in OfficialPackCatalog().load()
        if bundle.source.pack_id in pack_ids
    }

    assert set(bundles) == pack_ids
    analysis_tools = {
        "glob",
        "grep",
        "read_many_files",
        "symbol_search",
        "find_references",
        "record_observation",
        "propose_hypothesis",
        "propose_finding",
        "record_negative_result",
    }
    expected_tools = {
        "authn-authz-audit": analysis_tools | {"call_hierarchy"},
        "code-audit-foundation": {
            "list_files",
            "read_many_files",
            "symbol_search",
            "list_ready_tasks",
            "add_task",
            "query_reasoning_graph",
            "record_observation",
            "record_negative_result",
            "complete_task",
            "complete_run",
        },
        "dependency-and-supply-chain": analysis_tools | {"list_files"},
        "entrypoint-discovery": analysis_tools
        - {"propose_finding"}
        | {"call_hierarchy"},
        "file-upload-and-path-audit": analysis_tools | {"call_hierarchy"},
        "injection-audit": analysis_tools | {"call_hierarchy"},
        "repository-mapping": analysis_tools
        - {"propose_hypothesis", "propose_finding"}
        | {"list_files"},
        "secret-and-config-audit": analysis_tools | {"list_files"},
        "ssrf-and-outbound-request-audit": analysis_tools | {"call_hierarchy"},
    }
    for pack_id, bundle in bundles.items():
        assert set(bundle.source.tool_requirements) == expected_tools[pack_id]
        assert len(bundle.capability_versions) == 3
        assert len(bundle.skill_documents) == 1
        assert len(bundle.negative_cases) >= 2
        assert len(bundle.evaluation_cases) == 1
        assert all(
            version.manifest.permission.approval_level is ApprovalLevel.NEVER
            and not version.manifest.permission.requires_scope
            and not version.manifest.permission.credential_references
            and version.manifest.permission.effect_class
            is (
                CapabilityEffectClass.READ_ONLY
                if version.manifest.kind is CapabilityKind.EVAL_CASE
                else CapabilityEffectClass.LOCAL_MUTATION
            )
            for version in bundle.capability_versions
        )


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
