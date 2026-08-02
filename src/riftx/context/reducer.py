"""Deterministic proposal reducer for authoritative Working Memory."""

from __future__ import annotations

import json
from collections.abc import Sequence

from riftx.domain.base import utc_now

from .working_memory import (
    AttemptRecord,
    AttemptStatus,
    ConfirmedFact,
    EvidenceSource,
    FactCandidate,
    FactStatus,
    Hypothesis,
    HypothesisEvidenceEffect,
    HypothesisStatus,
    HypothesisUpdate,
    PlanItem,
    PlanItemStatus,
    PlanUpdateProposal,
    WorkingMemory,
)


class WorkingMemoryReductionError(RuntimeError):
    """Base class for rejected Working Memory proposals."""


class WorkingMemoryVersionConflict(WorkingMemoryReductionError):
    """The proposal was based on a stale Working Memory version."""


class PlanRegressionError(WorkingMemoryReductionError):
    """A completed Plan item was reopened without a reason."""


class DuplicateAttemptError(WorkingMemoryReductionError):
    """A failed action was proposed again without an explicit valid retry."""


class HypothesisEvidenceError(WorkingMemoryReductionError):
    """A Hypothesis update references evidence absent from Working Memory."""


_SOURCE_PRIORITY = {
    EvidenceSource.MODEL_INFERENCE: 1,
    EvidenceSource.USER_DECISION: 2,
    EvidenceSource.DETERMINISTIC_PARSER: 3,
}


class WorkingMemoryReducer:
    """Merge typed proposals without allowing a model to replace authoritative state."""

    def reduce(
        self,
        memory: WorkingMemory,
        *,
        expected_version: int,
        plan_update: PlanUpdateProposal | None = None,
        fact_candidates: Sequence[FactCandidate] = (),
        hypothesis_updates: Sequence[HypothesisUpdate] = (),
        attempts: Sequence[AttemptRecord] = (),
    ) -> WorkingMemory:
        if memory.version != expected_version:
            raise WorkingMemoryVersionConflict(
                f"Working Memory {memory.id!r} version conflict; "
                f"expected {expected_version}, current {memory.version}"
            )

        reduced = memory.model_copy(deep=True)
        if plan_update is not None:
            self._merge_plan(reduced, plan_update)
        for candidate in fact_candidates:
            self._merge_fact(reduced, candidate)
        for update in hypothesis_updates:
            self._merge_hypothesis(reduced, update)
        for attempt in attempts:
            self._merge_attempt(reduced, attempt)

        reduced.version += 1
        reduced.updated_at = utc_now()
        return WorkingMemory.model_validate(reduced.model_dump())

    def _merge_plan(self, memory: WorkingMemory, proposal: PlanUpdateProposal) -> None:
        items = {item.id: item for item in memory.run_plan.items}
        for update in proposal.item_updates:
            current = items.get(update.item_id)
            if current is None:
                if update.task is None:
                    raise WorkingMemoryReductionError(
                        f"new Plan item {update.item_id!r} requires a task"
                    )
                sequence = update.sequence or (
                    max((item.sequence for item in items.values()), default=0) + 1
                )
                items[update.item_id] = PlanItem(
                    id=update.item_id,
                    task=update.task,
                    status=update.status or PlanItemStatus.PENDING,
                    sequence=sequence,
                    completion_summary=update.completion_summary,
                )
                continue

            next_status = update.status or current.status
            if (
                current.status is PlanItemStatus.COMPLETED
                and next_status is not PlanItemStatus.COMPLETED
                and not update.reopen_reason
            ):
                raise PlanRegressionError(
                    f"completed Plan item {current.id!r} cannot regress without a reopen reason"
                )
            items[current.id] = current.model_copy(
                update={
                    "task": update.task or current.task,
                    "status": next_status,
                    "sequence": update.sequence or current.sequence,
                    "completion_summary": (
                        update.completion_summary
                        if update.completion_summary is not None
                        else current.completion_summary
                    ),
                }
            )

        memory.run_plan = memory.run_plan.model_copy(
            update={"items": sorted(items.values(), key=lambda item: (item.sequence, item.id))}
        )
        if proposal.current_focus is not None:
            memory.current_focus = proposal.current_focus
        if proposal.next_action is not None:
            memory.next_action = proposal.next_action

    def _merge_fact(self, memory: WorkingMemory, candidate: FactCandidate) -> None:
        matching = next(
            (
                fact
                for fact in memory.confirmed_facts
                if fact.subject == candidate.subject
                and fact.predicate == candidate.predicate
                and _canonical_value(fact.value) == _canonical_value(candidate.value)
                and fact.status is not FactStatus.SUPERSEDED
            ),
            None,
        )
        if matching is None:
            confidence = candidate.confidence
            if candidate.source_type is EvidenceSource.DETERMINISTIC_PARSER:
                confidence = max(confidence, 0.95)
            matching = ConfirmedFact(
                run_id=memory.run_id,
                subject=candidate.subject,
                predicate=candidate.predicate,
                value=candidate.value,
                natural_language=candidate.natural_language,
                confidence=confidence,
                source_refs=list(candidate.source_refs),
                source_types={ref: candidate.source_type for ref in candidate.source_refs},
                first_observed_at=candidate.observed_at,
                last_confirmed_at=candidate.observed_at,
            )
            memory.confirmed_facts.append(matching)
        else:
            new_refs = [ref for ref in candidate.source_refs if ref not in matching.source_refs]
            if new_refs:
                for source_ref in new_refs:
                    matching.source_refs.append(source_ref)
                    matching.source_types[source_ref] = candidate.source_type
                matching.confidence = _combine_confidence(
                    matching.confidence,
                    candidate.confidence,
                    evidence_count=len(new_refs),
                )
                if candidate.source_type is EvidenceSource.DETERMINISTIC_PARSER:
                    matching.confidence = max(matching.confidence, 0.95)
                matching.last_confirmed_at = candidate.observed_at
                if _fact_priority_from_type(candidate.source_type) >= _fact_priority(matching):
                    matching.natural_language = candidate.natural_language

        self._resolve_fact_conflicts(memory, candidate.subject, candidate.predicate)

    def _resolve_fact_conflicts(
        self,
        memory: WorkingMemory,
        subject: str,
        predicate: str,
    ) -> None:
        facts = [
            fact
            for fact in memory.confirmed_facts
            if fact.subject == subject
            and fact.predicate == predicate
            and fact.status is not FactStatus.SUPERSEDED
        ]
        if not facts:
            return
        priorities = {_canonical_value(fact.value): _fact_priority(fact) for fact in facts}
        top_priority = max(priorities.values())
        winning_values = {
            value for value, priority in priorities.items() if priority == top_priority
        }
        unique_winner = len(winning_values) == 1
        for fact in facts:
            value = _canonical_value(fact.value)
            fact.status = (
                FactStatus.CONFIRMED
                if unique_winner and value in winning_values
                else FactStatus.DISPUTED
            )

    def _merge_hypothesis(self, memory: WorkingMemory, update: HypothesisUpdate) -> None:
        known_fact_ids = {fact.id for fact in memory.confirmed_facts}
        missing = set(update.fact_ids) - known_fact_ids
        if missing:
            raise HypothesisEvidenceError(
                f"Hypothesis update references unknown fact IDs: {sorted(missing)}"
            )

        hypothesis = next(
            (item for item in memory.hypotheses if item.id == update.hypothesis_id),
            None,
        )
        if hypothesis is None:
            if update.statement is None:
                raise WorkingMemoryReductionError(
                    f"new Hypothesis {update.hypothesis_id!r} requires a statement"
                )
            hypothesis = Hypothesis(
                id=update.hypothesis_id,
                statement=update.statement,
                confidence=update.initial_confidence,
                next_validation_action=update.next_validation_action,
            )
            memory.hypotheses.append(hypothesis)
        elif update.statement is not None and update.statement != hypothesis.statement:
            raise WorkingMemoryReductionError(
                f"Hypothesis {hypothesis.id!r} statement cannot be overwritten"
            )

        if update.evidence_effect is HypothesisEvidenceEffect.SUPPORTS:
            new_fact_ids = [
                fact_id
                for fact_id in update.fact_ids
                if fact_id not in hypothesis.supporting_fact_ids
            ]
            hypothesis.supporting_fact_ids.extend(new_fact_ids)
            hypothesis.confidence = min(1.0, hypothesis.confidence + (0.15 * len(new_fact_ids)))
            if hypothesis.confidence >= 0.85:
                hypothesis.status = HypothesisStatus.CONFIRMED
            elif new_fact_ids:
                hypothesis.status = HypothesisStatus.SUPPORTED
        else:
            new_fact_ids = [
                fact_id
                for fact_id in update.fact_ids
                if fact_id not in hypothesis.contradicting_fact_ids
            ]
            hypothesis.contradicting_fact_ids.extend(new_fact_ids)
            hypothesis.confidence = max(0.0, hypothesis.confidence - (0.25 * len(new_fact_ids)))
            if hypothesis.confidence <= 0.2 and new_fact_ids:
                hypothesis.status = HypothesisStatus.REJECTED
            elif new_fact_ids:
                hypothesis.status = HypothesisStatus.INVESTIGATING
        if update.next_validation_action is not None:
            hypothesis.next_validation_action = update.next_validation_action

    def _merge_attempt(self, memory: WorkingMemory, attempt: AttemptRecord) -> None:
        if any(existing.id == attempt.id for existing in memory.attempts):
            raise DuplicateAttemptError(f"Attempt {attempt.id!r} already exists")

        key = _attempt_key(attempt)
        failed = next(
            (
                existing
                for existing in reversed(memory.attempts)
                if _attempt_key(existing) == key and existing.result_status is AttemptStatus.FAILED
            ),
            None,
        )
        if failed is not None:
            valid_retry = (
                failed.retryable
                and attempt.retry_of_attempt_id == failed.id
                and bool(attempt.retry_reason)
            )
            if not valid_retry:
                raise DuplicateAttemptError(
                    f"action {attempt.action_signature!r} already failed in Attempt {failed.id!r}"
                )
        memory.attempts.append(attempt)


def _fact_priority(fact: ConfirmedFact) -> int:
    return max((_SOURCE_PRIORITY[source] for source in fact.source_types.values()), default=0)


def _fact_priority_from_type(source_type: EvidenceSource) -> int:
    return _SOURCE_PRIORITY[source_type]


def _combine_confidence(current: float, incoming: float, *, evidence_count: int) -> float:
    combined = current
    for _ in range(evidence_count):
        combined = 1.0 - ((1.0 - combined) * (1.0 - incoming))
    return min(1.0, combined)


def _canonical_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _attempt_key(attempt: AttemptRecord) -> tuple[str, str, str, str]:
    return (
        attempt.action_signature,
        attempt.target,
        attempt.tool_id,
        json.dumps(
            attempt.normalized_arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
