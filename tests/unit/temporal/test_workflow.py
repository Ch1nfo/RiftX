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
    CleanupRunInput,
    CleanupRunResult,
    CompactContextInput,
    CompactContextResult,
    GenerateReportInput,
    GenerateReportResult,
    PrepareRunInput,
    PrepareRunResult,
    RiftXRunWorkflow,
    RunAgentCycleActivityInput,
    RunAgentCycleActivityResult,
    RuntimeYieldReason,
    RunWorkflowInput,
    RunWorkflowResult,
    RunWorkflowStatus,
    WorkflowPhase,
)


@dataclass
class FakeActivities:
    cycle_results: deque[RunAgentCycleActivityResult]
    cycle_inputs: list[RunAgentCycleActivityInput] = field(default_factory=list)
    compact_inputs: list[CompactContextInput] = field(default_factory=list)
    cleanup_inputs: list[CleanupRunInput] = field(default_factory=list)
    prepared_runs: list[str] = field(default_factory=list)

    @activity.defn(name="prepare_run_activity")
    async def prepare(self, input: PrepareRunInput) -> PrepareRunResult:
        self.prepared_runs.append(input.run_id)
        return PrepareRunResult(run_id=input.run_id)

    @activity.defn(name="run_agent_cycle_activity")
    async def run_agent_cycle(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        self.cycle_inputs.append(input)
        result = self.cycle_results.popleft()
        return RunAgentCycleActivityResult(
            run_id=input.run_id,
            session_id=input.session_id,
            cycle_id=input.cycle_id,
            yield_reason=result.yield_reason,
            waiting_object_id=result.waiting_object_id,
            checkpoint_id=result.checkpoint_id,
        )

    @activity.defn(name="compact_context_activity")
    async def compact(self, input: CompactContextInput) -> CompactContextResult:
        self.compact_inputs.append(input)
        return CompactContextResult(compacted=True, retained_items=input.max_history_items)

    @activity.defn(name="generate_report_activity")
    async def generate_report(self, input: GenerateReportInput) -> GenerateReportResult:
        return GenerateReportResult(report_id=f"report-{input.run_id}")

    @activity.defn(name="cleanup_run_activity")
    async def cleanup(self, input: CleanupRunInput) -> CleanupRunResult:
        self.cleanup_inputs.append(input)
        return CleanupRunResult()

    def registered(self) -> list[object]:
        return [
            self.prepare,
            self.run_agent_cycle,
            self.compact,
            self.generate_report,
            self.cleanup,
        ]


@dataclass
class RetryOnceActivities(FakeActivities):
    attempts: int = 0
    launched_cycle_ids: set[str] = field(default_factory=set)

    @activity.defn(name="run_agent_cycle_activity")
    async def run_agent_cycle(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        self.cycle_inputs.append(input)
        self.attempts += 1
        self.launched_cycle_ids.add(input.cycle_id)
        if self.attempts == 1:
            raise ApplicationError("retry once")
        result = self.cycle_results.popleft()
        return RunAgentCycleActivityResult(
            run_id=input.run_id,
            session_id=input.session_id,
            cycle_id=input.cycle_id,
            yield_reason=result.yield_reason,
        )


def cycle_result(
    reason: RuntimeYieldReason,
    *,
    waiting_object_id: str | None = None,
    checkpoint_id: str | None = None,
) -> RunAgentCycleActivityResult:
    return RunAgentCycleActivityResult(
        run_id="placeholder",
        session_id="placeholder",
        cycle_id="placeholder",
        yield_reason=reason,
        waiting_object_id=waiting_object_id,
        checkpoint_id=checkpoint_id,
    )


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


async def _environment() -> WorkflowEnvironment:
    return await WorkflowEnvironment.start_time_skipping()


async def test_tool_running_survives_worker_restart_and_replays() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(
                    RuntimeYieldReason.TOOL_RUNNING,
                    waiting_object_id="execution-1",
                    checkpoint_id="provider-state-1",
                ),
                cycle_result(RuntimeYieldReason.RUN_COMPLETED),
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
            RunWorkflowInput(run_id="run-1", session_id="session-1"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        waiting = await _wait_for_phase(handle, WorkflowPhase.AGENT_CYCLE)
        for _ in range(100):
            waiting = await handle.query(RiftXRunWorkflow.get_status)
            if waiting.yield_reason is RuntimeYieldReason.TOOL_RUNNING:
                break
            await asyncio.sleep(0.01)
        assert waiting.waiting_object_id == "execution-1"
        assert waiting.checkpoint_id == "provider-state-1"
        await handle.signal(RiftXRunWorkflow.pause)
        await _wait_for_phase(handle, WorkflowPhase.PAUSED)
        await handle.signal(RiftXRunWorkflow.execution_completed, "execution-1")

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
    assert result.session_id == "session-1"
    assert len(activities.cycle_inputs) == 2
    assert activities.cycle_inputs[1].completed_execution_id == "execution-1"
    assert activities.cycle_inputs[0].cycle_id != activities.cycle_inputs[1].cycle_id

    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_approval_and_user_input_signals_forward_only_persisted_ids() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(
                    RuntimeYieldReason.APPROVAL_REQUIRED,
                    waiting_object_id="approval-1",
                ),
                cycle_result(RuntimeYieldReason.USER_INPUT_REQUIRED),
                cycle_result(RuntimeYieldReason.RUN_COMPLETED),
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
            RunWorkflowInput(run_id="run-signals", session_id="session-signals"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, WorkflowPhase.WAITING_APPROVAL)
        await handle.signal(RiftXRunWorkflow.approve, "approval-1")
        await _wait_for_phase(handle, WorkflowPhase.WAITING_INPUT)
        await handle.signal(RiftXRunWorkflow.user_input, "message-1")
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert activities.cycle_inputs[1].approval_id == "approval-1"
    assert activities.cycle_inputs[2].latest_user_message_id == "message-1"
    assert activities.cycle_inputs[1].completed_execution_id is None
    await environment.shutdown()


async def test_activity_retry_reuses_cycle_id_and_does_not_duplicate_execution() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = RetryOnceActivities(
        cycle_results=deque([cycle_result(RuntimeYieldReason.RUN_COMPLETED)])
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
            RunWorkflowInput(run_id="run-retry", session_id="session-retry"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result.phase is WorkflowPhase.COMPLETED
    assert len(activities.cycle_inputs) == 2
    assert activities.cycle_inputs[0].cycle_id == activities.cycle_inputs[1].cycle_id
    assert len(activities.launched_cycle_ids) == 1
    await environment.shutdown()


async def test_pause_resume_and_cancel_are_durable_control_signals() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(RuntimeYieldReason.USER_INPUT_REQUIRED),
                cycle_result(RuntimeYieldReason.RUN_COMPLETED),
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
            RunWorkflowInput(run_id="run-control", session_id="session-control"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, WorkflowPhase.WAITING_INPUT)
        await handle.signal(RiftXRunWorkflow.pause)
        paused = await _wait_for_phase(handle, WorkflowPhase.PAUSED)
        assert paused.paused is True
        await handle.signal(RiftXRunWorkflow.resume)
        await _wait_for_phase(handle, WorkflowPhase.WAITING_INPUT)
        await handle.signal(RiftXRunWorkflow.cancel)
        result = await handle.result()

    assert result.phase is WorkflowPhase.CANCELLED
    assert len(activities.cycle_inputs) == 1
    assert activities.cleanup_inputs[-1].final_status == "cancelled"
    await environment.shutdown()


async def test_compaction_remains_available_without_putting_context_in_workflow() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(RuntimeYieldReason.USER_INPUT_REQUIRED),
                cycle_result(RuntimeYieldReason.RUN_COMPLETED),
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
            RunWorkflowInput(run_id="run-compact", session_id="session-compact"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, WorkflowPhase.WAITING_INPUT)
        await handle.signal(RiftXRunWorkflow.compact, 12)
        for _ in range(100):
            if activities.compact_inputs:
                break
            await asyncio.sleep(0.01)
        assert activities.compact_inputs == [
            CompactContextInput(run_id="run-compact", max_history_items=12)
        ]
        await handle.signal(RiftXRunWorkflow.user_input, "message-compact")
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    await environment.shutdown()
