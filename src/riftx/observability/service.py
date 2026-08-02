"""Compute bounded run-scoped observability snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from riftx.application.errors import EntityNotFoundError

from .models import (
    MetricDirection,
    RuntimeMetricName,
    RuntimeMetricsSnapshot,
    RuntimeMetricValue,
)


@dataclass(frozen=True, slots=True)
class RuntimeMetricsEvidence:
    completed_tasks: int = 0
    total_tasks: int = 0
    repeated_tool_calls: int = 0
    total_tool_calls: int = 0
    invalid_tool_calls: int = 0
    recovery_successes: int = 0
    recovery_attempts: int = 0
    duplicate_executions: int = 0
    total_executions: int = 0
    preserved_compaction_dimensions: int = 0
    total_compaction_dimensions: int = 0
    useful_context_tokens: int = 0
    total_context_tokens: int = 0
    useful_subagent_results: int = 0
    total_subagent_results: int = 0
    approval_resume_successes: int = 0
    resolved_approvals: int = 0
    failed_browser_actions: int = 0
    total_browser_actions: int = 0
    cited_claims: int = 0
    total_claims: int = 0

    def __post_init__(self) -> None:
        pairs = {
            "completed_tasks": (self.completed_tasks, self.total_tasks),
            "repeated_tool_calls": (self.repeated_tool_calls, self.total_tool_calls),
            "invalid_tool_calls": (self.invalid_tool_calls, self.total_tool_calls),
            "recovery_successes": (self.recovery_successes, self.recovery_attempts),
            "duplicate_executions": (self.duplicate_executions, self.total_executions),
            "preserved_compaction_dimensions": (
                self.preserved_compaction_dimensions,
                self.total_compaction_dimensions,
            ),
            "useful_context_tokens": (
                self.useful_context_tokens,
                self.total_context_tokens,
            ),
            "useful_subagent_results": (
                self.useful_subagent_results,
                self.total_subagent_results,
            ),
            "approval_resume_successes": (
                self.approval_resume_successes,
                self.resolved_approvals,
            ),
            "failed_browser_actions": (
                self.failed_browser_actions,
                self.total_browser_actions,
            ),
            "cited_claims": (self.cited_claims, self.total_claims),
        }
        for name, (numerator, denominator) in pairs.items():
            if numerator < 0 or denominator < 0:
                raise ValueError(f"{name} counts must be non-negative")
            if numerator > denominator:
                raise ValueError(f"{name} cannot exceed its denominator")


class RuntimeObservabilityRepository(Protocol):
    async def collect(self, run_id: str) -> RuntimeMetricsEvidence | None: ...


class RuntimeMetricsCalculator:
    def calculate(
        self,
        run_id: str,
        evidence: RuntimeMetricsEvidence,
    ) -> RuntimeMetricsSnapshot:
        metrics = {
            RuntimeMetricName.TASK_COMPLETION_RATE: _metric(
                RuntimeMetricName.TASK_COMPLETION_RATE,
                evidence.completed_tasks,
                evidence.total_tasks,
                MetricDirection.HIGHER_IS_BETTER,
                "Completed tasks divided by observed tasks.",
            ),
            RuntimeMetricName.REPEATED_TOOL_CALL_RATE: _metric(
                RuntimeMetricName.REPEATED_TOOL_CALL_RATE,
                evidence.repeated_tool_calls,
                evidence.total_tool_calls,
                MetricDirection.LOWER_IS_BETTER,
                "Semantically repeated Tool Calls beyond the first occurrence.",
            ),
            RuntimeMetricName.INVALID_TOOL_CALL_RATE: _metric(
                RuntimeMetricName.INVALID_TOOL_CALL_RATE,
                evidence.invalid_tool_calls,
                evidence.total_tool_calls,
                MetricDirection.LOWER_IS_BETTER,
                "Persisted Tool Calls missing a valid tool identity or execution snapshot.",
            ),
            RuntimeMetricName.RECOVERY_SUCCESS_RATE: _metric(
                RuntimeMetricName.RECOVERY_SUCCESS_RATE,
                evidence.recovery_successes,
                evidence.recovery_attempts,
                MetricDirection.HIGHER_IS_BETTER,
                "Successful durable reconciliation outcomes divided by recovery attempts.",
            ),
            RuntimeMetricName.EXECUTION_DUPLICATION_RATE: _metric(
                RuntimeMetricName.EXECUTION_DUPLICATION_RATE,
                evidence.duplicate_executions,
                evidence.total_executions,
                MetricDirection.LOWER_IS_BETTER,
                "Executions beyond the first row for a logical idempotency key.",
            ),
            RuntimeMetricName.COMPACTION_FIDELITY: _metric(
                RuntimeMetricName.COMPACTION_FIDELITY,
                evidence.preserved_compaction_dimensions,
                evidence.total_compaction_dimensions,
                MetricDirection.HIGHER_IS_BETTER,
                "Canonical checkpoint dimensions preserved across Compaction.",
            ),
            RuntimeMetricName.CONTEXT_TOKEN_EFFICIENCY: _metric(
                RuntimeMetricName.CONTEXT_TOKEN_EFFICIENCY,
                evidence.useful_context_tokens,
                evidence.total_context_tokens,
                MetricDirection.HIGHER_IS_BETTER,
                "Task-bearing Context tokens divided by all estimated input tokens.",
            ),
            RuntimeMetricName.SUBAGENT_UTILITY: _metric(
                RuntimeMetricName.SUBAGENT_UTILITY,
                evidence.useful_subagent_results,
                evidence.total_subagent_results,
                MetricDirection.HIGHER_IS_BETTER,
                "Subagents returning a completed or partial bounded merge packet.",
            ),
            RuntimeMetricName.APPROVAL_RESUME_SUCCESS_RATE: _metric(
                RuntimeMetricName.APPROVAL_RESUME_SUCCESS_RATE,
                evidence.approval_resume_successes,
                evidence.resolved_approvals,
                MetricDirection.HIGHER_IS_BETTER,
                "Approved Tool Calls that resumed beyond the waiting state.",
            ),
            RuntimeMetricName.BROWSER_ACTION_FAILURE_RATE: _metric(
                RuntimeMetricName.BROWSER_ACTION_FAILURE_RATE,
                evidence.failed_browser_actions,
                evidence.total_browser_actions,
                MetricDirection.LOWER_IS_BETTER,
                "Failed Browser Actions divided by all persisted Browser Actions.",
            ),
            RuntimeMetricName.CITATION_COVERAGE: _metric(
                RuntimeMetricName.CITATION_COVERAGE,
                evidence.cited_claims,
                evidence.total_claims,
                MetricDirection.HIGHER_IS_BETTER,
                "Web research claims carrying at least one Source-backed Evidence Span.",
            ),
        }
        return RuntimeMetricsSnapshot(run_id=run_id, metrics=metrics)


class RuntimeObservabilityService:
    def __init__(
        self,
        repository: RuntimeObservabilityRepository,
        calculator: RuntimeMetricsCalculator | None = None,
    ) -> None:
        self._repository = repository
        self._calculator = calculator or RuntimeMetricsCalculator()

    async def snapshot(self, run_id: str) -> RuntimeMetricsSnapshot:
        evidence = await self._repository.collect(run_id)
        if evidence is None:
            raise EntityNotFoundError("Run", run_id)
        return self._calculator.calculate(run_id, evidence)


def _metric(
    name: RuntimeMetricName,
    numerator: int,
    denominator: int,
    direction: MetricDirection,
    description: str,
) -> RuntimeMetricValue:
    available = denominator > 0
    return RuntimeMetricValue(
        name=name,
        numerator=numerator,
        denominator=denominator,
        value=(numerator / denominator if available else None),
        available=available,
        direction=direction,
        description=description,
    )
