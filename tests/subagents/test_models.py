import pytest
from pydantic import ValidationError

from riftx.context import EvidenceSource, FactCandidate
from riftx.subagents import (
    DelegationPacket,
    FindingCandidate,
    SubagentResult,
    SubagentStatus,
)


def delegation() -> DelegationPacket:
    return DelegationPacket(
        task_id="task-1",
        subagent_type="recon",
        task="Inspect the authorized HTTPS endpoint",
        run_contract_summary="Authorized local service assessment",
        relevant_scope=["127.0.0.1", "127.0.0.1"],
        selected_fact_ids=["fact-1"],
        selected_artifact_refs=["artifact://banner-1"],
        available_tool_ids=["nmap", "nmap"],
        workspace="/workspace",
    )


def fact() -> FactCandidate:
    return FactCandidate(
        subject="127.0.0.1",
        predicate="service:443.product",
        value="nginx",
        natural_language="127.0.0.1:443 runs nginx",
        confidence=0.99,
        source_refs=["artifact://banner-1"],
        source_type=EvidenceSource.DETERMINISTIC_PARSER,
    )


def test_delegation_normalizes_scope_and_independent_tool_allowlist() -> None:
    packet = delegation()

    assert packet.relevant_scope == ["127.0.0.1"]
    assert packet.available_tool_ids == ["nmap"]
    assert "transcript" not in DelegationPacket.model_fields


def test_primary_packet_exposes_only_the_merge_allowlist() -> None:
    result = SubagentResult(
        task_id="task-1",
        status=SubagentStatus.COMPLETED,
        summary="HTTPS service confirmed.",
        confirmed_fact_candidates=[fact()],
        finding_candidates=[
            FindingCandidate(
                title="Exposed HTTPS service",
                severity="info",
                evidence_refs=["artifact://banner-1"],
                confidence=0.95,
            )
        ],
        evidence_refs=["artifact://banner-1"],
        failed_approaches=["UDP probe"],
        unresolved_questions=["TLS policy"],
        recommended_next_actions=["Inspect TLS configuration"],
    )

    payload = result.primary_packet().model_dump(mode="json")

    assert set(payload) == {
        "task_id",
        "status",
        "summary",
        "confirmed_fact_candidates",
        "hypothesis_updates",
        "finding_candidates",
        "evidence_refs",
        "recommended_next_actions",
    }
    assert "failed_approaches" not in payload
    assert "unresolved_questions" not in payload
    assert "memory_candidates" not in payload


def test_result_rejects_candidate_evidence_outside_declared_refs() -> None:
    with pytest.raises(ValidationError, match="candidate evidence is not declared"):
        SubagentResult(
            task_id="task-1",
            status=SubagentStatus.COMPLETED,
            summary="Unverifiable result",
            confirmed_fact_candidates=[fact()],
        )
