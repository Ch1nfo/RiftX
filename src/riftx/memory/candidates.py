"""Adapters from authoritative Runtime results to controlled Memory Candidates."""

from __future__ import annotations

from riftx.context.working_memory import ConfirmedFact, EvidenceSource, FactCandidate
from riftx.domain import Finding, FindingStatus

from .models import MemoryScope, MemoryScopeType, MemoryType
from .promotion import MemoryCandidate, MemoryCandidateOrigin


class MemoryCandidateFactory:
    def from_fact(
        self,
        fact: FactCandidate | ConfirmedFact,
        *,
        scope: MemoryScope,
    ) -> MemoryCandidate:
        source_types = (
            set(fact.source_types.values())
            if isinstance(fact, ConfirmedFact)
            else {fact.source_type}
        )
        if source_types == {EvidenceSource.DETERMINISTIC_PARSER}:
            origin = MemoryCandidateOrigin.DETERMINISTIC_PARSER
        elif len(fact.source_refs) >= 2:
            origin = MemoryCandidateOrigin.MULTI_SOURCE_CONFIRMATION
        elif EvidenceSource.USER_DECISION in source_types:
            origin = MemoryCandidateOrigin.USER_EXPLICIT
        else:
            origin = MemoryCandidateOrigin.MODEL_INFERENCE
        return MemoryCandidate(
            title=f"{fact.subject}: {fact.predicate}",
            content=fact.natural_language,
            summary=fact.natural_language,
            suggested_type=MemoryType.SEMANTIC,
            suggested_scope=scope,
            source_refs=fact.source_refs,
            confidence=fact.confidence,
            reason_to_remember="Reusable confirmed Working Memory fact",
            origin=origin,
            retrieval_keywords=[fact.subject, fact.predicate, str(fact.value)],
            conflict_key=f"{fact.subject}:{fact.predicate}",
        )

    def from_finding(
        self,
        finding: Finding,
        *,
        engagement_id: str,
    ) -> MemoryCandidate:
        asset = finding.affected_assets[0] if len(finding.affected_assets) == 1 else None
        scope = MemoryScope(
            scope_type=(MemoryScopeType.ASSET if asset else MemoryScopeType.ENGAGEMENT),
            scope_id=(f"{engagement_id}::{asset}" if asset else engagement_id),
        )
        source_refs = [f"finding://{finding.id}"]
        source_refs.extend(
            f"artifact://{item.artifact_id}"
            for item in finding.evidence
            if item.artifact_id
        )
        source_refs.extend(
            f"execution://{item.execution_id}"
            for item in finding.evidence
            if item.execution_id
        )
        return MemoryCandidate(
            title=finding.title,
            content=finding.description or finding.title,
            summary=finding.title,
            suggested_type=MemoryType.EPISODIC,
            suggested_scope=scope,
            source_refs=source_refs,
            confidence=1.0 if finding.status is FindingStatus.CONFIRMED else 0.5,
            importance=_finding_importance(finding.severity.value),
            reason_to_remember="Confirmed security finding history",
            origin=(
                MemoryCandidateOrigin.CONFIRMED_FINDING
                if finding.status is FindingStatus.CONFIRMED
                else MemoryCandidateOrigin.UNVERIFIED_VULNERABILITY
            ),
            retrieval_keywords=[finding.title, *finding.affected_assets],
            conflict_key=f"finding:{finding.id}",
        )

    def from_explicit_user_request(
        self,
        *,
        content: str,
        scope: MemoryScope,
        source_message_id: str,
        memory_type: MemoryType = MemoryType.USER_PREFERENCE,
    ) -> MemoryCandidate:
        return MemoryCandidate(
            title=content[:120],
            content=content,
            summary=content[:240],
            suggested_type=memory_type,
            suggested_scope=scope,
            source_refs=[f"user://messages/{source_message_id}"],
            confidence=1.0,
            importance=0.8,
            reason_to_remember="User explicitly requested durable memory",
            origin=MemoryCandidateOrigin.USER_EXPLICIT,
        )

    def from_stable_node_fact(
        self,
        *,
        node_id: str,
        title: str,
        content: str,
        source_ref: str,
        keywords: list[str] | None = None,
    ) -> MemoryCandidate:
        return MemoryCandidate(
            title=title,
            content=content,
            summary=content[:240],
            suggested_type=MemoryType.PROCEDURAL,
            suggested_scope=MemoryScope(
                scope_type=MemoryScopeType.NODE,
                scope_id=node_id,
            ),
            source_refs=[source_ref],
            confidence=1.0,
            importance=0.7,
            reason_to_remember="Stable Tool or Node configuration",
            origin=MemoryCandidateOrigin.STABLE_TOOL_NODE,
            retrieval_keywords=keywords or [],
        )


def _finding_importance(severity: str) -> float:
    return {
        "critical": 1.0,
        "high": 0.9,
        "medium": 0.75,
        "low": 0.6,
        "info": 0.5,
    }[severity]
