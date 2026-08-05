"""Deterministic judge and run assembler for security capability evaluation."""

from __future__ import annotations

from .loader import LoadedScenario
from .models import (
    EvaluationComparison,
    EvaluationJudgement,
    EvaluationRun,
    EvaluationRunContext,
    EvaluationSubmission,
    FindingDisposition,
    JudgedFinding,
    MemoryShareMode,
)


class EvaluationAdmissionError(ValueError):
    """Raised before a run is admitted with invalid scope, memory, or budget."""


class SecurityEvaluationJudge:
    """Classify fixture findings without model calls or external state."""

    version = "riftx.security-evaluation-judge/v1"

    def judge(
        self,
        loaded: LoadedScenario,
        submission: EvaluationSubmission,
    ) -> EvaluationJudgement:
        expected_by_key = {
            finding.finding_key: finding for finding in loaded.scenario.expected_findings
        }
        observed_by_key = {
            observation.finding_key: observation for observation in submission.observations
        }
        judged: list[JudgedFinding] = []

        for finding_key in sorted(expected_by_key):
            expected = expected_by_key[finding_key]
            observation = observed_by_key.pop(finding_key, None)
            if observation is None:
                judged.append(
                    JudgedFinding(
                        finding_key=finding_key,
                        expected=True,
                        declared_disposition=None,
                        final_disposition=FindingDisposition.NOT_FOUND,
                        evidence_contract_met=False,
                        detail="expected finding was not present in the submission",
                    )
                )
                continue

            evidence_kinds = {item.kind for item in observation.evidence}
            evidence_contract_met = set(expected.required_evidence) <= evidence_kinds
            final_disposition = observation.disposition
            detail = "submission disposition retained"
            if (
                observation.disposition is FindingDisposition.VERIFIED
                and not evidence_contract_met
            ):
                final_disposition = FindingDisposition.SUSPECTED
                detail = "verified claim was downgraded because required evidence is missing"
            judged.append(
                JudgedFinding(
                    finding_key=finding_key,
                    expected=True,
                    declared_disposition=observation.disposition,
                    final_disposition=final_disposition,
                    evidence_contract_met=evidence_contract_met,
                    detail=detail,
                )
            )

        for finding_key in sorted(observed_by_key):
            observation = observed_by_key[finding_key]
            judged.append(
                JudgedFinding(
                    finding_key=finding_key,
                    expected=False,
                    declared_disposition=observation.disposition,
                    final_disposition=FindingDisposition.FALSE_POSITIVE,
                    evidence_contract_met=False,
                    detail="submission finding is not present in deterministic ground truth",
                )
            )

        counts = {disposition: 0 for disposition in FindingDisposition}
        for finding in judged:
            counts[finding.final_disposition] += 1
        return EvaluationJudgement(
            judge_version=self.version,
            findings=tuple(judged),
            disposition_counts=counts,
        )


class SecurityEvaluationHarness:
    """Admit one recorded run and produce a canonical evaluation result."""

    def __init__(self, judge: SecurityEvaluationJudge | None = None) -> None:
        self._judge = judge or SecurityEvaluationJudge()

    def assemble_run(
        self,
        loaded: LoadedScenario,
        context: EvaluationRunContext,
        submission: EvaluationSubmission,
    ) -> EvaluationRun:
        self._validate_memory(loaded, context)
        self._validate_budget(loaded, context)
        judgement = self._judge.judge(loaded, submission)
        return EvaluationRun(
            schema_version="riftx.security-evaluation-run/v1",
            run_id=context.run_id,
            scenario_id=loaded.scenario.scenario_id,
            scenario_version=loaded.scenario.version,
            scenario_digest=loaded.scenario_digest,
            subject=context.subject,
            build=context.build,
            model=context.model,
            runtime=context.runtime,
            capabilities=context.capabilities,
            memory_namespace=(
                f"security-eval:{loaded.scenario.scenario_id}:{context.run_id}"
            ),
            memory_source_ids=context.requested_memory_source_ids,
            started_at=context.started_at,
            completed_at=context.completed_at,
            usage=context.usage,
            trajectory=context.trajectory,
            submission=submission,
            judgement=judgement,
        )

    def compare(
        self,
        baseline: EvaluationRun,
        candidate: EvaluationRun,
    ) -> EvaluationComparison:
        if (
            baseline.scenario_id != candidate.scenario_id
            or baseline.scenario_version != candidate.scenario_version
            or baseline.scenario_digest != candidate.scenario_digest
        ):
            raise EvaluationAdmissionError(
                "evaluation comparison requires the same scenario snapshot"
            )
        return EvaluationComparison(
            baseline_run_id=baseline.run_id,
            candidate_run_id=candidate.run_id,
            disposition_deltas={
                disposition: (
                    candidate.judgement.disposition_counts[disposition]
                    - baseline.judgement.disposition_counts[disposition]
                )
                for disposition in FindingDisposition
            },
            token_delta=candidate.usage.total_tokens - baseline.usage.total_tokens,
            tool_call_delta=candidate.usage.tool_calls - baseline.usage.tool_calls,
            target_interaction_delta=(
                candidate.usage.target_interactions - baseline.usage.target_interactions
            ),
            duration_ms_delta=candidate.usage.duration_ms - baseline.usage.duration_ms,
            notes=(
                "all deltas are candidate minus baseline",
                "comparison is diagnostic and does not assert overall superiority",
            ),
        )

    def _validate_memory(
        self,
        loaded: LoadedScenario,
        context: EvaluationRunContext,
    ) -> None:
        requested = set(context.requested_memory_source_ids)
        policy = loaded.scenario.memory_policy
        if policy.share_mode is MemoryShareMode.ISOLATED and requested:
            raise EvaluationAdmissionError(
                "isolated evaluation runs cannot request shared memory"
            )
        allowed = set(policy.allowed_source_ids)
        if not requested <= allowed:
            raise EvaluationAdmissionError(
                f"evaluation requested unauthorized memory sources: {sorted(requested - allowed)}"
            )

    def _validate_budget(
        self,
        loaded: LoadedScenario,
        context: EvaluationRunContext,
    ) -> None:
        budget = loaded.scenario.budget
        usage = context.usage
        observed_duration_ms = int(
            (context.completed_at - context.started_at).total_seconds() * 1000
        )
        failures: list[str] = []
        if usage.duration_ms != observed_duration_ms:
            failures.append("recorded duration does not match run timestamps")
        if usage.duration_ms > budget.max_duration_seconds * 1000:
            failures.append("duration budget exceeded")
        if usage.total_tokens > budget.max_total_tokens:
            failures.append("token budget exceeded")
        if usage.tool_calls > budget.max_tool_calls:
            failures.append("tool-call budget exceeded")
        if usage.target_interactions > budget.max_target_interactions:
            failures.append("target-interaction budget exceeded")
        trajectory_input = sum(step.input_tokens for step in context.trajectory.steps)
        trajectory_output = sum(step.output_tokens for step in context.trajectory.steps)
        if trajectory_input > usage.input_tokens or trajectory_output > usage.output_tokens:
            failures.append("trajectory token usage exceeds run usage")
        if failures:
            raise EvaluationAdmissionError("; ".join(failures))
