from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from riftx.application.errors import ApplicationConflictError, ServiceUnavailableError
from riftx.application.services.run_safety import RunSafetyStopService
from riftx.application.services.runs import RunApplicationService
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    RunKind,
    RunStatus,
)
from riftx.domain.base import utc_now

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

    async def commit_finalization(
        self,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool = False,
    ) -> Run:
        assert run_id == self.run.id
        del defer_cleanup_event
        if self.run.status is not target:
            self.run.transition_to(target)
            self.transitions.append(target)
        return self.run

    async def get_finalization_intent(self, run_id: str) -> None:
        assert run_id == self.run.id
        return None


class TerminalRaceRunRepository(FakeRunRepository):
    def __init__(self, run: Run, *, race_target: RunStatus) -> None:
        super().__init__(run)
        self.race_target = race_target
        self.raced = False

    async def update_status(self, run_id: str, target: RunStatus) -> Run:
        if target is self.race_target and not self.raced:
            self.raced = True
            self.run.transition_to(RunStatus.FAILED)
            self.transitions.append(RunStatus.FAILED)
        return await super().update_status(run_id, target)


class FakeEventRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str | None = None,
    ) -> SimpleNamespace:
        body = dict(payload or {})
        self.events.append((run_id, event_type, body))
        return SimpleNamespace(
            id=event_id or f"event-{len(self.events)}",
            run_id=run_id,
            event_type=event_type,
            payload=body,
        )

    async def get(self, event_id: str) -> SimpleNamespace | None:
        for index, (run_id, event_type, payload) in enumerate(self.events, start=1):
            if event_id == f"event-{index}":
                return SimpleNamespace(
                    id=event_id,
                    run_id=run_id,
                    event_type=event_type,
                    payload=payload,
                )
        return None

    async def list_after(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(sequence=sequence, event_type=event_type, payload=payload)
            for sequence, (event_run_id, event_type, payload) in enumerate(self.events, start=1)
            if event_run_id == run_id and sequence > after_sequence
        ][:limit]

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
            execution for execution in self.executions.values() if execution.run_id == run_id
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

    async def resume(self, run_id: str) -> None:
        await self._record("resume", run_id)

    async def cancel_current_execution(self, run_id: str) -> None:
        await self._record("cancel_current_execution", run_id)

    async def cancel(self, run_id: str) -> None:
        await self._record("cancel", run_id)

    async def _record(self, action: str, run_id: str) -> None:
        self.calls.append((action, run_id))
        if self.failure is not None:
            raise self.failure


class BlockingWorkflowClient(FakeWorkflowClient):
    async def _record(self, action: str, run_id: str) -> None:
        self.calls.append((action, run_id))
        await asyncio.Event().wait()


class GatedResumeWorkflowClient(FakeWorkflowClient):
    def __init__(self) -> None:
        super().__init__()
        self.resume_entered = asyncio.Event()
        self.release_resume = asyncio.Event()

    async def resume(self, run_id: str) -> None:
        self.calls.append(("resume", run_id))
        self.resume_entered.set()
        await self.release_resume.wait()


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
        if execution_id not in self.unacknowledged_ids and (
            execution.status in _ACTIVE_EXECUTION_STATUSES
            or execution.status in {ExecutionStatus.FAILED, ExecutionStatus.LOST}
        ):
            execution.transition_to(ExecutionStatus.CANCELLED)
            execution.physical_stop_confirmed_at = utc_now()
        return await self.executions.save(execution)


class FakeResourceStopper:
    def __init__(
        self,
        run: Run,
        *,
        attempted_ids: tuple[str, ...] = (),
        node_ids: dict[str, str] | None = None,
        observed_statuses: dict[str, str] | None = None,
        confirmed_statuses: dict[str, str] | None = None,
        failures: dict[str, str] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.run = run
        self.result = SimpleNamespace(
            attempted_ids=attempted_ids,
            node_ids=node_ids or {},
            observed_statuses=observed_statuses or {},
            confirmed_statuses=confirmed_statuses or {},
            failures=failures or {},
        )
        self.failure = failure
        self.calls: list[str] = []
        self.run_statuses_at_stop: list[RunStatus] = []

    async def stop_run(self, run_id: str) -> SimpleNamespace:
        self.calls.append(run_id)
        self.run_statuses_at_stop.append(self.run.status)
        if self.failure is not None:
            raise self.failure
        return self.result


class SharedOwnedEffect:
    def __init__(self, resource_id: str, active_status: str, stopped_status: str) -> None:
        self.resource_id = resource_id
        self.active_status = active_status
        self.stopped_status = stopped_status
        self.stopped = False
        self.foreign_attempted = asyncio.Event()


class OwnerResourceStopper:
    def __init__(self, effect: SharedOwnedEffect) -> None:
        self.effect = effect

    async def stop_run(self, run_id: str) -> SimpleNamespace:
        self.effect.stopped = True
        return SimpleNamespace(
            attempted_ids=(self.effect.resource_id,),
            node_ids={self.effect.resource_id: "local"},
            observed_statuses={self.effect.resource_id: self.effect.stopped_status},
            confirmed_statuses={self.effect.resource_id: self.effect.stopped_status},
            failures={},
        )


class ForeignResourceStopper:
    def __init__(self, effect: SharedOwnedEffect) -> None:
        self.effect = effect

    async def stop_run(self, run_id: str) -> SimpleNamespace:
        if self.effect.stopped:
            return SimpleNamespace(
                attempted_ids=(),
                node_ids={},
                observed_statuses={},
                confirmed_statuses={},
                failures={},
            )
        self.effect.foreign_attempted.set()
        return SimpleNamespace(
            attempted_ids=(self.effect.resource_id,),
            node_ids={self.effect.resource_id: "local"},
            observed_statuses={self.effect.resource_id: self.effect.active_status},
            confirmed_statuses={},
            failures={self.effect.resource_id: "handle belongs to the other process"},
        )


def make_run(tmp_path: Path, status: RunStatus = RunStatus.RUNNING) -> Run:
    run = Run(
        kind="general",
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
        run.transition_to(RunStatus.RUNNING)
        run.transition_to(RunStatus.FAILED)
    elif status is RunStatus.CANCELLED:
        run.transition_to(RunStatus.PREPARING)
        run.transition_to(RunStatus.RUNNING)
        run.transition_to(RunStatus.CANCELLING)
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


def make_initially_waiting_run(tmp_path: Path) -> Run:
    run = Run(
        kind="general",
        id="run-initially-waiting",
        engagement_id="engagement-1",
        node_id="local",
        objective=Objective(description="Wait for the initial instruction"),
        workspace_path=str(tmp_path),
        temporal_workflow_id="workflow-initially-waiting",
    )
    run.transition_to(RunStatus.WAITING_USER)
    assert run.started_at is None
    return run


def make_initially_waiting_paused_run(tmp_path: Path) -> Run:
    run = make_initially_waiting_run(tmp_path)
    run.transition_to(RunStatus.PAUSING)
    run.transition_to(RunStatus.PAUSED)
    assert run.started_at is None
    return run


def make_code_audit_run(tmp_path: Path) -> Run:
    return Run(
        kind=RunKind.CODE_AUDIT,
        id="code-audit-run",
        engagement_id="engagement-1",
        node_id="local",
        objective=Objective(description="Code Audit safety bridge"),
        workspace_path=str(tmp_path / "audit-workspace"),
        temporal_workflow_id="riftx-code-audit-audit-1",
    )


def make_service(
    tmp_path: Path,
    run: Run,
    executions: list[Execution],
    *,
    workflow_failure: Exception | None = None,
    failing_execution_ids: set[str] | None = None,
    unacknowledged_execution_ids: set[str] | None = None,
    execution_cancel_max_passes: int = 1,
    resource_stoppers: dict[str, object] | None = None,
    workflow_client: FakeWorkflowClient | None = None,
    run_repository: FakeRunRepository | None = None,
    workflow_signal_timeout_seconds: float = 0.5,
) -> tuple[
    RunApplicationService,
    FakeRunRepository,
    FakeEventRepository,
    FakeExecutionRepository,
    FakeWorkflowClient,
    FakeExecutionRunner,
]:
    runs = run_repository or FakeRunRepository(run)
    events = FakeEventRepository()
    execution_repository = FakeExecutionRepository(executions)
    workflow = workflow_client or FakeWorkflowClient(workflow_failure)
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
        resource_stoppers=resource_stoppers,  # type: ignore[arg-type]
        execution_cancel_timeout_seconds=0.01,
        execution_cancel_poll_seconds=0.001,
        execution_cancel_max_passes=execution_cancel_max_passes,
        workflow_signal_timeout_seconds=workflow_signal_timeout_seconds,
    )
    return service, runs, events, execution_repository, workflow, runner


@pytest.mark.parametrize(
    "operation",
    [
        "pause",
        "resume",
        "cancel",
        "cancel_current_execution",
        "compact",
        "switch_model",
        "append_user_message",
    ],
)
async def test_code_audit_rejects_generic_run_operations_before_any_effect(
    tmp_path: Path,
    operation: str,
) -> None:
    run = make_code_audit_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "audit-execution-canary")
    service, runs, events, _, workflow, runner = make_service(
        tmp_path,
        run,
        [execution],
    )
    calls = {
        "pause": lambda: service.pause(run.id),
        "resume": lambda: service.resume(run.id),
        "cancel": lambda: service.cancel(run.id),
        "cancel_current_execution": lambda: service.cancel_current_execution(run.id),
        "compact": lambda: service.compact(run.id, max_history_items=1),
        "switch_model": lambda: service.switch_model(run.id, "fast"),
        "append_user_message": lambda: service.append_user_message(run.id, "bypass"),
    }

    with pytest.raises(ApplicationConflictError) as captured:
        await calls[operation]()

    assert captured.value.code == "run_kind_operation_unsupported"
    assert run.status is RunStatus.CREATED
    assert runs.transitions == []
    assert events.events == []
    assert workflow.calls == []
    assert runner.calls == []
    assert execution.status is ExecutionStatus.RUNNING
    assert execution.physical_stop_confirmed_at is None


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


async def test_cancel_current_never_waits_on_unavailable_temporal_connection(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "execution-temporal-timeout")
    workflow = BlockingWorkflowClient()
    service, _, events, _, _, runner = make_service(
        tmp_path,
        run,
        [execution],
        workflow_client=workflow,
        workflow_signal_timeout_seconds=0.01,
    )

    returned = await asyncio.wait_for(
        service.cancel_current_execution(run.id),
        timeout=0.2,
    )

    assert returned.status is RunStatus.RUNNING
    assert execution.status is ExecutionStatus.CANCELLED
    assert runner.calls == [execution.id]
    assert workflow.calls == [("cancel_current_execution", run.id)]
    signal_failure = events.payload("workflow.signal_failed")
    assert signal_failure["error_type"] == "TimeoutError"
    assert "safety-control deadline" in str(signal_failure["reason"])
    assert events.payload("execution.cancel_requested")["workflow_synced"] is False


async def test_cancel_current_does_not_release_workflow_when_stop_is_unconfirmed(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "execution-unconfirmed")
    service, _, events, _, workflow, runner = make_service(
        tmp_path,
        run,
        [execution],
        unacknowledged_execution_ids={execution.id},
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.cancel_current_execution(run.id)

    assert captured.value.code == "execution_cancel_failed"
    assert execution.status is ExecutionStatus.RUNNING
    assert runner.calls == [execution.id]
    assert workflow.calls == []
    event = events.payload("execution.cancel_requested")
    assert event["workflow_synced"] is False
    assert event["confirmed_execution_ids"] == []


async def test_resume_initial_waiting_run_restores_waiting_user_without_starting_clock(
    tmp_path: Path,
) -> None:
    run = make_initially_waiting_paused_run(tmp_path)
    service, runs, events, _, workflow, _ = make_service(tmp_path, run, [])

    returned = await service.resume(run.id)

    assert returned.status is RunStatus.WAITING_USER
    assert returned.started_at is None
    assert runs.transitions == [RunStatus.WAITING_USER]
    assert workflow.calls == []
    assert events.payload("run.resume_requested") == {}


async def test_resume_initial_waiting_run_ignores_legacy_workflow_started_marker(
    tmp_path: Path,
) -> None:
    run = make_initially_waiting_paused_run(tmp_path)
    service, runs, events, _, workflow, _ = make_service(
        tmp_path,
        run,
        [],
        workflow_failure=RuntimeError("Workflow unavailable"),
    )
    await events.append(
        run.id,
        "workflow.started",
        {"workflow_id": run.temporal_workflow_id},
    )

    returned = await service.resume(run.id)

    assert returned.status is RunStatus.WAITING_USER
    assert returned.started_at is None
    assert runs.transitions == [RunStatus.WAITING_USER]
    assert workflow.calls == []
    assert events.payload("run.resume_requested") == {}


async def test_resume_cannot_bypass_an_unconfirmed_pause_stop(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.transition_to(RunStatus.PAUSING)
    execution = make_execution(tmp_path, run.id, "execution-still-running")
    service, runs, _, _, workflow, runner = make_service(
        tmp_path,
        run,
        [execution],
        unacknowledged_execution_ids={execution.id},
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.resume(run.id)

    assert captured.value.code == "execution_cancel_failed"
    assert run.status is RunStatus.PAUSING
    assert runs.transitions == []
    assert runner.calls == [execution.id]
    assert workflow.calls == [("pause", run.id)]
    assert not any(action == "resume" for action, _ in workflow.calls)


async def test_cancel_fence_supersedes_an_inflight_resume_signal(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.transition_to(RunStatus.PAUSING)
    run.transition_to(RunStatus.PAUSED)
    execution = make_execution(tmp_path, run.id, "execution-cancel-race")
    workflow = GatedResumeWorkflowClient()
    service, runs, events, _, _, runner = make_service(
        tmp_path,
        run,
        [execution],
        unacknowledged_execution_ids={execution.id},
        workflow_client=workflow,
    )

    resume_task = asyncio.create_task(service.resume(run.id))
    await workflow.resume_entered.wait()

    with pytest.raises(ServiceUnavailableError) as cancel_error:
        await service.cancel(run.id)

    assert cancel_error.value.code == "execution_cancel_failed"
    assert run.status is RunStatus.CANCELLING
    assert runner.calls == [execution.id]
    assert not any(action == "cancel" for action, _ in workflow.calls)

    workflow.release_resume.set()
    with pytest.raises(ApplicationConflictError) as resume_error:
        await resume_task

    assert resume_error.value.code == "run_resume_superseded"
    assert resume_error.value.details == {
        "run_id": run.id,
        "status": "cancelling",
        "workflow_resuspended": True,
    }
    assert run.status is RunStatus.CANCELLING
    assert runs.transitions == [RunStatus.RUNNING, RunStatus.CANCELLING]
    assert workflow.calls == [("resume", run.id), ("pause", run.id)]
    assert events.payload("run.resume_superseded") == {
        "status": "cancelling",
        "workflow_resuspended": True,
    }
    assert not any(event_type == "run.resume_requested" for _, event_type, _ in events.events)


async def test_ambiguous_resume_failure_runs_full_stop_gate(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.transition_to(RunStatus.PAUSING)
    run.transition_to(RunStatus.PAUSED)
    execution = make_execution(tmp_path, run.id, "execution-after-ambiguous-resume")
    temporal_failure = ServiceUnavailableError(
        "temporal_unavailable",
        "Temporal may have accepted the resume signal",
    )
    service, runs, events, _, workflow, runner = make_service(
        tmp_path,
        run,
        [execution],
        workflow_failure=temporal_failure,
        unacknowledged_execution_ids={execution.id},
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.resume(run.id)

    assert captured.value.code == "execution_cancel_failed"
    assert captured.value.__cause__ is temporal_failure
    assert run.status is RunStatus.PAUSING
    assert execution.status is ExecutionStatus.RUNNING
    assert runs.transitions == [RunStatus.RUNNING, RunStatus.PAUSING]
    assert runner.calls == [execution.id]
    assert workflow.calls == [("resume", run.id), ("pause", run.id)]
    pause_event = events.payload("run.pause_requested")
    assert execution.id in pause_event["failed_executions"]  # type: ignore[operator]
    assert not any(event_type == "run.resume_requested" for _, event_type, _ in events.events)


@pytest.mark.parametrize(
    ("operation", "expected_status", "expected_transitions"),
    [
        ("pause", RunStatus.PAUSED, [RunStatus.PAUSING, RunStatus.PAUSED]),
        ("cancel_current_execution", RunStatus.WAITING_USER, []),
        ("cancel", RunStatus.CANCELLED, [RunStatus.CANCELLING, RunStatus.CANCELLED]),
    ],
)
async def test_pre_instruction_safety_controls_are_local_even_with_legacy_workflow_marker(
    tmp_path: Path,
    operation: str,
    expected_status: RunStatus,
    expected_transitions: list[RunStatus],
) -> None:
    run = make_initially_waiting_run(tmp_path)
    service, runs, events, _, workflow, runner = make_service(
        tmp_path,
        run,
        [],
        workflow_failure=RuntimeError("Temporal must not be contacted"),
    )
    await events.append(
        run.id,
        "workflow.started",
        {"workflow_id": run.temporal_workflow_id},
    )

    returned = await getattr(service, operation)(run.id)

    assert returned.status is expected_status
    assert returned.started_at is None
    assert runs.transitions == expected_transitions
    assert workflow.calls == []
    assert runner.calls == []


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
    assert captured.value.details["execution_nodes"] == {
        good.id: good.node_id,
        bad.id: bad.node_id,
    }
    assert captured.value.details["execution_statuses"] == {
        good.id: "cancelled",
        bad.id: "running",
    }
    failed = captured.value.details["failed_executions"]
    assert isinstance(failed, dict)
    assert bad.id in failed
    pause_event = events.payload("run.pause_requested")
    assert pause_event["confirmed_execution_ids"] == [good.id]
    assert bad.id in pause_event["failed_executions"]  # type: ignore[operator]


@pytest.mark.parametrize(
    ("operation", "race_target", "workflow_action"),
    [
        ("pause", RunStatus.PAUSING, None),
        ("cancel", RunStatus.CANCELLING, "cancel"),
    ],
)
async def test_safety_control_still_stops_effects_when_terminal_race_wins(
    tmp_path: Path,
    operation: str,
    race_target: RunStatus,
    workflow_action: str | None,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, f"execution-{operation}-terminal-race")
    racing_runs = TerminalRaceRunRepository(run, race_target=race_target)
    service, runs, events, _, workflow, runner = make_service(
        tmp_path,
        run,
        [execution],
        run_repository=racing_runs,
    )

    returned = await getattr(service, operation)(run.id)

    assert returned.status is RunStatus.FAILED
    assert runs.transitions == [RunStatus.FAILED]
    assert execution.status is ExecutionStatus.CANCELLED
    assert runner.calls == [execution.id]
    assert workflow.calls == ([] if workflow_action is None else [(workflow_action, run.id)])
    event_type = "run.pause_requested" if operation == "pause" else "run.cancel_requested"
    assert events.payload(event_type)["confirmed_execution_ids"] == [execution.id]
    if operation == "pause":
        assert events.payload(event_type)["pause_fence_acquired"] is False
        assert events.payload(event_type)["superseded_by_status"] == "failed"


def test_stop_disposition_closes_over_observed_only_resource_id() -> None:
    disposition = RunSafetyStopService._normalize_stop_disposition(
        "browser_sessions",
        SimpleNamespace(
            attempted_ids=(),
            node_ids={},
            observed_statuses={"browser-observed-only": "active"},
            confirmed_statuses={},
            failures={},
        ),
    )

    assert disposition.attempted_ids == ("browser-observed-only",)
    assert disposition.observed_statuses == {"browser-observed-only": "active"}
    assert disposition.confirmed_statuses == {}
    assert "no affirmative confirmation" in disposition.failures["browser-observed-only"]
    assert disposition.succeeded is False


@pytest.mark.parametrize(
    ("resource_type", "active_status", "stopped_status"),
    [
        ("browser_sessions", "active", "closed"),
        ("target_http_requests", "executing", "cancelled"),
    ],
)
async def test_cross_process_drain_observes_owner_ack_in_same_safety_request(
    tmp_path: Path,
    resource_type: str,
    active_status: str,
    stopped_status: str,
) -> None:
    run = make_run(tmp_path)
    executions = FakeExecutionRepository([])
    runner = FakeExecutionRunner(executions, FakeRunRepository(run))
    effect = SharedOwnedEffect(f"{resource_type}-owned", active_status, stopped_status)
    empty = FakeResourceStopper(run)

    def service(stopper: object) -> RunSafetyStopService:
        resource_stoppers = {
            "browser_sessions": empty,
            "target_http_requests": empty,
        }
        resource_stoppers[resource_type] = stopper  # type: ignore[assignment]
        return RunSafetyStopService(
            execution_repository=executions,  # type: ignore[arg-type]
            execution_runner=runner,  # type: ignore[arg-type]
            resource_stoppers=resource_stoppers,  # type: ignore[arg-type]
            execution_cancel_poll_seconds=0.001,
            resource_stop_poll_seconds=0.001,
            resource_stop_max_passes=100,
        )

    worker_stop = asyncio.create_task(
        service(ForeignResourceStopper(effect)).stop_run(run.id, drain=True)
    )
    await asyncio.wait_for(effect.foreign_attempted.wait(), timeout=1)
    owner_result = await service(OwnerResourceStopper(effect)).stop_run(run.id, drain=False)
    foreign_result = await asyncio.wait_for(worker_stop, timeout=1)

    assert owner_result.succeeded is True
    assert effect.stopped is True
    assert foreign_result.succeeded is True


def test_stop_disposition_closes_over_node_only_resource_id() -> None:
    disposition = RunSafetyStopService._normalize_stop_disposition(
        "target_http_requests",
        SimpleNamespace(
            attempted_ids=(),
            node_ids={"request-node-only": "remote-1"},
            observed_statuses={},
            confirmed_statuses={},
            failures={},
        ),
    )

    assert disposition.attempted_ids == ("request-node-only",)
    assert disposition.node_ids == {"request-node-only": "remote-1"}
    assert disposition.confirmed_statuses == {}
    assert "no affirmative confirmation" in disposition.failures["request-node-only"]
    assert disposition.succeeded is False


@pytest.mark.parametrize(
    ("resource_type", "status"),
    [
        pytest.param("browser_sessions", "active", id="browser-active"),
        pytest.param("target_http_requests", "executing", id="target-http-executing"),
        pytest.param("executions", ExecutionStatus.RUNNING.value, id="execution-running"),
        pytest.param("executions", ExecutionStatus.FAILED.value, id="execution-failed"),
        pytest.param("executions", ExecutionStatus.LOST.value, id="execution-lost"),
        pytest.param("executions", "unknown", id="execution-unknown"),
    ],
)
def test_stop_disposition_rejects_active_or_unknown_confirmation(
    resource_type: str,
    status: str,
) -> None:
    disposition = RunSafetyStopService._normalize_stop_disposition(
        resource_type,
        SimpleNamespace(
            attempted_ids=("resource-1",),
            node_ids={"resource-1": "node-1"},
            observed_statuses={"resource-1": status},
            confirmed_statuses={"resource-1": status},
            failures={},
        ),
    )

    assert disposition.attempted_ids == ("resource-1",)
    assert disposition.confirmed_ids == ()
    assert disposition.confirmed_statuses == {}
    assert "untrusted confirmation status" in disposition.failures["resource-1"]
    assert status in disposition.failures["resource-1"]
    assert disposition.succeeded is False


@pytest.mark.parametrize(
    ("resource_type", "status"),
    [
        pytest.param("browser_sessions", "closed", id="browser-closed"),
        pytest.param("target_http_requests", "completed", id="target-http-completed"),
        pytest.param("target_http_requests", "rejected", id="target-http-rejected"),
        pytest.param("target_http_requests", "failed", id="target-http-failed"),
        pytest.param("target_http_requests", "cancelled", id="target-http-cancelled"),
        pytest.param(
            "executions",
            ExecutionStatus.COMPLETED.value,
            id="execution-completed",
        ),
        pytest.param("executions", ExecutionStatus.EXITED.value, id="execution-exited"),
        pytest.param(
            "executions",
            ExecutionStatus.CANCELLED.value,
            id="execution-cancelled",
        ),
        pytest.param(
            "executions",
            ExecutionStatus.HARD_TIMEOUT.value,
            id="execution-hard-timeout",
        ),
    ],
)
def test_stop_disposition_accepts_only_family_specific_physical_stop_statuses(
    resource_type: str,
    status: str,
) -> None:
    disposition = RunSafetyStopService._normalize_stop_disposition(
        resource_type,
        SimpleNamespace(
            attempted_ids=("resource-1",),
            node_ids={},
            observed_statuses={"resource-1": status},
            confirmed_statuses={"resource-1": status},
            failures={},
        ),
    )

    assert disposition.confirmed_statuses == {"resource-1": status}
    assert disposition.failures == {}
    assert disposition.succeeded is True


async def test_pause_fences_and_confirms_every_effect_family(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "execution-1")
    browser = FakeResourceStopper(
        run,
        attempted_ids=("browser-1",),
        node_ids={"browser-1": "local"},
        observed_statuses={"browser-1": "closed"},
        confirmed_statuses={"browser-1": "closed"},
    )
    target_http = FakeResourceStopper(
        run,
        attempted_ids=("request-1",),
        node_ids={"request-1": "local"},
        observed_statuses={"request-1": "cancelled"},
        confirmed_statuses={"request-1": "cancelled"},
    )
    service, runs, events, _, _, _ = make_service(
        tmp_path,
        run,
        [execution],
        resource_stoppers={
            "browser_sessions": browser,
            "target_http_requests": target_http,
        },
    )

    returned = await service.pause(run.id)

    assert returned.status is RunStatus.PAUSED
    assert runs.transitions == [RunStatus.PAUSING, RunStatus.PAUSED]
    assert browser.calls == [run.id]
    assert target_http.calls == [run.id]
    assert browser.run_statuses_at_stop == [RunStatus.PAUSING]
    assert target_http.run_statuses_at_stop == [RunStatus.PAUSING]
    payload = events.payload("run.pause_requested")
    assert payload["execution_ids"] == [execution.id]
    resources = payload["stop_resources"]
    assert isinstance(resources, dict)
    assert resources["executions"]["confirmed_ids"] == [execution.id]  # type: ignore[index]
    assert resources["browser_sessions"]["confirmed_ids"] == ["browser-1"]  # type: ignore[index]
    assert resources["target_http_requests"]["confirmed_ids"] == ["request-1"]  # type: ignore[index]
    assert payload["failed_resource_types"] == []


async def test_unconfirmed_effect_keeps_run_fenced_with_structured_disposition(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    browser = FakeResourceStopper(
        run,
        attempted_ids=("browser-remote",),
        node_ids={"browser-remote": "remote-1"},
        observed_statuses={"browser-remote": "active"},
        failures={"browser-remote": "remote Runner did not acknowledge closure"},
    )
    service, runs, events, _, workflow, _ = make_service(
        tmp_path,
        run,
        [],
        workflow_failure=ServiceUnavailableError(
            "temporal_unavailable",
            "Temporal is unavailable",
        ),
        resource_stoppers={"browser_sessions": browser},
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.pause(run.id)

    assert captured.value.code == "safety_stop_failed"
    assert run.status is RunStatus.PAUSING
    assert runs.transitions == [RunStatus.PAUSING]
    assert workflow.calls == [("pause", run.id)]
    assert captured.value.details["failed_resource_types"] == ["browser_sessions"]
    resources = captured.value.details["stop_resources"]
    assert isinstance(resources, dict)
    browser_disposition = resources["browser_sessions"]
    assert browser_disposition["attempted_ids"] == ["browser-remote"]  # type: ignore[index]
    assert browser_disposition["confirmed_ids"] == []  # type: ignore[index]
    assert "did not acknowledge" in browser_disposition["failures"]["browser-remote"]  # type: ignore[index]
    event = events.payload("run.pause_requested")
    assert event["workflow_synced"] is False
    assert event["failed_resource_types"] == ["browser_sessions"]


async def test_stopper_crash_is_fail_closed_and_does_not_hide_other_results(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    browser = FakeResourceStopper(run, failure=RuntimeError("browser controller crashed"))
    target_http = FakeResourceStopper(
        run,
        attempted_ids=("request-stopped",),
        observed_statuses={"request-stopped": "cancelled"},
        confirmed_statuses={"request-stopped": "cancelled"},
    )
    service, _, events, _, workflow, _ = make_service(
        tmp_path,
        run,
        [],
        resource_stoppers={
            "browser_sessions": browser,
            "target_http_requests": target_http,
        },
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.cancel(run.id)

    assert captured.value.code == "safety_stop_failed"
    assert run.status is RunStatus.CANCELLING
    assert workflow.calls == []
    resources = captured.value.details["stop_resources"]
    assert isinstance(resources, dict)
    browser_disposition = resources["browser_sessions"]
    assert browser_disposition["attempted_ids"] == ["browser_sessions:controller"]  # type: ignore[index]
    assert (
        "controller crashed"
        in browser_disposition["failures"][  # type: ignore[index]
            "browser_sessions:controller"
        ]
    )
    assert resources["target_http_requests"]["confirmed_ids"] == ["request-stopped"]  # type: ignore[index]
    cancel_event = events.payload("run.cancel_requested")
    assert cancel_event["workflow_synced"] is False
    assert cancel_event["failed_resource_types"] == ["browser_sessions"]


async def test_omitted_resource_stop_outcome_cannot_make_cancel_terminal(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    target_http = FakeResourceStopper(
        run,
        attempted_ids=("request-unconfirmed",),
        node_ids={"request-unconfirmed": "remote-1"},
        observed_statuses={"request-unconfirmed": "executing"},
        # A buggy controller omitted both confirmation and failure evidence.
        confirmed_statuses={},
        failures={},
    )
    service, runs, events, _, workflow, _ = make_service(
        tmp_path,
        run,
        [],
        resource_stoppers={"target_http_requests": target_http},
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.cancel(run.id)

    assert captured.value.code == "safety_stop_failed"
    assert run.status is RunStatus.CANCELLING
    assert runs.transitions == [RunStatus.CANCELLING]
    assert workflow.calls == []
    resources = captured.value.details["stop_resources"]
    assert isinstance(resources, dict)
    failure = resources["target_http_requests"]["failures"]["request-unconfirmed"]  # type: ignore[index]
    assert "no affirmative confirmation" in failure
    event = events.payload("run.cancel_requested")
    assert event["workflow_synced"] is False
    assert event["failed_resource_types"] == ["target_http_requests"]


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
    assert pause_event["confirmed_statuses"] == {execution.id: ExecutionStatus.CANCELLED.value}


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
    assert captured.value.details["execution_nodes"] == {execution.id: execution.node_id}
    assert captured.value.details["execution_statuses"] == {execution.id: "lost"}
    failed = captured.value.details["failed_executions"]
    assert isinstance(failed, dict)
    assert "did not acknowledge" in str(failed[execution.id])
    control_event = events.payload(event_type)
    assert control_event["confirmed_execution_ids"] == []
    assert execution.id in control_event["failed_executions"]  # type: ignore[operator]


@pytest.mark.parametrize(
    ("operation", "expected_status", "event_type"),
    [
        ("pause", RunStatus.PAUSING, "run.pause_requested"),
        ("cancel", RunStatus.CANCELLING, "run.cancel_requested"),
    ],
)
async def test_run_control_requires_cancelled_ack_for_failed_execution(
    tmp_path: Path,
    operation: str,
    expected_status: RunStatus,
    event_type: str,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "remote-failed-but-unconfirmed")
    execution.transition_to(ExecutionStatus.FAILED)
    service, runs, events, _, workflow, runner = make_service(
        tmp_path,
        run,
        [execution],
        unacknowledged_execution_ids={execution.id},
    )

    with pytest.raises(ServiceUnavailableError) as captured:
        await getattr(service, operation)(run.id)

    assert captured.value.code == "execution_cancel_failed"
    assert run.status is expected_status
    assert runs.transitions == [expected_status]
    assert runner.calls == [execution.id]
    assert execution.status is ExecutionStatus.FAILED
    assert captured.value.details["confirmed_execution_ids"] == []
    assert captured.value.details["execution_statuses"] == {execution.id: "failed"}
    control_event = events.payload(event_type)
    assert control_event["confirmed_execution_ids"] == []
    assert execution.id in control_event["failed_executions"]  # type: ignore[operator]
    expected_workflow_calls = [("pause", run.id)] if operation == "pause" else []
    assert workflow.calls == expected_workflow_calls


@pytest.mark.parametrize(
    ("operation", "fenced_status", "terminal_status", "workflow_action"),
    [
        ("pause", RunStatus.PAUSING, RunStatus.PAUSED, "pause"),
        ("cancel", RunStatus.CANCELLING, RunStatus.CANCELLED, "cancel"),
    ],
)
async def test_owner_reconciler_finishes_control_after_late_stop_ack(
    tmp_path: Path,
    operation: str,
    fenced_status: RunStatus,
    terminal_status: RunStatus,
    workflow_action: str,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, f"late-ack-{operation}")
    service, runs, events, _, workflow, runner = make_service(
        tmp_path,
        run,
        [execution],
        unacknowledged_execution_ids={execution.id},
    )

    with pytest.raises(ServiceUnavailableError):
        await getattr(service, operation)(run.id)
    assert run.status is fenced_status
    runner.unacknowledged_ids.clear()

    result = await service.stop_resources_for_cleanup(run.id)

    assert result.succeeded is True
    assert run.status is terminal_status
    assert runs.transitions == [fenced_status, terminal_status]
    expected_calls = (
        [("pause", run.id), (workflow_action, run.id)]
        if operation == "pause"
        else [(workflow_action, run.id)]
    )
    assert workflow.calls == expected_calls
    reconciled = events.payload("run.cleanup_reconciled")
    assert reconciled["status"] == terminal_status.value
    assert reconciled["confirmed_execution_ids"] == [execution.id]


async def test_failed_execution_converges_only_after_cancelled_ack(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "remote-failed-then-stopped")
    execution.transition_to(ExecutionStatus.FAILED)
    service, _, events, _, _, runner = make_service(tmp_path, run, [execution])

    returned = await service.pause(run.id)

    assert returned.status is RunStatus.PAUSED
    assert runner.calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    pause_event = events.payload("run.pause_requested")
    assert pause_event["confirmed_execution_ids"] == [execution.id]
    assert pause_event["confirmed_statuses"] == {execution.id: "cancelled"}


async def test_unacknowledged_remote_terminal_remains_enumerable_and_blocks_pause(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "remote-terminal-offline")
    execution.executor_type = ExecutorType.PTY
    execution.execution_key = "terminal:terminal-offline"
    service, runs, events, executions, _, runner = make_service(
        tmp_path,
        run,
        [execution],
        unacknowledged_execution_ids={execution.id},
    )

    assert [item.id for item in await executions.list_active()] == [execution.id]

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.pause(run.id)

    assert captured.value.code == "execution_cancel_failed"
    assert run.status is RunStatus.PAUSING
    assert runs.transitions == [RunStatus.PAUSING]
    assert runner.calls == [execution.id]
    assert execution.status is ExecutionStatus.RUNNING
    assert captured.value.details["execution_statuses"] == {execution.id: "running"}
    failed = captured.value.details["failed_executions"]
    assert isinstance(failed, dict)
    assert "remains running after cancellation" in str(failed[execution.id])
    pause_event = events.payload("run.pause_requested")
    assert pause_event["confirmed_execution_ids"] == []
    assert execution.id in pause_event["failed_executions"]  # type: ignore[operator]


async def test_pause_does_not_cancel_normal_historical_completion(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "remote-completed")
    execution.transition_to(ExecutionStatus.COMPLETED)
    execution.physical_stop_confirmed_at = utc_now()
    service, _, events, _, _, runner = make_service(tmp_path, run, [execution])

    returned = await service.pause(run.id)

    assert returned.status is RunStatus.PAUSED
    assert runner.calls == []
    assert events.payload("run.pause_requested")["execution_ids"] == []


async def test_pause_refuses_terminal_execution_without_durable_stop_proof(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    execution = make_execution(tmp_path, run.id, "unproved-completed")
    execution.transition_to(ExecutionStatus.COMPLETED)
    service, _, events, _, _, runner = make_service(tmp_path, run, [execution])

    with pytest.raises(ServiceUnavailableError) as captured:
        await service.pause(run.id)

    assert captured.value.code == "execution_cancel_failed"
    assert runner.calls == [execution.id]
    assert captured.value.details["confirmed_execution_ids"] == []
    assert execution.id in captured.value.details["failed_executions"]
    pause_event = events.payload("run.pause_requested")
    assert execution.id in pause_event["failed_executions"]  # type: ignore[operator]


async def test_pause_pages_past_history_to_find_lost_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("riftx.application.services.run_safety._EXECUTION_LIST_PAGE_SIZE", 2)
    run = make_run(tmp_path)
    completed = [make_execution(tmp_path, run.id, f"completed-{index}") for index in range(2)]
    for execution in completed:
        execution.transition_to(ExecutionStatus.COMPLETED)
        execution.physical_stop_confirmed_at = utc_now()
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
