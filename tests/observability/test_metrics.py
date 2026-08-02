from __future__ import annotations

import pytest

from riftx.observability import (
    MetricDirection,
    RuntimeMetricName,
    RuntimeMetricsCalculator,
    RuntimeMetricsEvidence,
)


def test_calculator_exposes_all_eleven_metrics_and_directions() -> None:
    evidence = RuntimeMetricsEvidence(
        completed_tasks=9,
        total_tasks=10,
        repeated_tool_calls=2,
        total_tool_calls=100,
        invalid_tool_calls=1,
        recovery_successes=8,
        recovery_attempts=10,
        duplicate_executions=0,
        total_executions=100,
        preserved_compaction_dimensions=14,
        total_compaction_dimensions=15,
        useful_context_tokens=7_500,
        total_context_tokens=10_000,
        useful_subagent_results=2,
        total_subagent_results=3,
        approval_resume_successes=3,
        resolved_approvals=3,
        failed_browser_actions=1,
        total_browser_actions=20,
        cited_claims=19,
        total_claims=20,
    )

    snapshot = RuntimeMetricsCalculator().calculate("run-1", evidence)

    assert set(snapshot.metrics) == set(RuntimeMetricName)
    assert snapshot.metrics[RuntimeMetricName.TASK_COMPLETION_RATE].value == 0.9
    assert snapshot.metrics[RuntimeMetricName.REPEATED_TOOL_CALL_RATE].value == 0.02
    assert snapshot.metrics[RuntimeMetricName.CONTEXT_TOKEN_EFFICIENCY].value == 0.75
    assert (
        snapshot.metrics[RuntimeMetricName.EXECUTION_DUPLICATION_RATE].direction
        is MetricDirection.LOWER_IS_BETTER
    )
    assert (
        snapshot.metrics[RuntimeMetricName.CITATION_COVERAGE].direction
        is MetricDirection.HIGHER_IS_BETTER
    )


def test_zero_denominator_metric_is_explicitly_unavailable() -> None:
    snapshot = RuntimeMetricsCalculator().calculate(
        "empty-run",
        RuntimeMetricsEvidence(total_tasks=1),
    )

    recovery = snapshot.metrics[RuntimeMetricName.RECOVERY_SUCCESS_RATE]
    assert not recovery.available
    assert recovery.value is None
    assert recovery.numerator == recovery.denominator == 0


def test_evidence_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="repeated_tool_calls"):
        RuntimeMetricsEvidence(repeated_tool_calls=2, total_tool_calls=1)
