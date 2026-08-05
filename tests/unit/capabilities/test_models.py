from __future__ import annotations

from datetime import UTC, datetime

import pytest

from riftx.api.schemas.capabilities import (
    CapabilityCandidateResponse,
    CapabilityVersionResponse,
    CreateCapabilityCandidateRequest,
    CreateCapabilityEvaluationResultRequest,
    CreateCapabilityVersionRequest,
)
from riftx.capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilityCandidateStatus,
    CapabilityDependency,
    CapabilityDependencyKind,
    CapabilityEffectClass,
    CapabilityKind,
    CapabilityManifest,
    CapabilityPackManifest,
    CapabilityPackMember,
    CapabilityPermission,
    CapabilityProvenance,
    CapabilitySource,
    CapabilityTrustTier,
    ConfirmationPolicy,
    EvaluationResultStatus,
    EvidenceContract,
    capability_manifest_digest,
    capability_pack_digest,
    evaluation_report_digest,
)
from riftx.domain.enums import ApprovalLevel

NOW = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)


def provenance() -> CapabilityProvenance:
    return CapabilityProvenance(
        publisher="riftx",
        source=CapabilitySource.OFFICIAL,
        source_reference="builtin://web/request-analysis",
        authored_by="riftx-maintainers",
        authored_at=NOW,
        source_digest="a" * 64,
    )


def manifest(*, version: str = "1.0.0") -> CapabilityManifest:
    return CapabilityManifest(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        capability_id="web.request-analysis",
        version=version,
        kind=CapabilityKind.TECHNIQUE,
        title="Web request analysis",
        description="Compare bounded request and response evidence.",
        domains=("web", "traffic"),
        triggers=("request diff", "response diff"),
        dependencies=(
            CapabilityDependency(
                kind=CapabilityDependencyKind.TOOL,
                reference="target_http",
                version_constraint=">=1.0.0",
            ),
        ),
        permission=CapabilityPermission(
            effect_class=CapabilityEffectClass.TARGET_INTERACTION,
            approval_level=ApprovalLevel.SENSITIVE,
            requires_scope=True,
        ),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        evidence_contract=EvidenceContract(
            required_refs=("request", "response"),
            minimum_independent_sources=1,
            confirmation_policy=ConfirmationPolicy.EXPLICIT_VERIFICATION,
        ),
        provenance=provenance(),
        evaluation_case_ids=("eval.web.request-analysis",),
        trust_tier=CapabilityTrustTier.OFFICIAL,
    )


def test_manifest_digest_is_stable_and_binds_versioned_content() -> None:
    first = manifest()
    replay = CapabilityManifest.model_validate(first.model_dump(mode="json"))

    assert capability_manifest_digest(first) == capability_manifest_digest(replay)
    assert capability_manifest_digest(first) != capability_manifest_digest(
        manifest(version="1.0.1")
    )


def test_target_interaction_permission_requires_scope() -> None:
    with pytest.raises(ValueError, match="must require scope"):
        CapabilityPermission(
            effect_class=CapabilityEffectClass.TARGET_INTERACTION,
            approval_level=ApprovalLevel.SENSITIVE,
            requires_scope=False,
        )


def test_independent_source_contract_requires_two_sources() -> None:
    with pytest.raises(ValueError, match="at least two sources"):
        EvidenceContract(
            required_refs=("source",),
            minimum_independent_sources=1,
            confirmation_policy=ConfirmationPolicy.INDEPENDENT_SOURCES,
        )


def test_pack_digest_locks_exact_member_versions() -> None:
    capability = manifest()
    member = CapabilityPackMember(
        capability_id=capability.capability_id,
        version=capability.version,
        version_digest=capability_manifest_digest(capability),
    )
    pack = CapabilityPackManifest(
        schema_version="riftx.capability-pack/v1",
        pack_id="official.web-foundation",
        version="1.0.0",
        title="Official Web Foundation",
        description="Baseline web assessment capabilities.",
        source=CapabilitySource.OFFICIAL,
        publisher="riftx",
        members=(member,),
        provenance=provenance(),
    )

    assert len(capability_pack_digest(pack)) == 64
    with pytest.raises(ValueError, match="member IDs must be unique"):
        pack.model_copy(update={"members": (member, member)}, deep=True).model_validate(
            {**pack.model_dump(), "members": [member, member]}
        )


def test_api_commands_create_separate_version_candidate_and_evaluation_models() -> None:
    capability, version = CreateCapabilityVersionRequest(manifest=manifest()).to_domain()
    candidate = CreateCapabilityCandidateRequest(
        proposed_manifest=manifest(version="1.1.0"),
        proposed_by="operator-1",
        source_run_id="run-1",
    ).to_domain()
    report = {"passed": True, "cases": ["eval.web.request-analysis"]}
    evaluation = CreateCapabilityEvaluationResultRequest(
        promotion_id="promotion-1",
        evaluator="security-eval/v1",
        status=EvaluationResultStatus.PASSED,
        scenario_ids=("eval.web.request-analysis",),
        report=report,
    ).to_domain()

    assert capability.capability_id == version.manifest.capability_id
    assert version.manifest_digest == capability_manifest_digest(version.manifest)
    assert candidate.status is CapabilityCandidateStatus.DRAFT
    assert candidate.candidate_digest == capability_manifest_digest(
        candidate.proposed_manifest
    )
    assert evaluation.report_digest == evaluation_report_digest(report)
    assert CapabilityVersionResponse.from_domain(version).manifest == version.manifest
    assert CapabilityCandidateResponse.from_domain(candidate).candidate_id == candidate.candidate_id
