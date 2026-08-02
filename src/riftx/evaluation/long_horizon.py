"""Deterministic acceptance contracts for long-horizon and recovery evaluation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecoveryBoundary(StrEnum):
    """Mandatory crash boundaries from the Post-V2 QA-01 gate."""

    AFTER_CONTEXT_COMPILE = "after_context_compile"
    AFTER_MODEL_CALL = "after_model_call"
    AFTER_TOOL_INTENT_PERSISTED = "after_tool_intent_persisted"
    AFTER_EXECUTION_STARTED = "after_execution_started"
    AFTER_EXECUTION_COMPLETED = "after_execution_completed_before_processing"
    WHILE_WAITING_APPROVAL = "while_waiting_approval"
    DURING_COMPACTION = "during_compaction"
    DURING_SUBAGENT = "during_subagent"
    DURING_BROWSER_ACTION = "during_browser_action"


class InjectedRecoveryFault(RuntimeError):
    """One-shot synthetic process failure raised at a durable boundary."""

    def __init__(self, boundary: RecoveryBoundary) -> None:
        self.boundary = boundary
        super().__init__(f"injected recovery fault at {boundary.value}")


class OneShotFaultInjector:
    """Trip configured recovery boundaries exactly once.

    The injector deliberately contains no I/O or global state, so a test can place it
    around a real adapter boundary and emulate the process disappearing immediately
    after the durable side of an operation completed.
    """

    def __init__(self, boundaries: set[RecoveryBoundary] | None = None) -> None:
        self._armed = set(boundaries or set(RecoveryBoundary))
        self._tripped: list[RecoveryBoundary] = []

    @property
    def tripped(self) -> tuple[RecoveryBoundary, ...]:
        return tuple(self._tripped)

    def trip(self, boundary: RecoveryBoundary) -> None:
        if boundary not in self._armed:
            return
        self._armed.remove(boundary)
        self._tripped.append(boundary)
        raise InjectedRecoveryFault(boundary)


class LongHorizonRequirements(BaseModel):
    """The fixed QA-01 workload and recovery gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_calls: int = Field(default=100, ge=1)
    tool_failures: int = Field(default=10, ge=0)
    user_supplements: int = Field(default=5, ge=0)
    approvals: int = Field(default=3, ge=0)
    subagents: int = Field(default=3, ge=0)
    compactions: int = Field(default=2, ge=0)
    model_switches: int = Field(default=1, ge=0)
    worker_restarts: int = Field(default=1, ge=0)
    runner_restarts: int = Field(default=1, ge=0)
    web_sources: int = Field(default=20, ge=0)
    browser_takeovers: int = Field(default=1, ge=0)
    max_temporal_payload_bytes: int = Field(default=64 * 1024, ge=1)

    @model_validator(mode="after")
    def failures_cannot_exceed_calls(self) -> LongHorizonRequirements:
        if self.tool_failures > self.tool_calls:
            raise ValueError("tool_failures cannot exceed tool_calls")
        return self


class LongHorizonEvidence(BaseModel):
    """Repository-derived evidence consumed by the QA-01 evaluator."""

    model_config = ConfigDict(extra="forbid")

    tool_call_ids: list[str] = Field(default_factory=list)
    failed_tool_call_ids: list[str] = Field(default_factory=list)
    processed_tool_call_ids: list[str] = Field(default_factory=list)
    execution_by_tool_call: dict[str, str] = Field(default_factory=dict)
    execution_launch_counts: dict[str, int] = Field(default_factory=dict)
    user_message_ids: list[str] = Field(default_factory=list)
    approval_ids: list[str] = Field(default_factory=list)
    subagent_session_ids: list[str] = Field(default_factory=list)
    compaction_checkpoint_ids: list[str] = Field(default_factory=list)
    model_switch_checkpoint_ids: list[str] = Field(default_factory=list)
    worker_restart_count: int = Field(default=0, ge=0)
    runner_restart_count: int = Field(default=0, ge=0)
    web_source_ids: list[str] = Field(default_factory=list)
    browser_takeover_ids: list[str] = Field(default_factory=list)
    recovery_boundaries: list[RecoveryBoundary] = Field(default_factory=list)
    objective_before: str
    objective_after: str
    scope_digest_before: str
    scope_digest_after: str
    working_memory_digest_before_restart: str
    working_memory_digest_after_restart: str
    artifact_ids: list[str] = Field(default_factory=list)
    traced_artifact_ids: list[str] = Field(default_factory=list)
    temporal_payload_sizes: list[int] = Field(default_factory=list)
    temporal_contains_large_content: bool = False


class LongHorizonEvaluationReport(BaseModel):
    """Machine-readable QA-01 gate result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    checks: dict[str, bool]
    failures: list[str]
    observed: dict[str, int]


class LongHorizonEvaluator:
    """Evaluate exact workload counts and durable consistency invariants."""

    def __init__(self, requirements: LongHorizonRequirements | None = None) -> None:
        self._requirements = requirements or LongHorizonRequirements()

    def evaluate(self, evidence: LongHorizonEvidence) -> LongHorizonEvaluationReport:
        requirements = self._requirements
        tool_ids = set(evidence.tool_call_ids)
        failed_ids = set(evidence.failed_tool_call_ids)
        processed_ids = set(evidence.processed_tool_call_ids)
        execution_ids = list(evidence.execution_by_tool_call.values())
        artifact_ids = set(evidence.artifact_ids)
        traced_artifact_ids = set(evidence.traced_artifact_ids)
        required_boundaries = set(RecoveryBoundary)
        observed_boundaries = set(evidence.recovery_boundaries)

        checks = {
            "tool_call_count": len(evidence.tool_call_ids) == requirements.tool_calls,
            "unique_tool_calls": len(tool_ids) == len(evidence.tool_call_ids),
            "tool_failure_count": len(evidence.failed_tool_call_ids)
            == requirements.tool_failures,
            "unique_tool_failures": len(failed_ids) == len(evidence.failed_tool_call_ids),
            "tool_failures_belong_to_run": failed_ids <= tool_ids,
            "all_results_processed": (
                processed_ids == tool_ids
                and len(processed_ids) == len(evidence.processed_tool_call_ids)
            ),
            "one_execution_per_tool_call": (
                set(evidence.execution_by_tool_call) == tool_ids
                and len(execution_ids) == len(set(execution_ids))
            ),
            "no_duplicate_execution_launch": (
                set(evidence.execution_launch_counts) == tool_ids
                and all(count == 1 for count in evidence.execution_launch_counts.values())
            ),
            "user_supplement_count": len(evidence.user_message_ids)
            == requirements.user_supplements,
            "approval_count": len(evidence.approval_ids) == requirements.approvals,
            "subagent_count": len(evidence.subagent_session_ids) == requirements.subagents,
            "compaction_count": len(evidence.compaction_checkpoint_ids)
            == requirements.compactions,
            "model_switch_count": len(evidence.model_switch_checkpoint_ids)
            == requirements.model_switches,
            "worker_restart_count": evidence.worker_restart_count
            == requirements.worker_restarts,
            "runner_restart_count": evidence.runner_restart_count
            == requirements.runner_restarts,
            "web_source_count": len(evidence.web_source_ids) == requirements.web_sources,
            "browser_takeover_count": len(evidence.browser_takeover_ids)
            == requirements.browser_takeovers,
            "unique_scenario_ids": all(
                len(values) == len(set(values))
                for values in (
                    evidence.user_message_ids,
                    evidence.approval_ids,
                    evidence.subagent_session_ids,
                    evidence.compaction_checkpoint_ids,
                    evidence.model_switch_checkpoint_ids,
                    evidence.web_source_ids,
                    evidence.browser_takeover_ids,
                    evidence.artifact_ids,
                )
            ),
            "all_recovery_boundaries": (
                observed_boundaries == required_boundaries
                and len(observed_boundaries) == len(evidence.recovery_boundaries)
            ),
            "objective_preserved": evidence.objective_before == evidence.objective_after,
            "scope_preserved": evidence.scope_digest_before == evidence.scope_digest_after,
            "working_memory_preserved": (
                evidence.working_memory_digest_before_restart
                == evidence.working_memory_digest_after_restart
            ),
            "artifacts_traceable": bool(artifact_ids) and artifact_ids <= traced_artifact_ids,
            "temporal_payloads_bounded": (
                bool(evidence.temporal_payload_sizes)
                and max(evidence.temporal_payload_sizes)
                <= requirements.max_temporal_payload_bytes
                and not evidence.temporal_contains_large_content
            ),
        }
        failures = [name for name, passed in checks.items() if not passed]
        observed = {
            "tool_calls": len(evidence.tool_call_ids),
            "tool_failures": len(evidence.failed_tool_call_ids),
            "user_supplements": len(evidence.user_message_ids),
            "approvals": len(evidence.approval_ids),
            "subagents": len(evidence.subagent_session_ids),
            "compactions": len(evidence.compaction_checkpoint_ids),
            "model_switches": len(evidence.model_switch_checkpoint_ids),
            "worker_restarts": evidence.worker_restart_count,
            "runner_restarts": evidence.runner_restart_count,
            "web_sources": len(evidence.web_source_ids),
            "browser_takeovers": len(evidence.browser_takeover_ids),
            "recovery_boundaries": len(observed_boundaries),
            "artifacts": len(evidence.artifact_ids),
            "max_temporal_payload_bytes": max(evidence.temporal_payload_sizes, default=0),
        }
        return LongHorizonEvaluationReport(
            passed=not failures,
            checks=checks,
            failures=failures,
            observed=observed,
        )
