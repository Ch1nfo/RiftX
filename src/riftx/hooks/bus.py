"""Timeout-bounded Hook dispatch with deterministic conflict resolution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from .models import (
    HookAuditRecord,
    HookDecision,
    HookDispatchResult,
    HookFailurePolicy,
    HookPoint,
    HookRequest,
    HookResult,
)

HookHandler = Callable[[HookRequest], Awaitable[HookResult]]

_DECISION_PRIORITY = {
    HookDecision.ABSTAIN: 0,
    HookDecision.CONTINUE: 1,
    HookDecision.MODIFY: 2,
    HookDecision.REQUIRE_APPROVAL: 3,
    HookDecision.BLOCK: 4,
}


class HookAuditSink(Protocol):
    async def record(self, audit: HookAuditRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class HookRegistration:
    hook_id: str
    point: HookPoint
    handler: HookHandler
    priority: int = 0
    timeout_seconds: float = 10.0
    failure_policy: HookFailurePolicy = HookFailurePolicy.WARN

    def __post_init__(self) -> None:
        if not self.hook_id.strip():
            raise ValueError("hook_id must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("Hook timeout must be positive")


class HookBus:
    def __init__(self, *, audit_sink: HookAuditSink | None = None) -> None:
        self._registrations: list[HookRegistration] = []
        self._audit_sink = audit_sink

    def register(self, registration: HookRegistration) -> None:
        if any(item.hook_id == registration.hook_id for item in self._registrations):
            raise ValueError(f"Hook {registration.hook_id!r} is already registered")
        self._registrations.append(registration)

    async def dispatch(self, request: HookRequest) -> HookDispatchResult:
        payload = dict(request.payload)
        decisions: list[HookDecision] = []
        contexts: list[str] = []
        emitted_events: list[dict[str, object]] = []
        audits: list[HookAuditRecord] = []
        modifications: dict[str, tuple[int, object]] = {}
        registrations = sorted(
            (item for item in self._registrations if item.point is request.point),
            key=lambda item: (-item.priority, item.hook_id),
        )
        for registration in registrations:
            invocation = request.model_copy(update={"payload": dict(payload)})
            result, audit = await self._invoke(registration, invocation)
            audits.append(audit)
            await self._record(audit)
            decisions.append(result.decision)
            if result.additional_context:
                contexts.append(result.additional_context)
            emitted_events.extend(result.emitted_events)
            if result.decision is not HookDecision.MODIFY:
                continue
            for field, value in (result.modified_payload or {}).items():
                previous = modifications.get(field)
                if previous is not None and previous[0] == registration.priority:
                    if previous[1] != value:
                        decisions.append(HookDecision.BLOCK)
                        emitted_events.append(
                            {
                                "event_type": "hook.configuration_conflict",
                                "field": field,
                                "priority": registration.priority,
                            }
                        )
                    continue
                if previous is None:
                    modifications[field] = (registration.priority, value)
                    payload[field] = value
        decision = max(
            decisions or [HookDecision.CONTINUE],
            key=_DECISION_PRIORITY.__getitem__,
        )
        return HookDispatchResult(
            decision=decision,
            payload=payload,
            additional_context=contexts,
            emitted_events=emitted_events,
            audits=audits,
        )

    async def _invoke(
        self,
        registration: HookRegistration,
        request: HookRequest,
    ) -> tuple[HookResult, HookAuditRecord]:
        started = monotonic()
        error: str | None = None
        try:
            result = await asyncio.wait_for(
                registration.handler(request),
                timeout=registration.timeout_seconds,
            )
        except TimeoutError:
            error = f"Hook timed out after {registration.timeout_seconds:g} seconds"
            result = _failure_result(registration, error)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            result = _failure_result(registration, error)
        duration_ms = max(0.0, (monotonic() - started) * 1000)
        modified_fields = sorted((result.modified_payload or {}).keys())
        audit = HookAuditRecord(
            request_id=request.id,
            hook_id=registration.hook_id,
            point=request.point,
            run_id=request.run_id,
            decision=result.decision,
            priority=registration.priority,
            duration_ms=duration_ms,
            input_digest=_digest(request.payload),
            output_digest=_digest(result.model_dump(mode="json")),
            modified_fields=modified_fields,
            reason=result.reason,
            error=error,
        )
        return result, audit

    async def _record(self, audit: HookAuditRecord) -> None:
        if self._audit_sink is not None:
            await self._audit_sink.record(audit)


def _failure_result(registration: HookRegistration, error: str) -> HookResult:
    return HookResult(
        decision=(
            HookDecision.BLOCK
            if registration.failure_policy is HookFailurePolicy.BLOCK
            else HookDecision.ABSTAIN
        ),
        reason=error,
    )


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
