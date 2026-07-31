from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from riftx.application.errors import ServiceUnavailableError
from riftx.application.services.runs import RunApplicationService
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    RunStatus,
)

_ACTIVE_EXECUTION_STATUSES = {
    ExecutionStatus.CREATED,
    ExecutionStatus.QUEUED,
    ExecutionStatus.STARTING,
    ExecutionStatus.RUNNING,
}


class FakeRunRepository:
    def __init__(self, run: Run) -> None:
        self.run = run
        self.transitions: list[RunStatus] = []

    async def get(self, run_id: str) -> Run | None:
        return self.run if self.run.id == run_id else None

    async def update_status(self, run_id: str, target: RunStatus) -> Run:
        assert run_id == self.run.id
        self.run.transition_to(target)
        self.transitions.append(target)
        return self.run


class FakeEventRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> SimpleNamespace:
        body = dict(payload or {})
        self.events.append((run_id, event_type, body))
        return SimpleNamespace(id=f"event-{len(self.events)}", payload=body)

    def payload(self, event_type: str) -> dict[str, object]:
        return next(payload for _, current, payload in self.events if current == event_type)


class FakeExecutionRepository:
    def __init__(self, executions: list[Execution]) -> None:
        self.executions = {execution.id: execution for execution in executions}

    async def list_active(self) -> list[Execution]:
        return [
            execution
            for execution in self.executions.values()
            if execution.status in _ACTIVE_EXECUTION_STATUSES
        ]

    async def list(
        self,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Execution]:
        matches = [
            execution
            for execution in self.executions.values()
            if execution.run_id == run_id
        ]
        return matches[offset : offset + limit]

    async def get(self, execution_id: str) -> Execution | None:
        return self.executions.get(execution_id)

    async def save(self, execution: Execution) -> Execution:
        self.executions[execution.id] = execution
        return execution


class FakeWorkflowClient:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, str]] = []

    async def pause(self, run_id: str) -> None:
        await self._record("pause", run_id)

    async def cancel_current_execution(self, run_id: str) -> None:
        await self._record("cancel_current_execution", run_id)

    async def cancel(self, run_id: str) -> None:
        await self._record("cancel", run_id)

    async def _record(self, action: str, run_id: str) -> None:
        self.calls.append((action, run_id))
        if self.failure is not None:
            raise self.failure


class FakeExecutionRunner:
    def __init__(
        self,
        executions: FakeExecutionRepository,
        runs: FakeRunRepository,
        *,
        failing_ids: set[str] | None = None,
        unacknowledged_ids: set[str] | None = None,
    ) -> None:
        self.executions = executions
        self.runs = runs
        self.failing_ids = failing_ids or set()
        self.unacknowledged_ids = unacknowledged_ids or set()
        self.calls: list[str] = []
        self.run_statuses_at_cancel: list[RunStatus] = []

    async def cancel(self, execution_id: str) -> Execution:
        execution = self.executions.executions[execution_id]
        self.calls.append(execution_id)
        self.run_statuses_at_cancel.append(self.runs.run.status)
        if execution_id in self.failing_ids:
            raise RuntimeError(f"cancellation failed for {execution_id}")
        if execution.status in _ACTIVE_EXECUTION_STATUSES or (
            execution.status is ExecutionStatus.LOST
            and execution_id not in self.unacknowledged_ids
        ):
            execution.transition_to(ExecutionStatus.CANCELLED)
        return await self.executions.save(execution)


def make_run(tmp_path: Path, status: RunStatus = RunStatus.RUNNING) -> Run:
    run = Run(
        id=f"run-{status.value}",
        engagement_id="engagement-1",
        node_id="local",
        objective=Objective(description="Exercise fail-safe controls"),
        workspace_path=str(tmp_path),
        temporal_workflow_id=f"workflow-{status.value}",
    )
    if status is RunStatus.RUNNING:
        run.transition_to(RunStatus.PREPARING)
        run.transition_to(RunStatus.RUNNING)
    elif status is RunStatus.COMPLETED:
        run.transition_to(RunStatus.PREPARING)
        run.transition_to(RunStatus.RUNNING)
        run.transition_to(RunStatus.COMPLETED)
    elif status is RunStatus.FAILED:
        run.transition_to(RunStatus.PREPARING)
        run.transition_to(RunStatus.FAILED)
    elif status is RunStatus.CANCELLED:
        run.transition_to(RunStatus.CANCELLED)
    else:
        raise ValueError(f"unsupported test Run status {status.value}")
    return run


def make_execution(tmp_path: Path, run_id: str, execution_id: str) -> Execution:
    execution = Execution(
        id=execution_id,
        execution_key=f"key-{execution_id}",
        run_id=run_id,
        node_id="local",
        executor_type=ExecutorType.PROCESS,
        argv=["sleep", "30"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{execution_id}.stdout"),
        stderr_path=str(tmp_path / f"{execution_id}.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    return execution


def make_service(
    tmp_path: Path,
    run: Run,
    executions: list[Execution],
    *,
    workflow_failure: Exception | None = None,
    failing_execution_ids: set[str] | None = None,
    unacknowledged_execution_ids: set[str] | None = None,
    execution_cancel_max_passes: int = 1,
) -> tuple[
    RunApplicationService,
    FakeRunRepository,
    FakeEventRepository,
    FakeExecutionRepository,
    FakeWorkflowClient,
    FakeExecutionRunner,
]:
    runs = FakeRunRepository(run)
    events = FakeEventRepository()
    execution_repository = FakeExecutionRepository(executions)
    workflow = FakeWorkflowClient(workflow_failure)
    runner = FakeExecutionRunner(
        execution_repository,
        runs,
        failing_ids=failing_execution_ids,
        unacknowledged_ids=unacknowledged_execution_ids,
    )
    service = RunApplicationService(
        engagement_repository=object(),  # type: ignore[arg-type]
        run_repository=runs,  # type: ignore[arg-type]
        event_repository=events,  # type: ignore[arg-type]
        workflow_client=workflow,  # type: ignore[arg-type]
        execution_repository=execution_repository,  # type: ignore[arg-type]
        execution_runner=runner,  # type: ignore[arg-type]
        workspace_root=tmp_path,
        execution_cancel_timeout_seconds=0.01,
        execution_cancel_poll_seconds=0.001,
        execution_cancel_max_passes=execution_cancel_max_passes,
    )
    return service, runs, events, execution_repository, workflow, runner


@pytest.mark.parametrize(
    ("workflow_failure", "expected_error_code"),
    [
        pytest.param(
            ServiceUnavailableError("temporal_unavailable", "Temporal is unavailable"),
            "temporal_unavailable",
            id="temporal-unavailable",
        ),
        pytest.param(
            RuntimeError("Completed workflow"),
            "workflow_control_failed",
            id="workflow-closed",
        ),
    ],
)
async def test_cancel_current_stops_execution_when_workflow_signal_fails(
    tmp_path: Path,
    workflow_failure: Exception,
    expected_error_code: str,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "execution-1")
    service, _, events, _, workflow, runner = make_service(
        tmp_path,
        run,
        [execution],
        workflow_failure=workflow_failure,
    )

    returned = await service.cancel_current_execution(run.id)

    assert returned.status is RunStatus.RUNNING
    assert execution.status is ExecutionStatus.CANCELLED
    assert runner.calls == [execution.id]
    assert workflow.calls == [("cancel_current_execution", run.id)]
    signal_failure = events.payload("workflow.signal_failed")
    assert signal_failure["error_code"] == expected_error_code
    cancel_event = events.payload("execution.cancel_requested")
    assert cancel_event["workflow_synced"] is False
    assert cancel_event["confirmed_execution_ids"] == [execution.id]
    assert cancel_event["failed_executions"] == {}


async def test_pause_fences_first_and_reports_partial_execution_cancel_failure(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    good = make_execution(tmp_path, run.id, "execution-good")
    bad = make_execution(tmp_path, run.id, "execution-bad")
    service, runs, events, _, _, runner = make_service(
        tmp_path,
        run,
        [good, bad],
        failing_execution_ids={bad.id},
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.pause(run.id)

    assert captured.value.code == "execution_cancel_failed"
    assert runs.transitions == [RunStatus.PAUSING]
    assert run.status is RunStatus.PAUSING
    assert sorted(runner.calls) == sorted([good.id, bad.id])
    assert runner.run_statuses_at_cancel == [RunStatus.PAUSING, RunStatus.PAUSING]
    assert good.status is ExecutionStatus.CANCELLED
    assert bad.status is ExecutionStatus.RUNNING
    assert captured.value.details["confirmed_execution_ids"] == [good.id]
    failed = captured.value.details["failed_executions"]
    assert isinstance(failed, dict)
    assert bad.id in failed
    pause_event = events.payload("run.pause_requested")
    assert pause_event["confirmed_execution_ids"] == [good.id]
    assert bad.id in pause_event["failed_executions"]  # type: ignore[operator]


async def test_full_cancel_reaches_cancelled_when_temporal_is_unavailable(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "execution-1")
    service, runs, events, _, _, runner = make_service(
        tmp_path,
        run,
        [execution],
        workflow_failure=ServiceUnavailableError(
            "temporal_unavailable",
            "Temporal is unavailable",
        ),
    )

    returned = await service.cancel(run.id)

    assert returned.status is RunStatus.CANCELLED
    assert runs.transitions == [RunStatus.CANCELLING, RunStatus.CANCELLED]
    assert runner.calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert events.payload("workflow.signal_failed")["error_code"] == "temporal_unavailable"
    assert events.payload("run.cancel_requested")["workflow_synced"] is False


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
@pytest.mark.parametrize("operation", ["cancel_current_execution", "cancel"])
async def test_terminal_run_can_still_clean_up_orphaned_execution(
    tmp_path: Path,
    status: RunStatus,
    operation: str,
) -> None:
    run = make_run(tmp_path, status)
    execution = make_execution(tmp_path, run.id, f"orphan-{status.value}-{operation}")
    service, runs, events, _, _, runner = make_service(
        tmp_path,
        run,
        [execution],
        workflow_failure=RuntimeError("Completed workflow"),
    )

    returned = await getattr(service, operation)(run.id)

    assert returned.status is status
    assert runs.transitions == []
    assert runner.calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert events.payload("workflow.signal_failed")["error_code"] == "workflow_control_failed"


async def test_pause_accepts_lost_execution_only_after_cancel_acknowledgement(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "remote-lost-acknowledged")
    execution.transition_to(ExecutionStatus.LOST)
    service, runs, events, executions, _, runner = make_service(
        tmp_path,
        run,
        [execution],
    )

    assert await executions.list_active() == []

    returned = await service.pause(run.id)

    assert returned.status is RunStatus.PAUSED
    assert runs.transitions == [RunStatus.PAUSING, RunStatus.PAUSED]
    assert runner.calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    pause_event = events.payload("run.pause_requested")
    assert pause_event["execution_ids"] == [execution.id]
    assert pause_event["confirmed_statuses"] == {
        execution.id: ExecutionStatus.CANCELLED.value
    }


@pytest.mark.parametrize(
    ("operation", "expected_status", "event_type"),
    [
        ("pause", RunStatus.PAUSING, "run.pause_requested"),
        ("cancel", RunStatus.CANCELLING, "run.cancel_requested"),
    ],
)
async def test_run_control_fails_safe_when_lost_cancel_is_not_acknowledged(
    tmp_path: Path,
    operation: str,
    expected_status: RunStatus,
    event_type: str,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "remote-lost-offline")
    execution.transition_to(ExecutionStatus.LOST)
    service, runs, events, executions, _, runner = make_service(
        tmp_path,
        run,
        [execution],
        unacknowledged_execution_ids={execution.id},
        execution_cancel_max_passes=5,
    )

    assert await executions.list_active() == []

    with pytest.raises(ServiceUnavailableError) as captured:
        await getattr(service, operation)(run.id)

    assert captured.value.code == "execution_cancel_failed"
    assert run.status is expected_status
    assert runs.transitions == [expected_status]
    assert runner.calls == [execution.id]
    assert execution.status is ExecutionStatus.LOST
    failed = captured.value.details["failed_executions"]
    assert isinstance(failed, dict)
    assert "did not acknowledge" in str(failed[execution.id])
    control_event = events.payload(event_type)
    assert control_event["confirmed_execution_ids"] == []
    assert execution.id in control_event["failed_executions"]  # type: ignore[operator]


async def test_pause_does_not_cancel_normal_historical_completion(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "remote-completed")
    execution.transition_to(ExecutionStatus.COMPLETED)
    service, _, events, _, _, runner = make_service(tmp_path, run, [execution])

    returned = await service.pause(run.id)

    assert returned.status is RunStatus.PAUSED
    assert runner.calls == []
    assert events.payload("run.pause_requested")["execution_ids"] == []


async def test_pause_pages_past_history_to_find_lost_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("riftx.application.services.runs._EXECUTION_LIST_PAGE_SIZE", 2)
    run = make_run(tmp_path)
    completed = [
        make_execution(tmp_path, run.id, f"completed-{index}") for index in range(2)
    ]
    for execution in completed:
        execution.transition_to(ExecutionStatus.COMPLETED)
    lost = make_execution(tmp_path, run.id, "lost-on-second-page")
    lost.transition_to(ExecutionStatus.LOST)
    service, _, _, _, _, runner = make_service(
        tmp_path,
        run,
        [*completed, lost],
    )

    returned = await service.pause(run.id)

    assert returned.status is RunStatus.PAUSED
    assert runner.calls == [lost.id]
    assert lost.status is ExecutionStatus.CANCELLED
