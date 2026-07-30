from datetime import UTC, datetime

import pytest

from riftx.domain import InvalidStateTransitionError, Objective, Run, RunStatus
from riftx.domain.run import _RUN_TRANSITIONS as RUN_TRANSITIONS
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    CycleStatus,
    RuntimeStateMachine,
    SessionStatus,
    StepStatus,
    YieldReason,
)
from riftx.runtime.types.state_machine import (
    _CYCLE_TRANSITIONS as CYCLE_TRANSITIONS,
)
from riftx.runtime.types.state_machine import (
    _SESSION_TRANSITIONS as SESSION_TRANSITIONS,
)
from riftx.runtime.types.state_machine import _STEP_TRANSITIONS as STEP_TRANSITIONS


def make_run(status: RunStatus) -> Run:
    return Run(
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Map the authorized target"),
        workspace_path="/tmp/riftx/run-1",
        status=status,
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [(source, target) for source, targets in RUN_TRANSITIONS.items() for target in targets],
)
def test_all_canonical_run_transitions_are_legal(source: RunStatus, target: RunStatus) -> None:
    run = make_run(source)
    RuntimeStateMachine().transition_run(run, target)
    assert run.status is target


@pytest.mark.parametrize(
    ("source", "target"),
    [(source, target) for source, targets in SESSION_TRANSITIONS.items() for target in targets],
)
def test_all_session_transitions_are_legal(source: SessionStatus, target: SessionStatus) -> None:
    session = AgentSession(run_id="run-1", model_profile="default", status=source)
    RuntimeStateMachine().transition_session(session, target)
    assert session.status is target


@pytest.mark.parametrize(
    ("source", "target"),
    [(source, target) for source, targets in CYCLE_TRANSITIONS.items() for target in targets],
)
def test_all_cycle_transitions_are_legal(source: CycleStatus, target: CycleStatus) -> None:
    cycle = AgentCycle(run_id="run-1", session_id="session-1", sequence=1, status=source)
    kwargs = {"yield_reason": YieldReason.TOOL_RUNNING} if target is CycleStatus.YIELDED else {}
    RuntimeStateMachine().transition_cycle(cycle, target, **kwargs)
    assert cycle.status is target


@pytest.mark.parametrize(
    ("source", "target"),
    [(source, target) for source, targets in STEP_TRANSITIONS.items() for target in targets],
)
def test_all_step_transitions_are_legal(source: StepStatus, target: StepStatus) -> None:
    step = AgentStep(
        cycle_id="cycle-1",
        sequence=1,
        step_type=AgentStepType.MODEL_CALL,
        status=source,
    )
    RuntimeStateMachine().transition_step(step, target)
    assert step.status is target


@pytest.mark.parametrize(
    ("entity", "target", "method"),
    [
        (make_run(RunStatus.COMPLETED), RunStatus.RUNNING, "transition_run"),
        (
            AgentSession(
                run_id="run-1",
                model_profile="default",
                status=SessionStatus.COMPLETED,
            ),
            SessionStatus.ACTIVE,
            "transition_session",
        ),
        (
            AgentCycle(
                run_id="run-1",
                session_id="session-1",
                sequence=1,
                status=CycleStatus.COMPLETED,
            ),
            CycleStatus.RUNNING,
            "transition_cycle",
        ),
        (
            AgentStep(
                cycle_id="cycle-1",
                sequence=1,
                step_type=AgentStepType.MODEL_CALL,
                status=StepStatus.COMPLETED,
            ),
            StepStatus.RUNNING,
            "transition_step",
        ),
    ],
)
def test_terminal_states_reject_regression(entity: object, target: object, method: str) -> None:
    with pytest.raises(InvalidStateTransitionError):
        getattr(RuntimeStateMachine(), method)(entity, target)


def test_cycle_yield_requires_reason_and_tracks_timestamps() -> None:
    machine = RuntimeStateMachine()
    cycle = AgentCycle(run_id="run-1", session_id="session-1", sequence=1)
    started = datetime(2026, 7, 30, 1, tzinfo=UTC)
    finished = datetime(2026, 7, 30, 2, tzinfo=UTC)

    machine.transition_cycle(cycle, CycleStatus.RUNNING, at=started)
    with pytest.raises(ValueError, match="yield_reason is required"):
        machine.transition_cycle(cycle, CycleStatus.YIELDED)

    machine.transition_cycle(
        cycle,
        CycleStatus.YIELDED,
        yield_reason=YieldReason.CYCLE_LIMIT_REACHED,
        at=finished,
    )
    assert cycle.started_at == started
    assert cycle.finished_at == finished
    assert cycle.yield_reason is YieldReason.CYCLE_LIMIT_REACHED
