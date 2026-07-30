"""Runtime metric contracts for QA-02 observability."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from riftx.domain.base import DomainModel, utc_now


class RuntimeMetricName(StrEnum):
    TASK_COMPLETION_RATE = "task_completion_rate"
    REPEATED_TOOL_CALL_RATE = "repeated_tool_call_rate"
    INVALID_TOOL_CALL_RATE = "invalid_tool_call_rate"
    RECOVERY_SUCCESS_RATE = "recovery_success_rate"
    EXECUTION_DUPLICATION_RATE = "execution_duplication_rate"
    COMPACTION_FIDELITY = "compaction_fidelity"
    CONTEXT_TOKEN_EFFICIENCY = "context_token_efficiency"
    SUBAGENT_UTILITY = "subagent_utility"
    APPROVAL_RESUME_SUCCESS_RATE = "approval_resume_success_rate"
    BROWSER_ACTION_FAILURE_RATE = "browser_action_failure_rate"
    CITATION_COVERAGE = "citation_coverage"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class RuntimeMetricValue(DomainModel):
    name: RuntimeMetricName
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None = Field(default=None, ge=0.0, le=1.0)
    available: bool
    direction: MetricDirection
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ratio(self) -> RuntimeMetricValue:
        if self.numerator > self.denominator:
            raise ValueError("metric numerator cannot exceed denominator")
        if self.denominator == 0:
            if self.available or self.value is not None:
                raise ValueError("zero-denominator metrics must be unavailable")
        else:
            expected = self.numerator / self.denominator
            if not self.available or self.value is None:
                raise ValueError("non-empty metrics must expose a value")
            if abs(self.value - expected) > 1e-12:
                raise ValueError("metric value must equal numerator / denominator")
        return self


class RuntimeMetricsSnapshot(DomainModel):
    run_id: str = Field(min_length=1)
    metrics: dict[RuntimeMetricName, RuntimeMetricValue]
    generated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_all_metrics(self) -> RuntimeMetricsSnapshot:
        if set(self.metrics) != set(RuntimeMetricName):
            missing = sorted(set(RuntimeMetricName) - set(self.metrics))
            extra = sorted(set(self.metrics) - set(RuntimeMetricName))
            raise ValueError(f"runtime metrics mismatch; missing={missing}, extra={extra}")
        for name, metric in self.metrics.items():
            if metric.name is not name:
                raise ValueError(f"metric key {name.value!r} does not match payload")
        return self
