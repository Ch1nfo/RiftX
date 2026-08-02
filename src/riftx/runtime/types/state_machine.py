"""Canonical state transitions for durable Agent Runtime entities."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import AwareDatetime

from riftx.domain import InvalidStateTransitionError, Run, RunStatus
from riftx.domain.base import utc_now

from .enums import CycleStatus, SessionStatus, StepStatus, YieldReason
from .models import AgentCycle, AgentSession, AgentStep

_SESSION_TRANSITIONS: Mapping[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.CREATED: frozenset(
        {SessionStatus.ACTIVE, SessionStatus.FAILED, SessionStatus.CANCELLED}
    ),
    SessionStatus.ACTIVE: frozenset(
        {
            SessionStatus.SUSPENDED,
            SessionStatus.COMPACTING,
            SessionStatus.WAITING_APPROVAL,
            SessionStatus.WAITING_USER,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }
    ),
    SessionStatus.SUSPENDED: frozenset(
        {SessionStatus.ACTIVE, SessionStatus.FAILED, SessionStatus.CANCELLED}
    ),
    SessionStatus.COMPACTING: frozenset(
        {SessionStatus.ACTIVE, SessionStatus.FAILED, SessionStatus.CANCELLED}
    ),
    SessionStatus.WAITING_APPROVAL: frozenset(
        {SessionStatus.ACTIVE, SessionStatus.FAILED, SessionStatus.CANCELLED}
    ),
    SessionStatus.WAITING_USER: frozenset(
        {SessionStatus.ACTIVE, SessionStatus.FAILED, SessionStatus.CANCELLED}
    ),
    SessionStatus.COMPLETED: frozenset(),
    SessionStatus.FAILED: frozenset(),
    SessionStatus.CANCELLED: frozenset(),
}

_CYCLE_TRANSITIONS: Mapping[CycleStatus, frozenset[CycleStatus]] = {
    CycleStatus.CREATED: frozenset({CycleStatus.RUNNING, CycleStatus.CANCELLED}),
    CycleStatus.RUNNING: frozenset(
        {
            CycleStatus.YIELDED,
            CycleStatus.COMPLETED,
            CycleStatus.FAILED,
            CycleStatus.CANCELLED,
        }
    ),
    CycleStatus.YIELDED: frozenset(),
    CycleStatus.COMPLETED: frozenset(),
    CycleStatus.FAILED: frozenset(),
    CycleStatus.CANCELLED: frozenset(),
}

_STEP_TRANSITIONS: Mapping[StepStatus, frozenset[StepStatus]] = {
    StepStatus.CREATED: frozenset({StepStatus.RUNNING, StepStatus.CANCELLED}),
    StepStatus.RUNNING: frozenset({StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.CANCELLED}),
    StepStatus.COMPLETED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
}

_TERMINAL_SESSION_STATUSES = {
    SessionStatus.COMPLETED,
    SessionStatus.FAILED,
    SessionStatus.CANCELLED,
}
_TERMINAL_CYCLE_STATUSES = {
    CycleStatus.YIELDED,
    CycleStatus.COMPLETED,
    CycleStatus.FAILED,
    CycleStatus.CANCELLED,
}
_TERMINAL_STEP_STATUSES = {
    StepStatus.COMPLETED,
    StepStatus.FAILED,
    StepStatus.CANCELLED,
}


class RuntimeStateMachine:
    """Apply only legal transitions and maintain lifecycle timestamps."""

    def transition_run(
        self,
        run: Run,
        target: RunStatus,
        *,
        at: AwareDatetime | None = None,
    ) -> Run:
        run.transition_to(target, at=at)
        return run

    def transition_session(
        self,
        session: AgentSession,
        target: SessionStatus,
        *,
        at: AwareDatetime | None = None,
    ) -> AgentSession:
        self._require_transition("AgentSession", session.status, target, _SESSION_TRANSITIONS)
        session.status = target
        if target in _TERMINAL_SESSION_STATUSES:
            session.closed_at = at or utc_now()
        return session

    def transition_cycle(
        self,
        cycle: AgentCycle,
        target: CycleStatus,
        *,
        yield_reason: YieldReason | None = None,
        at: AwareDatetime | None = None,
    ) -> AgentCycle:
        self._require_transition("AgentCycle", cycle.status, target, _CYCLE_TRANSITIONS)
        changed_at = at or utc_now()
        if target is CycleStatus.YIELDED and yield_reason is None:
            raise ValueError("yield_reason is required when yielding an agent cycle")
        if target is not CycleStatus.YIELDED and yield_reason is not None:
            raise ValueError("yield_reason is only valid for a yielded agent cycle")
        cycle.status = target
        if target is CycleStatus.RUNNING and cycle.started_at is None:
            cycle.started_at = changed_at
        if target in _TERMINAL_CYCLE_STATUSES:
            cycle.finished_at = changed_at
        cycle.yield_reason = yield_reason
        return cycle

    def transition_step(
        self,
        step: AgentStep,
        target: StepStatus,
        *,
        at: AwareDatetime | None = None,
    ) -> AgentStep:
        self._require_transition("AgentStep", step.status, target, _STEP_TRANSITIONS)
        changed_at = at or utc_now()
        step.status = target
        if target is StepStatus.RUNNING and step.started_at is None:
            step.started_at = changed_at
        if target in _TERMINAL_STEP_STATUSES:
            step.finished_at = changed_at
        return step

    @staticmethod
    def _require_transition(
        entity: str,
        current: object,
        target: object,
        transitions: Mapping[object, frozenset[object]],
    ) -> None:
        if target not in transitions[current]:
            raise InvalidStateTransitionError(entity, current, target)
