from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from uuid import uuid4

from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from riftx.temporal import (
    AgentCycleActivityInput,
    AgentCycleActivityResult,
    AgentCycleActivityStatus,
    CleanupRunInput,
    CleanupRunResult,
    GenerateReportInput,
    GenerateReportResult,
    PendingApproval,
    PrepareRunInput,
    PrepareRunResult,
    RiftXRunWorkflow,
    RunWorkflowInput,
    RunWorkflowResult,
    RunWorkflowStatus,
    WorkflowPhase,
)


@dataclass
class FakeActivities:
    cycle_results: deque[AgentCycleActivityResult]
    cycle_inputs: list[AgentCycleActivityInput] = field(default_factory=list)
    prepared_runs: list[str] = field(default_factory=list)
    cleaned_runs: list[str] = field(default_factory=list)

    @activity.defn(name="prepare_run_activity")
    async def prepare_run(self, input: PrepareRunInput) -> PrepareRunResult:
        self.prepared_runs.append(input.run_id)
        return PrepareRunResult(run_id=input.run_id)

    @activity.defn(name="agent_cycle_activity")
    async def agent_cycle(self, input: AgentCycleActivityInput) -> AgentCycleActivityResult:
        self.cycle_inputs.append(input)
        return self.cycle_results.popleft()

    @activity.defn(name="generate_report_activity")
    async def generate_report(self, input: GenerateReportInput) -> GenerateReportResult:
        return GenerateReportResult(report_id=f"report-{input.run_id}")

    @activity.defn(name="cleanup_run_activity")
    async def cleanup(self, input: CleanupRunInput) -> CleanupRunResult:
        self.cleaned_runs.append(input.run_id)
        return CleanupRunResult()

    def registered(self) -> list[object]:
        return [self.prepare_run, self.agent_cycle, self.generate_report, self.cleanup]


@dataclass
class RetryOnceActivities(FakeActivities):
    attempts: int = 0

    @activity.defn(name="agent_cycle_activity")
    async def agent_cycle(self, input: AgentCycleActivityInput) -> AgentCycleActivityResult:
        self.cycle_inputs.append(input)
        self.attempts += 1
        if self.attempts == 1:
            raise ApplicationError("retry once")
        return self.cycle_results.popleft()


async def _wait_for_phase(
    handle: WorkflowHandle[RunWorkflowResult, RunWorkflowStatus],
    phase: WorkflowPhase,
) -> RunWorkflowStatus:
    for _ in range(100):
        status = await handle.query(RiftXRunWorkflow.get_status)
        if status.phase is phase:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(f"workflow did not reach phase {phase}")


async def test_workflow_survives_worker_restart_and_replays() -> None:
    environment = await WorkflowEnvironment.start_time_skipping()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                AgentCycleActivityResult(
                    status=AgentCycleActivityStatus.WAITING_APPROVAL,
                    checkpoint_id="checkpoint-1",
                    pending_approvals=[
                        PendingApproval(
                            call_id="call-1",
                            tool_name="run_shell",
                            arguments='{"script":"echo safe"}',
                        )
                    ],
                ),
                AgentCycleActivityResult(
                    status=AgentCycleActivityStatus.COMPLETED,
                    summary="done",
                ),
            ]
        )
    )

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(run_id="run-1"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        waiting = await _wait_for_phase(handle, WorkflowPhase.WAITING_APPROVAL)
        assert waiting.checkpoint_id == "checkpoint-1"
        assert waiting.pending_approvals[0].call_id == "call-1"
        await handle.signal(RiftXRunWorkflow.pause)
        await _wait_for_phase(handle, WorkflowPhase.PAUSED)
        await handle.signal(RiftXRunWorkflow.approve, "call-1")

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        await handle.signal(RiftXRunWorkflow.resume)
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert result.report_id == "report-run-1"
    assert activities.prepared_runs == ["run-1"]
    assert activities.cleaned_runs == ["run-1"]
    assert len(activities.cycle_inputs) == 2
    assert activities.cycle_inputs[1].checkpoint_id == "checkpoint-1"
    assert activities.cycle_inputs[1].approval_decisions == {"call-1": True}
    assert activities.cycle_inputs[0].agent_step_id != activities.cycle_inputs[1].agent_step_id

    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_workflow_waits_for_and_forwards_user_message_and_cancel_request() -> None:
    environment = await WorkflowEnvironment.start_time_skipping()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                AgentCycleActivityResult(status=AgentCycleActivityStatus.NEEDS_INPUT),
                AgentCycleActivityResult(status=AgentCycleActivityStatus.COMPLETED),
            ]
        )
    )

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(run_id="run-input"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, WorkflowPhase.WAITING_INPUT)
        await handle.signal(RiftXRunWorkflow.cancel_current_execution)
        await handle.signal(RiftXRunWorkflow.append_user_message, "  continue safely  ")
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert activities.cycle_inputs[1].user_messages == ["continue safely"]
    assert activities.cycle_inputs[1].cancel_current_execution is True
    await environment.shutdown()


async def test_agent_cycle_activity_retry_reuses_stable_agent_step_id() -> None:
    environment = await WorkflowEnvironment.start_time_skipping()
    task_queue = f"riftx-test-{uuid4()}"
    activities = RetryOnceActivities(
        cycle_results=deque([AgentCycleActivityResult(status=AgentCycleActivityStatus.COMPLETED)])
    )

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        result = await environment.client.execute_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(run_id="run-retry"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result.phase is WorkflowPhase.COMPLETED
    assert len(activities.cycle_inputs) == 2
    assert activities.cycle_inputs[0].agent_step_id == activities.cycle_inputs[1].agent_step_id
    await environment.shutdown()
