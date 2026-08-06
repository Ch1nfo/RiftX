"""Deterministic Closure verification over existing durable Run state."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import RunRepository
from riftx.domain import SuccessCriterion
from riftx.domain.base import DomainModel
from riftx.evidence import Evidence, EvidenceLedgerRepository
from riftx.reasoning import (
    ReasoningGraphRepository,
    ReasoningNode,
    ReasoningNodeKind,
    ReasoningNodeStatus,
)
from riftx.tasks import (
    Task,
    TaskAttempt,
    TaskEvidenceRequirement,
    TaskGraphRepository,
    TaskStatus,
)

CLOSURE_EVALUATED_EVENT_TYPE = "run.closure_evaluated"
_CLOSURE_DIGEST_DOMAIN = b"riftx.closure-report/v1\0"


class ClosureOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class SuccessCriterionClosure(DomainModel):
    index: int = Field(ge=0)
    description: str
    required: bool
    requirement_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    satisfied: bool
    reason_codes: tuple[str, ...] = ()


class IncompleteTaskClosure(DomainModel):
    task_id: str
    status: TaskStatus
    explanation: str | None = None
    explained: bool
    reason_codes: tuple[str, ...] = ()


class FindingClosure(DomainModel):
    finding_id: str
    evidence_ids: tuple[str, ...]
    replayable: bool
    reason_codes: tuple[str, ...] = ()


class ClosureReport(DomainModel):
    run_id: str
    outcome: ClosureOutcome
    task_graph_version: int | None = Field(default=None, ge=1)
    reasoning_graph_version: int | None = Field(default=None, ge=1)
    success_criteria: tuple[SuccessCriterionClosure, ...] = ()
    incomplete_tasks: tuple[IncompleteTaskClosure, ...] = ()
    confirmed_findings: tuple[FindingClosure, ...] = ()
    reason_codes: tuple[str, ...] = ()


class ClosureVerifierApplicationService:
    """Classify a Run as complete or partial without creating new authority."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        task_graphs: TaskGraphRepository,
        reasoning_graphs: ReasoningGraphRepository,
        evidence: EvidenceLedgerRepository,
    ) -> None:
        self._runs = runs
        self._task_graphs = task_graphs
        self._reasoning_graphs = reasoning_graphs
        self._evidence = evidence

    async def verify(self, run_id: str) -> ClosureReport:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        task_graph = await self._task_graphs.get(run_id)
        reasoning_graph = await self._reasoning_graphs.get(run_id)

        requirement_refs: dict[int, list[str]] = defaultdict(list)
        requirements_by_index: dict[int, list[TaskEvidenceRequirement]] = defaultdict(list)
        if task_graph is not None:
            for requirement in task_graph.evidence_requirements:
                if requirement.success_criterion_index is None:
                    continue
                requirements_by_index[requirement.success_criterion_index].append(requirement)
                requirement_refs[requirement.success_criterion_index].extend(
                    requirement.evidence_refs
                )

        confirmed_findings = (
            [
                node
                for node in reasoning_graph.nodes
                if node.kind is ReasoningNodeKind.FINDING
                and node.status is ReasoningNodeStatus.CONFIRMED
            ]
            if reasoning_graph is not None
            else []
        )
        requested_evidence_ids = {
            evidence_id
            for refs in requirement_refs.values()
            for evidence_id in refs
        } | {
            evidence_id
            for finding in confirmed_findings
            for evidence_id in finding.evidence_ids
        }
        evidence_by_id = (
            {
                item.id: item
                for item in await self._evidence.list_by_ids(
                    run_id,
                    tuple(sorted(requested_evidence_ids)),
                )
            }
            if requested_evidence_ids
            else {}
        )

        criteria, criterion_failures = _verify_criteria(
            run.success_criteria,
            requirements_by_index,
            evidence_by_id,
        )
        incomplete_tasks, task_failures = _verify_incomplete_tasks(
            task_graph.tasks if task_graph is not None else [],
            task_graph.attempts if task_graph is not None else [],
        )
        findings, finding_failures = _verify_findings(
            confirmed_findings,
            evidence_by_id,
        )
        reason_codes = {
            *criterion_failures,
            *task_failures,
            *finding_failures,
        }
        if run.success_criteria and task_graph is None:
            reason_codes.add("task_graph_missing")
        if set(requirements_by_index) - set(range(len(run.success_criteria))):
            reason_codes.add("success_criterion_mapping_out_of_range")
        return ClosureReport(
            run_id=run_id,
            outcome=(
                ClosureOutcome.PARTIAL if reason_codes else ClosureOutcome.COMPLETE
            ),
            task_graph_version=task_graph.version if task_graph is not None else None,
            reasoning_graph_version=(
                reasoning_graph.version if reasoning_graph is not None else None
            ),
            success_criteria=criteria,
            incomplete_tasks=incomplete_tasks,
            confirmed_findings=findings,
            reason_codes=tuple(sorted(reason_codes)),
        )


def closure_report_digest(report: ClosureReport) -> str:
    payload = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(_CLOSURE_DIGEST_DOMAIN + payload).hexdigest()


def closure_event_id(report: ClosureReport) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"riftx:{report.run_id}:closure:{closure_report_digest(report)}",
        )
    )


def closure_event_payload(report: ClosureReport) -> dict[str, object]:
    return {
        "version": 1,
        "outcome": report.outcome.value,
        "reason_codes": list(report.reason_codes),
        "report_digest": closure_report_digest(report),
        "task_graph_version": report.task_graph_version,
        "reasoning_graph_version": report.reasoning_graph_version,
        "success_criterion_count": len(report.success_criteria),
        "satisfied_success_criterion_count": sum(
            criterion.satisfied for criterion in report.success_criteria
        ),
        "incomplete_task_count": len(report.incomplete_tasks),
        "confirmed_finding_count": len(report.confirmed_findings),
        "replayable_confirmed_finding_count": sum(
            finding.replayable for finding in report.confirmed_findings
        ),
    }


def _verify_criteria(
    criteria: list[SuccessCriterion],
    requirements_by_index: dict[int, list[TaskEvidenceRequirement]],
    evidence_by_id: dict[str, Evidence],
) -> tuple[tuple[SuccessCriterionClosure, ...], set[str]]:
    results: list[SuccessCriterionClosure] = []
    failures: set[str] = set()
    for index, criterion in enumerate(criteria):
        requirements = sorted(
            requirements_by_index.get(index, []),
            key=lambda requirement: requirement.id,
        )
        evidence_ids = tuple(
            sorted(
                {
                    evidence_id
                    for requirement in requirements
                    for evidence_id in requirement.evidence_refs
                }
            )
        )
        reason_codes: set[str] = set()
        if not requirements:
            reason_codes.add("success_criterion_unmapped")
        if requirements and any(not requirement.satisfied for requirement in requirements):
            reason_codes.add("success_criterion_evidence_requirement_unsatisfied")
        if any(evidence_id not in evidence_by_id for evidence_id in evidence_ids):
            reason_codes.add("success_criterion_evidence_missing")
        satisfied = not reason_codes
        required = criterion.required
        if required and not satisfied:
            failures.update(reason_codes)
        results.append(
            SuccessCriterionClosure(
                index=index,
                description=criterion.description,
                required=required,
                requirement_ids=tuple(requirement.id for requirement in requirements),
                evidence_ids=evidence_ids,
                satisfied=satisfied,
                reason_codes=tuple(sorted(reason_codes)),
            )
        )
    return tuple(results), failures


def _verify_incomplete_tasks(
    tasks: list[Task],
    attempts: list[TaskAttempt],
) -> tuple[tuple[IncompleteTaskClosure, ...], set[str]]:
    results: list[IncompleteTaskClosure] = []
    failures: set[str] = set()
    attempts_by_task: dict[str, list[TaskAttempt]] = defaultdict(list)
    for attempt in attempts:
        attempts_by_task[attempt.task_id].append(attempt)
    for task in sorted(tasks, key=lambda item: (item.sequence, item.id)):
        if task.status is TaskStatus.COMPLETED:
            continue
        explanation = _task_explanation(task, attempts_by_task[task.id])
        reason_codes: tuple[str, ...] = ()
        if explanation is None:
            code = f"{task.status.value}_task_explanation_missing"
            reason_codes = (code,)
            failures.add(code)
        results.append(
            IncompleteTaskClosure(
                task_id=task.id,
                status=task.status,
                explanation=explanation,
                explained=explanation is not None,
                reason_codes=reason_codes,
            )
        )
    return tuple(results), failures


def _task_explanation(task: Task, attempts: list[TaskAttempt]) -> str | None:
    if task.status is TaskStatus.BLOCKED:
        return task.blocked_reason
    if task.status is TaskStatus.PENDING:
        return task.stop_condition
    if task.status is TaskStatus.FAILED:
        return next(
            (
                attempt.failure_summary
                for attempt in sorted(
                    attempts,
                    key=lambda item: item.sequence,
                    reverse=True,
                )
                if attempt.failure_summary
            ),
            None,
        )
    if task.status is TaskStatus.CANCELLED:
        return next(
            (
                entry.removeprefix("cancelled: ")
                for entry in reversed(task.reopen_history)
                if entry.startswith("cancelled: ")
            ),
            None,
        )
    return None


def _verify_findings(
    findings: list[ReasoningNode],
    evidence_by_id: dict[str, Evidence],
) -> tuple[tuple[FindingClosure, ...], set[str]]:
    results: list[FindingClosure] = []
    failures: set[str] = set()
    for finding in sorted(findings, key=lambda item: item.id):
        reason_codes: set[str] = set()
        missing = [
            evidence_id
            for evidence_id in finding.evidence_ids
            if evidence_id not in evidence_by_id
        ]
        if missing:
            reason_codes.add("confirmed_finding_evidence_missing")
        if any(
            not evidence_by_id[evidence_id].replay.replayable
            for evidence_id in finding.evidence_ids
            if evidence_id in evidence_by_id
        ):
            reason_codes.add("confirmed_finding_evidence_not_replayable")
        failures.update(reason_codes)
        results.append(
            FindingClosure(
                finding_id=finding.id,
                evidence_ids=tuple(sorted(finding.evidence_ids)),
                replayable=not reason_codes,
                reason_codes=tuple(sorted(reason_codes)),
            )
        )
    return tuple(results), failures
