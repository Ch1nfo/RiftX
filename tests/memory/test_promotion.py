from pathlib import Path

import pytest

from riftx.memory import (
    ConflictAction,
    MemoryCandidate,
    MemoryCandidateOrigin,
    MemoryScope,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
    MemoryWriter,
    PromotionDecision,
    PromotionPolicy,
)
from riftx.persistence import Database
from riftx.persistence.memory_repositories import SQLAlchemyMemoryRepository


def candidate(
    candidate_id: str,
    *,
    origin: MemoryCandidateOrigin = MemoryCandidateOrigin.DETERMINISTIC_PARSER,
    content: str = "10.10.10.20:443 runs nginx 1.24",
    sources: list[str] | None = None,
    confidence: float = 0.9,
    conflict_key: str | None = "asset:10.10.10.20:443:service",
) -> MemoryCandidate:
    return MemoryCandidate(
        id=candidate_id,
        title="HTTPS service",
        content=content,
        summary=content,
        suggested_type=MemoryType.SEMANTIC,
        suggested_scope=MemoryScope(
            scope_type=MemoryScopeType.ASSET,
            scope_id="engagement-1::10.10.10.20",
        ),
        source_refs=sources or ["parser://nmap/execution-1"],
        confidence=confidence,
        reason_to_remember="Stable asset service fact",
        origin=origin,
        retrieval_keywords=["nginx", "443"],
        conflict_key=conflict_key,
    )


@pytest.mark.parametrize(
    ("origin", "decision"),
    [
        (MemoryCandidateOrigin.DETERMINISTIC_PARSER, PromotionDecision.PROMOTE),
        (MemoryCandidateOrigin.USER_EXPLICIT, PromotionDecision.PROMOTE),
        (MemoryCandidateOrigin.CONFIRMED_FINDING, PromotionDecision.PROMOTE),
        (MemoryCandidateOrigin.STABLE_TOOL_NODE, PromotionDecision.PROMOTE),
        (MemoryCandidateOrigin.MODEL_INFERENCE, PromotionDecision.CANDIDATE_ONLY),
        (MemoryCandidateOrigin.UNVERIFIED_VULNERABILITY, PromotionDecision.CANDIDATE_ONLY),
        (MemoryCandidateOrigin.WEB_CONTENT, PromotionDecision.CANDIDATE_ONLY),
    ],
)
def test_promotion_policy_enforces_candidate_origin(
    origin: MemoryCandidateOrigin,
    decision: PromotionDecision,
) -> None:
    assert PromotionPolicy().assess(candidate("candidate-1", origin=origin)).decision is decision


@pytest.mark.parametrize(
    "content",
    [
        "Authorization: Bearer top-secret",
        "Cookie: session=top-secret",
        "access_token=top-secret",
        "https://storage.example/object?X-Amz-Signature=top-secret",
        "https://storage.example/object?X-Goog-Signature=top-secret",
    ],
)
def test_promotion_policy_rejects_secrets_and_signed_urls(content: str) -> None:
    secret = candidate(
        "secret",
        origin=MemoryCandidateOrigin.USER_EXPLICIT,
        content=content,
    )

    assert PromotionPolicy().assess(secret).decision is PromotionDecision.REJECT


def test_promotion_policy_requires_independent_sources() -> None:
    one_source = candidate(
        "one-source",
        origin=MemoryCandidateOrigin.MULTI_SOURCE_CONFIRMATION,
    )
    two_sources = candidate(
        "two-sources",
        origin=MemoryCandidateOrigin.MULTI_SOURCE_CONFIRMATION,
        sources=["parser://nmap/1", "parser://nuclei/2"],
    )

    assert PromotionPolicy().assess(one_source).decision is PromotionDecision.CANDIDATE_ONLY
    assert PromotionPolicy().assess(two_sources).decision is PromotionDecision.PROMOTE


async def test_writer_deduplicates_and_supersedes_conflicting_facts(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'promotion.db'}")
    await database.create_schema()
    repository = SQLAlchemyMemoryRepository(database.session_factory)
    writer = MemoryWriter(repository)

    created = await writer.write(candidate("candidate-create"))
    merged = await writer.write(
        candidate(
            "candidate-duplicate",
            sources=["parser://nuclei/execution-2"],
            confidence=0.95,
        )
    )
    superseded = await writer.write(
        candidate(
            "candidate-replacement",
            content="10.10.10.20:443 runs nginx 1.25",
            sources=["parser://nmap/execution-3"],
            confidence=0.99,
        )
    )

    assert created.action is ConflictAction.CREATE
    assert created.memory is not None
    assert merged.action is ConflictAction.MERGE
    assert merged.memory is not None
    assert merged.memory.source_refs == [
        "parser://nmap/execution-1",
        "parser://nuclei/execution-2",
    ]
    assert superseded.action is ConflictAction.SUPERSEDE
    assert superseded.memory is not None
    previous = await repository.get(created.memory.id)
    assert previous is not None and previous.status is MemoryStatus.SUPERSEDED
    assert superseded.memory.supersedes == previous.id
    await database.dispose()
