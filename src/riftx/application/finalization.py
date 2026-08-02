"""Durable Run finalization intent shared by cleanup owners."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from riftx.domain import RunEvent, RunStatus

FINALIZATION_INTENT_EVENT_TYPE = "run.finalization_intent"
FINALIZATION_TARGETS = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})
REPORT_GENERATION_FAILED_EVENT_TYPE = "report.generation_failed"


@dataclass(frozen=True, slots=True)
class RunFinalizationIntent:
    target: RunStatus
    defer_cleanup_event: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "target_status": self.target.value,
            "defer_cleanup_event": self.defer_cleanup_event,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> RunFinalizationIntent:
        if payload.get("version") != 1:
            raise ValueError("unsupported Run finalization intent version")
        raw_target = payload.get("target_status")
        try:
            target = RunStatus(raw_target)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Run finalization target") from exc
        if target not in FINALIZATION_TARGETS:
            raise ValueError(f"unsupported Run finalization target {target.value!r}")
        defer_cleanup_event = payload.get("defer_cleanup_event", False)
        if not isinstance(defer_cleanup_event, bool):
            raise ValueError("invalid defer_cleanup_event value")
        return cls(target=target, defer_cleanup_event=defer_cleanup_event)


def resolve_finalization_intent(events: list[RunEvent]) -> RunFinalizationIntent | None:
    """Resolve an immutable target while conservatively preserving report order."""

    intents = [
        RunFinalizationIntent.from_payload(event.payload)
        for event in events
        if event.event_type == FINALIZATION_INTENT_EVENT_TYPE
    ]
    if not intents:
        return None
    targets = {intent.target for intent in intents}
    if len(targets) != 1:
        raise ValueError(f"conflicting Run finalization targets: {sorted(targets)!r}")
    return RunFinalizationIntent(
        target=intents[0].target,
        # Once report-aware cleanup has requested deferral, an owner
        # reconciler must never publish run.cleaned_up ahead of that report.
        defer_cleanup_event=any(intent.defer_cleanup_event for intent in intents),
    )


def cleanup_event_id(run_id: str, target: RunStatus) -> str:
    """Return a bounded deterministic ID for cross-owner cleanup idempotency."""

    return str(uuid5(NAMESPACE_URL, f"riftx:{run_id}:cleaned:{target.value}"))


def cleanup_event_payload(target: RunStatus) -> dict[str, object]:
    """Return the canonical terminal-cleanup fact shared by every owner.

    Owner-specific diagnostics belong in ``run.cleanup_reconciled`` or
    ``run.cleanup_stop_confirmed``. A canonical payload lets persistence
    require exact equality whenever a deterministic event ID is reused.
    """

    return {
        "version": 1,
        "status": target.value,
        "stop_confirmed": True,
    }


def report_failure_event_id(run_id: str) -> str:
    """Return a stable ID so Activity retries record one report failure."""

    return str(uuid5(NAMESPACE_URL, f"riftx:{run_id}:report:generation-failed"))


def report_failure_event_payload() -> dict[str, object]:
    """Return a non-sensitive durable fact for exhausted report generation."""

    return {
        "version": 1,
        "stage": "generate_report_activity",
        "outcome": "failed",
    }
