"""Pure deterministic checks over authoritative Runtime snapshots."""

from __future__ import annotations

import json

from riftx.context import AttemptRecord, AttemptStatus
from riftx.reasoning import ReasoningNodeKind, ReasoningNodeStatus
from riftx.runtime.types import ToolCallIntent, ToolCallStatus, YieldReason
from riftx.tasks import TaskStatus

from .models import (
    SupervisorCheck,
    SupervisorDisposition,
    SupervisorReport,
    SupervisorSeverity,
    SupervisorSignal,
    SupervisorSnapshot,
    attempt_key,
)

_SCOPE_ERROR_MARKERS = (
    "scope",
    "_run_mismatch",
    "_session_mismatch",
    "_task_mismatch",
    "_owner_mismatch",
)
_TERMINAL_TOOL_STATUSES = {
    ToolCallStatus.COMPLETED,
    ToolCallStatus.REJECTED,
    ToolCallStatus.FAILED,
    ToolCallStatus.CANCELLED,
}


class ObserverSupervisor:
    def __init__(self, *, loop_threshold: int = 3) -> None:
        if loop_threshold < 2:
            raise ValueError("loop threshold must be at least two")
        self._loop_threshold = loop_threshold

    def inspect(self, snapshot: SupervisorSnapshot) -> SupervisorReport:
        signals = [
            *self._scope_signals(snapshot),
            *self._approval_signals(snapshot),
            *self._attempt_signals(snapshot),
            *self._evidence_signals(snapshot),
            *self._capability_signals(snapshot),
            *self._budget_signals(snapshot),
            *self._loop_signals(snapshot),
            *self._human_control_signals(snapshot),
        ]
        blocking = any(signal.severity is SupervisorSeverity.BLOCKING for signal in signals)
        yield_reason = next(
            (signal.yield_reason for signal in signals if signal.yield_reason is not None),
            None,
        )
        return SupervisorReport(
            run_id=snapshot.run_id,
            session_id=snapshot.session.id,
            cycle_id=snapshot.cycle.id,
            disposition=(
                SupervisorDisposition.BLOCK
                if blocking
                else SupervisorDisposition.YIELD
                if yield_reason is not None
                else SupervisorDisposition.CONTINUE
            ),
            yield_reason=None if blocking else yield_reason,
            signals=tuple(signals),
        )

    @staticmethod
    def _scope_signals(snapshot: SupervisorSnapshot) -> list[SupervisorSignal]:
        signals: list[SupervisorSignal] = []
        for event in snapshot.recent_events:
            error_code = event.payload.get("error_code")
            if not isinstance(error_code, str) or not any(
                marker in error_code for marker in _SCOPE_ERROR_MARKERS
            ):
                continue
            signals.append(
                _signal(
                    "scope_boundary_rejected",
                    SupervisorCheck.SCOPE,
                    SupervisorSeverity.BLOCKING,
                    "A recent Runtime action crossed a Run, Session, Task, or target "
                    "scope boundary",
                    refs=(f"event:{event.id}",),
                )
            )
        return signals

    @staticmethod
    def _approval_signals(snapshot: SupervisorSnapshot) -> list[SupervisorSignal]:
        pending_by_intent = {
            approval.tool_call_intent_id: approval for approval in snapshot.pending_approvals
        }
        waiting = [
            intent
            for intent in snapshot.recent_tool_intents
            if intent.status is ToolCallStatus.WAITING_APPROVAL
        ]
        missing = [intent for intent in waiting if intent.id not in pending_by_intent]
        signals = [
            _signal(
                "approval_record_missing",
                SupervisorCheck.APPROVAL,
                SupervisorSeverity.BLOCKING,
                "A Tool Intent is waiting for approval without a durable Approval Request",
                refs=tuple(f"tool-intent:{intent.id}" for intent in missing),
            )
        ] if missing else []
        if snapshot.pending_approvals:
            signals.append(
                _signal(
                    "approval_pending",
                    SupervisorCheck.APPROVAL,
                    SupervisorSeverity.INFO,
                    "The Runtime must wait for a durable operator approval decision",
                    refs=tuple(
                        f"approval:{approval.id}" for approval in snapshot.pending_approvals
                    ),
                    yield_reason=YieldReason.APPROVAL_REQUIRED,
                )
            )
        return signals

    @staticmethod
    def _attempt_signals(snapshot: SupervisorSnapshot) -> list[SupervisorSignal]:
        if snapshot.working_memory is None:
            return []
        by_id = {attempt.id: attempt for attempt in snapshot.working_memory.attempts}
        failed_by_key: dict[tuple[str, str, str, str], AttemptRecord] = {}
        invalid_refs: list[str] = []
        for attempt in snapshot.working_memory.attempts:
            key = attempt_key(attempt)
            failed = failed_by_key.get(key)
            if failed is not None:
                previous = by_id.get(attempt.retry_of_attempt_id or "")
                if (
                    previous is not failed
                    or not previous.retryable
                    or not attempt.retry_reason
                ):
                    invalid_refs.append(f"attempt:{attempt.id}")
            if attempt.result_status is AttemptStatus.FAILED:
                failed_by_key[key] = attempt
        return [
            _signal(
                "duplicate_attempt_retry_invalid",
                SupervisorCheck.DUPLICATE_ATTEMPT,
                SupervisorSeverity.BLOCKING,
                "A repeated failed operation lacks valid Retry lineage",
                refs=tuple(invalid_refs),
            )
        ] if invalid_refs else []

    @staticmethod
    def _evidence_signals(snapshot: SupervisorSnapshot) -> list[SupervisorSignal]:
        missing: list[str] = []
        if snapshot.task_graph is not None:
            completed = {
                task.id for task in snapshot.task_graph.tasks if task.status is TaskStatus.COMPLETED
            }
            missing.extend(
                f"task-evidence:{requirement.id}"
                for requirement in snapshot.task_graph.evidence_requirements
                if requirement.task_id in completed and not requirement.satisfied
            )
        if snapshot.reasoning_graph is not None:
            missing.extend(
                f"reasoning-node:{node.id}"
                for node in snapshot.reasoning_graph.nodes
                if (
                    node.kind is not ReasoningNodeKind.HYPOTHESIS
                    and not node.evidence_ids
                )
                or (
                    node.kind is ReasoningNodeKind.FINDING
                    and node.status is ReasoningNodeStatus.CONFIRMED
                    and node.reproduction_contract is None
                )
            )
        return [
            _signal(
                "required_evidence_missing",
                SupervisorCheck.EVIDENCE,
                SupervisorSeverity.BLOCKING,
                "Completed or confirmed cognitive state is missing required Evidence",
                refs=tuple(missing),
            )
        ] if missing else []

    @staticmethod
    def _capability_signals(snapshot: SupervisorSnapshot) -> list[SupervisorSignal]:
        tools = set(snapshot.available_tool_ids)
        skills = set(snapshot.available_skill_ids)
        mismatched = [
            intent
            for intent in snapshot.recent_tool_intents
            if intent.status not in _TERMINAL_TOOL_STATUSES
            and (
                (intent.tool_id is not None and intent.tool_id not in tools)
                or (intent.skill_id is not None and intent.skill_id not in skills)
            )
        ]
        return [
            _signal(
                "tool_capability_mismatch",
                SupervisorCheck.CAPABILITY,
                SupervisorSeverity.BLOCKING,
                "An active Tool Intent is absent from the compiled capability manifest",
                refs=tuple(f"tool-intent:{intent.id}" for intent in mismatched),
            )
        ] if mismatched else []

    @staticmethod
    def _budget_signals(snapshot: SupervisorSnapshot) -> list[SupervisorSignal]:
        exceeded: list[str] = []
        if snapshot.cycle.model_call_count >= snapshot.limits.max_model_calls:
            exceeded.append("budget:model_calls")
        if snapshot.cycle.tool_call_count >= snapshot.limits.max_tool_calls:
            exceeded.append("budget:tool_calls")
        if snapshot.elapsed_seconds >= snapshot.limits.max_duration_seconds:
            exceeded.append("budget:duration")
        return [
            _signal(
                "cycle_budget_reached",
                SupervisorCheck.BUDGET,
                SupervisorSeverity.WARNING,
                "The current Agent Cycle reached a configured execution budget",
                refs=tuple(exceeded),
                yield_reason=YieldReason.CYCLE_LIMIT_REACHED,
            )
        ] if exceeded else []

    def _loop_signals(self, snapshot: SupervisorSnapshot) -> list[SupervisorSignal]:
        if len(snapshot.recent_tool_intents) < self._loop_threshold:
            return []
        tail = snapshot.recent_tool_intents[-self._loop_threshold :]
        fingerprints = {_intent_fingerprint(intent) for intent in tail}
        if len(fingerprints) != 1:
            return []
        return [
            _signal(
                "repeated_tool_call_loop",
                SupervisorCheck.LOOP,
                SupervisorSeverity.WARNING,
                "The same Tool Call was proposed repeatedly without observable progress",
                refs=tuple(f"tool-intent:{intent.id}" for intent in tail),
                yield_reason=YieldReason.CYCLE_LIMIT_REACHED,
            )
        ]

    @staticmethod
    def _human_control_signals(snapshot: SupervisorSnapshot) -> list[SupervisorSignal]:
        signals: list[SupervisorSignal] = []
        if snapshot.pending_user_input is not None:
            signals.append(
                _signal(
                    "user_input_pending",
                    SupervisorCheck.HUMAN_CONTROL,
                    SupervisorSeverity.INFO,
                    "The Runtime must wait for requested user input",
                    refs=(f"user-input:{snapshot.pending_user_input.id}",),
                    yield_reason=YieldReason.USER_INPUT_REQUIRED,
                )
            )
        if snapshot.active_takeover_refs:
            signals.append(
                _signal(
                    "human_takeover_active",
                    SupervisorCheck.HUMAN_CONTROL,
                    SupervisorSeverity.INFO,
                    "An operator currently owns an interactive Runtime resource",
                    refs=snapshot.active_takeover_refs,
                    yield_reason=YieldReason.RUN_PAUSED,
                )
            )
        return signals


def _intent_fingerprint(intent: ToolCallIntent) -> str:
    return json.dumps(
        [intent.tool_id or intent.skill_id, intent.arguments],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _signal(
    code: str,
    check: SupervisorCheck,
    severity: SupervisorSeverity,
    summary: str,
    *,
    refs: tuple[str, ...],
    yield_reason: YieldReason | None = None,
) -> SupervisorSignal:
    return SupervisorSignal(
        code=code,
        check=check,
        severity=severity,
        summary=summary,
        refs=refs,
        yield_reason=yield_reason,
    )
