from riftx.context import EvidenceSource, FactCandidate
from riftx.domain import Finding, FindingSeverity, FindingStatus
from riftx.memory import (
    MemoryCandidateFactory,
    MemoryCandidateOrigin,
    MemoryScope,
    MemoryScopeType,
    PromotionDecision,
    PromotionPolicy,
)


def test_fact_candidates_preserve_deterministic_and_model_trust() -> None:
    factory = MemoryCandidateFactory()
    scope = MemoryScope(
        scope_type=MemoryScopeType.ASSET,
        scope_id="engagement-1::10.10.10.20",
    )
    deterministic = factory.from_fact(
        FactCandidate(
            subject="10.10.10.20",
            predicate="service:443.product",
            value="nginx",
            natural_language="10.10.10.20:443 runs nginx",
            confidence=0.95,
            source_refs=["parser://nmap/execution-1"],
            source_type=EvidenceSource.DETERMINISTIC_PARSER,
        ),
        scope=scope,
    )
    inferred = deterministic.model_copy(
        update={"origin": MemoryCandidateOrigin.MODEL_INFERENCE}
    )

    assert deterministic.origin is MemoryCandidateOrigin.DETERMINISTIC_PARSER
    assert PromotionPolicy().assess(deterministic).decision is PromotionDecision.PROMOTE
    assert PromotionPolicy().assess(inferred).decision is PromotionDecision.CANDIDATE_ONLY


def test_only_confirmed_finding_can_auto_promote() -> None:
    factory = MemoryCandidateFactory()
    draft = Finding(
        id="finding-1",
        run_id="run-1",
        title="Exposed admin service",
        severity=FindingSeverity.HIGH,
        affected_assets=["10.10.10.20"],
        description="The admin service is exposed.",
    )
    confirmed = draft.model_copy(update={"status": FindingStatus.CONFIRMED})

    draft_candidate = factory.from_finding(draft, engagement_id="engagement-1")
    confirmed_candidate = factory.from_finding(
        confirmed,
        engagement_id="engagement-1",
    )

    assert draft_candidate.origin is MemoryCandidateOrigin.UNVERIFIED_VULNERABILITY
    assert (
        PromotionPolicy().assess(draft_candidate).decision
        is PromotionDecision.CANDIDATE_ONLY
    )
    assert confirmed_candidate.origin is MemoryCandidateOrigin.CONFIRMED_FINDING
    assert (
        PromotionPolicy().assess(confirmed_candidate).decision
        is PromotionDecision.PROMOTE
    )
    assert confirmed_candidate.suggested_scope.scope_id == "engagement-1::10.10.10.20"
