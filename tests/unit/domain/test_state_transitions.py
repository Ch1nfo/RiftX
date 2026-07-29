from datetime import UTC, datetime

import pytest

from riftx.domain import (
    Approval,
    ApprovalStatus,
    Execution,
    ExecutionStatus,
    ExecutorType,
    InvalidStateTransitionError,
    Objective,
    Run,
    RunStatus,
    TerminalSession,
    TerminalStatus,
)


def make_run() -> Run:
    return Run(
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Map the authorized target"),
        workspace_path="/tmp/riftx/run-1",
    )


def make_execution() -> Execution:
    return Execution(
        execution_key="run-1:step-1:tool-1",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        argv=["printf", "ok"],
        cwd="/tmp/riftx/run-1",
        stdout_path="/tmp/riftx/run-1/stdout.log",
        stderr_path="/tmp/riftx/run-1/stderr.log",
    )


def test_run_tracks_start_and_finish_timestamps() -> None:
    run = make_run()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    finished_at = datetime(2026, 1, 2, tzinfo=UTC)

    run.transition_to(RunStatus.PREPARING)
    run.transition_to(RunStatus.RUNNING, at=started_at)
    run.transition_to(RunStatus.COMPLETED, at=finished_at)

    assert run.status is RunStatus.COMPLETED
    assert run.started_at == started_at
    assert run.finished_at == finished_at


def test_run_supports_pause_resume_and_approval_wait() -> None:
    run = make_run()

    for target in (
        RunStatus.PREPARING,
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.PAUSED,
        RunStatus.RUNNING,
    ):
        assert run.can_transition_to(target)
        run.transition_to(target)

    assert run.status is RunStatus.RUNNING


def test_terminal_run_state_rejects_further_transitions() -> None:
    run = make_run()
    run.transition_to(RunStatus.CANCELLED)

    with pytest.raises(InvalidStateTransitionError, match="Run cannot transition"):
        run.transition_to(RunStatus.RUNNING)


def test_execution_tracks_exit_code_and_timestamps() -> None:
    execution = make_execution()
    started_at = datetime(2026, 2, 1, tzinfo=UTC)
    finished_at = datetime(2026, 2, 1, 0, 0, 2, tzinfo=UTC)

    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING, at=started_at)
    execution.transition_to(ExecutionStatus.EXITED, at=finished_at, exit_code=0)

    assert execution.status is ExecutionStatus.EXITED
    assert execution.started_at == started_at
    assert execution.finished_at == finished_at
    assert execution.exit_code == 0


def test_execution_cannot_skip_starting_state() -> None:
    execution = make_execution()

    with pytest.raises(InvalidStateTransitionError, match="Execution cannot transition"):
        execution.transition_to(ExecutionStatus.RUNNING)


def test_approval_can_only_be_decided_once() -> None:
    approval = Approval(run_id="run-1", tool_call_id="call-1")
    approval.decide(
        ApprovalStatus.APPROVED,
        decided_by="operator@example.test",
        reason="Authorized scope",
    )

    assert approval.status is ApprovalStatus.APPROVED
    assert approval.decided_at is not None

    with pytest.raises(InvalidStateTransitionError, match="Approval cannot transition"):
        approval.decide(ApprovalStatus.REJECTED, decided_by="other")


def test_terminal_session_has_explicit_lifecycle() -> None:
    session = TerminalSession(run_id="run-1", execution_id="execution-1")
    session.transition_to(TerminalStatus.OPEN)
    session.transition_to(TerminalStatus.CLOSED)

    assert session.closed_at is not None
    with pytest.raises(InvalidStateTransitionError):
        session.transition_to(TerminalStatus.OPEN)
