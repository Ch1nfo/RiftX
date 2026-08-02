from __future__ import annotations

import pytest

from riftx.evaluation import (
    InjectedRecoveryFault,
    LongHorizonEvaluator,
    LongHorizonEvidence,
    OneShotFaultInjector,
    RecoveryBoundary,
)


def complete_evidence() -> LongHorizonEvidence:
    tool_ids = [f"tool-{index:03d}" for index in range(100)]
    return LongHorizonEvidence(
        tool_call_ids=tool_ids,
        failed_tool_call_ids=tool_ids[:10],
        processed_tool_call_ids=tool_ids,
        execution_by_tool_call={tool_id: f"execution-{tool_id}" for tool_id in tool_ids},
        execution_launch_counts={tool_id: 1 for tool_id in tool_ids},
        user_message_ids=[f"message-{index}" for index in range(5)],
        approval_ids=[f"approval-{index}" for index in range(3)],
        subagent_session_ids=[f"subagent-{index}" for index in range(3)],
        compaction_checkpoint_ids=["checkpoint-1", "checkpoint-2"],
        model_switch_checkpoint_ids=["checkpoint-model-switch"],
        worker_restart_count=1,
        runner_restart_count=1,
        web_source_ids=[f"source-{index}" for index in range(20)],
        browser_takeover_ids=["takeover-1"],
        recovery_boundaries=list(RecoveryBoundary),
        objective_before="Assess example.com",
        objective_after="Assess example.com",
        scope_digest_before="scope",
        scope_digest_after="scope",
        working_memory_digest_before_restart="memory",
        working_memory_digest_after_restart="memory",
        artifact_ids=["artifact-1"],
        traced_artifact_ids=["artifact-1"],
        temporal_payload_sizes=[128, 1024],
    )


def test_complete_qa_01_evidence_passes() -> None:
    report = LongHorizonEvaluator().evaluate(complete_evidence())

    assert report.passed
    assert report.failures == []
    assert report.observed["tool_calls"] == 100
    assert report.observed["recovery_boundaries"] == 9


def test_duplicate_execution_and_missing_recovery_boundary_fail_gate() -> None:
    evidence = complete_evidence()
    evidence.execution_by_tool_call["tool-001"] = evidence.execution_by_tool_call["tool-000"]
    evidence.recovery_boundaries.remove(RecoveryBoundary.DURING_BROWSER_ACTION)

    report = LongHorizonEvaluator().evaluate(evidence)

    assert not report.passed
    assert "one_execution_per_tool_call" in report.failures
    assert "all_recovery_boundaries" in report.failures


def test_fault_injector_trips_each_boundary_only_once() -> None:
    injector = OneShotFaultInjector({RecoveryBoundary.AFTER_EXECUTION_STARTED})

    with pytest.raises(InjectedRecoveryFault) as failure:
        injector.trip(RecoveryBoundary.AFTER_EXECUTION_STARTED)
    injector.trip(RecoveryBoundary.AFTER_EXECUTION_STARTED)

    assert failure.value.boundary is RecoveryBoundary.AFTER_EXECUTION_STARTED
    assert injector.tripped == (RecoveryBoundary.AFTER_EXECUTION_STARTED,)
