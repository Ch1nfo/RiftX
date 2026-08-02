from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from riftx.application.services import ResourceStopDisposition, SafetyStopResult
from riftx.domain import Engagement, Objective, Run, RunStatus
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.temporal import (
    CleanupReportFailureInput,
    CleanupRunInput,
    CleanupRunResult,
    CompactContextInput,
    CompactContextResult,
    GenerateReportInput,
    GenerateReportResult,
    PrepareConversationInput,
    PrepareConversationResult,
    PrepareRunInput,
    PrepareRunResult,
    RiftXRunWorkflow,
    RunAgentCycleActivityInput,
    RunAgentCycleActivityResult,
    RuntimeYieldReason,
    RunWorkflowInput,
    RunWorkflowResult,
    RunWorkflowStatus,
    SwitchModelInput,
    SwitchModelResult,
    WorkflowPhase,
)
from riftx.temporal.activities import RiftXActivities
from riftx.temporal.worker_runtime import TemporalWorkerRuntime


@dataclass
class FakeActivities:
    cycle_results: deque[RunAgentCycleActivityResult]
    cycle_inputs: list[RunAgentCycleActivityInput] = field(default_factory=list)
    compact_inputs: list[CompactContextInput] = field(default_factory=list)
    switch_inputs: list[SwitchModelInput] = field(default_factory=list)
    cleanup_inputs: list[CleanupRunInput] = field(default_factory=list)
    conversation_inputs: list[PrepareConversationInput] = field(default_factory=list)
    conversation_cancelled: bool = False
    preparation_cancelled: bool = False
    prepared_runs: list[str] = field(default_factory=list)
    completion_fence_results: deque[CleanupRunResult] = field(default_factory=deque)
    report_failure_cleanup_inputs: list[CleanupReportFailureInput] = field(default_factory=list)

    @activity.defn(name="prepare_conversation_activity")
    async def prepare_conversation(
        self,
        input: PrepareConversationInput,
    ) -> PrepareConversationResult:
        self.conversation_inputs.append(input)
        return PrepareConversationResult(
            run_id=input.run_id,
            cancelled=self.conversation_cancelled,
        )

    @activity.defn(name="prepare_run_activity")
    async def prepare(self, input: PrepareRunInput) -> PrepareRunResult:
        self.prepared_runs.append(input.run_id)
        return PrepareRunResult(run_id=input.run_id, prepared=not self.preparation_cancelled)

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
        return CompactContextResult(
            compacted=True,
            retained_items=input.max_history_items,
            checkpoint_id=input.checkpoint_id,
        )

    @activity.defn(name="switch_model_activity")
    async def switch_model(self, input: SwitchModelInput) -> SwitchModelResult:
        self.switch_inputs.append(input)
        return SwitchModelResult(
            checkpoint_id=input.checkpoint_id,
            previous_model_profile="model-a",
            model_profile=input.model_profile,
            context_compilation_id="compilation-model-switch",
        )

    @activity.defn(name="generate_report_activity")
    async def generate_report(self, input: GenerateReportInput) -> GenerateReportResult:
        return GenerateReportResult(report_id=f"report-{input.run_id}")

    @activity.defn(name="cleanup_report_failure_activity")
    async def cleanup_report_failure(
        self,
        input: CleanupReportFailureInput,
    ) -> CleanupRunResult:
        self.report_failure_cleanup_inputs.append(input)
        return await self.cleanup(CleanupRunInput(run_id=input.run_id, final_status="completed"))

    @activity.defn(name="cleanup_run_activity")
    async def cleanup(self, input: CleanupRunInput) -> CleanupRunResult:
        self.cleanup_inputs.append(input)
        if input.completion_fence and self.completion_fence_results:
            return self.completion_fence_results.popleft()
        return CleanupRunResult()

    def registered(self) -> list[object]:
        return [
            self.prepare_conversation,
            self.prepare,
            self.run_agent_cycle,
            self.compact,
            self.switch_model,
            self.generate_report,
            self.cleanup_report_failure,
            self.cleanup,
        ]


# This test-only Workflow only produces a pre-patch history. Pytest importlib
# mode gives this module a ``tests.*`` name that is not importable when the
# standalone ``pytest`` entry point omits the repository root from sys.path.
# Keep the producer local; the production Workflow replay below remains
# sandboxed and is the compatibility assertion this fixture exists to exercise.
@workflow.defn(name="RiftXRunWorkflow", sandboxed=False)
class _PreDurableCleanupRetryWorkflow(RiftXRunWorkflow):
    """History producer for the version before durable cleanup retries."""

    @workflow.run
    async def run(self, input: RunWorkflowInput) -> RunWorkflowResult:
        return await super().run(input)

    async def _cleanup(
        self,
        final_status: str,
        *,
        completion_fence: bool = False,
        consumed_user_message_ids: list[str] | None = None,
        defer_cleanup_event: bool = False,
    ) -> CleanupRunResult:
        resume_phase = self._phase
        self._phase = WorkflowPhase.CLEANUP
        result = await workflow.execute_activity(
            "cleanup_run_activity",
            CleanupRunInput(
                run_id=self._run_id,
                final_status=final_status,
                completion_fence=completion_fence,
                consumed_user_message_ids=list(consumed_user_message_ids or ()),
                defer_cleanup_event=defer_cleanup_event,
            ),
            result_type=CleanupRunResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=3,
            ),
        )
        self._phase = resume_phase
        return result


# This producer records histories from before completion/approval signals moved
# from last-write-wins scalar slots to matched queues. It intentionally omits
# the production patch marker; replay by RiftXRunWorkflow must therefore take
# the legacy branch. As above, only the test producer disables sandboxing.
@workflow.defn(name="RiftXRunWorkflow", sandboxed=False)
class _PreMatchedSignalQueuesWorkflow(RiftXRunWorkflow):
    @workflow.run
    async def run(self, input: RunWorkflowInput) -> RunWorkflowResult:
        return await super().run(input)

    def _matched_signal_queues_enabled(self) -> bool:
        return False


@workflow.defn(name="RiftXRunWorkflow", sandboxed=False)
class _PreReportFailureCleanupWorkflow(RiftXRunWorkflow):
    """Produce a failed-report history from before durable cleanup recovery."""

    @workflow.run
    async def run(self, input: RunWorkflowInput) -> RunWorkflowResult:
        return await super().run(input)

    @staticmethod
    def _report_failure_cleanup_enabled() -> bool:
        return False


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


@dataclass
class RetryInitialPreparationActivities(FakeActivities):
    conversation_attempts: int = 0

    @activity.defn(name="prepare_conversation_activity")
    async def prepare_conversation(
        self,
        input: PrepareConversationInput,
    ) -> PrepareConversationResult:
        self.conversation_inputs.append(input)
        self.conversation_attempts += 1
        if self.conversation_attempts <= 4:
            raise ApplicationError("run remains locally paused")
        return PrepareConversationResult(run_id=input.run_id)


@dataclass
class RetryCleanupActivities(FakeActivities):
    cleanup_attempts: int = 0

    @activity.defn(name="cleanup_run_activity")
    async def cleanup(self, input: CleanupRunInput) -> CleanupRunResult:
        self.cleanup_inputs.append(input)
        self.cleanup_attempts += 1
        if self.cleanup_attempts <= 4:
            raise ApplicationError(
                "owner Runner has not acknowledged stop",
                type="cleanup_stop_unconfirmed",
            )
        return CleanupRunResult()


@dataclass
class GatedFirstCycleActivities(FakeActivities):
    first_cycle_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_first_cycle: asyncio.Event = field(default_factory=asyncio.Event)

    @activity.defn(name="run_agent_cycle_activity")
    async def run_agent_cycle(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        self.cycle_inputs.append(input)
        if len(self.cycle_inputs) == 1:
            self.first_cycle_started.set()
            await self.release_first_cycle.wait()
        result = self.cycle_results.popleft()
        return RunAgentCycleActivityResult(
            run_id=input.run_id,
            session_id=input.session_id,
            cycle_id=input.cycle_id,
            yield_reason=result.yield_reason,
        )


@dataclass
class GatedCompletionFenceActivities(FakeActivities):
    completion_fence_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_completion_fence: asyncio.Event = field(default_factory=asyncio.Event)

    @activity.defn(name="cleanup_run_activity")
    async def cleanup(self, input: CleanupRunInput) -> CleanupRunResult:
        self.cleanup_inputs.append(input)
        if input.completion_fence and len(self.cleanup_inputs) == 1:
            self.completion_fence_started.set()
            await self.release_completion_fence.wait()
            return CleanupRunResult(
                cleaned=False,
                pending_user_message_ids=["message-2"],
            )
        return CleanupRunResult()


@dataclass
class BlockingCompactionActivities(FakeActivities):
    compaction_started: asyncio.Event = field(default_factory=asyncio.Event)

    @activity.defn(name="compact_context_activity")
    async def compact(self, input: CompactContextInput) -> CompactContextResult:
        self.compact_inputs.append(input)
        self.compaction_started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


@dataclass
class BlockingCycleActivities(FakeActivities):
    cycle_started: asyncio.Event = field(default_factory=asyncio.Event)
    cycle_cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    @activity.defn(name="run_agent_cycle_activity")
    async def run_agent_cycle(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        self.cycle_inputs.append(input)
        self.cycle_started.set()
        try:
            while True:
                activity.heartbeat("blocking-cycle")
                await asyncio.sleep(0.01)
        finally:
            self.cycle_cancelled.set()


@dataclass
class AlwaysFailCycleActivities(FakeActivities):
    attempts: int = 0

    @activity.defn(name="run_agent_cycle_activity")
    async def run_agent_cycle(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        self.cycle_inputs.append(input)
        self.attempts += 1
        raise ApplicationError("cycle failed")


@dataclass
class AlwaysFailReportActivities(FakeActivities):
    report_attempts: int = 0

    @activity.defn(name="generate_report_activity")
    async def generate_report(self, input: GenerateReportInput) -> GenerateReportResult:
        assert input.run_id
        self.report_attempts += 1
        raise ApplicationError("report generation failed")


@dataclass
class GatedUncleanFailedCleanupActivities(FakeActivities):
    failed_cleanup_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_failed_cleanup: asyncio.Event = field(default_factory=asyncio.Event)
    failed_cleanup_attempts: int = 0

    @activity.defn(name="cleanup_run_activity")
    async def cleanup(self, input: CleanupRunInput) -> CleanupRunResult:
        self.cleanup_inputs.append(input)
        if input.final_status == "failed":
            self.failed_cleanup_attempts += 1
            if self.failed_cleanup_attempts == 1:
                self.failed_cleanup_started.set()
                await self.release_failed_cleanup.wait()
                return CleanupRunResult(cleaned=False)
        return CleanupRunResult()


@dataclass
class AlwaysFailCycleWithGatedCleanupActivities(GatedUncleanFailedCleanupActivities):
    attempts: int = 0

    @activity.defn(name="run_agent_cycle_activity")
    async def run_agent_cycle(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        self.cycle_inputs.append(input)
        self.attempts += 1
        raise ApplicationError("cycle failed")


@dataclass
class ToggleCleanupSafetyStopper:
    acknowledge: bool = False
    attempts: int = 0

    async def stop_run(self, run_id: str, *, drain: bool = True) -> SafetyStopResult:
        assert run_id
        assert drain is True
        self.attempts += 1
        empty = ResourceStopDisposition((), {}, {}, {}, {})
        browser = ResourceStopDisposition(
            attempted_ids=("browser-1",),
            node_ids={"browser-1": "worker-local"},
            observed_statuses={
                "browser-1": "closed" if self.acknowledge else "active",
            },
            confirmed_statuses={"browser-1": "closed"} if self.acknowledge else {},
            failures={} if self.acknowledge else {"browser-1": "owner ACK pending"},
        )
        return SafetyStopResult(
            resources={
                "executions": empty,
                "browser_sessions": browser,
                "target_http_requests": empty,
            }
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


async def test_report_failure_runs_deferred_cleanup_before_workflow_fails() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = AlwaysFailReportActivities(
        cycle_results=deque([cycle_result(RuntimeYieldReason.RUN_COMPLETED)])
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
            RunWorkflowInput(run_id="run-report-failed", session_id="session-report-failed"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    assert activities.report_attempts == 3
    assert activities.report_failure_cleanup_inputs == [
        CleanupReportFailureInput(run_id="run-report-failed")
    ]
    assert activities.cleanup_inputs[0].completion_fence is True
    assert activities.cleanup_inputs[0].defer_cleanup_event is True
    assert activities.cleanup_inputs[-1] == CleanupRunInput(
        run_id="run-report-failed",
        final_status="completed",
    )
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_pre_report_failure_cleanup_history_replays_without_new_commands() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = AlwaysFailReportActivities(
        cycle_results=deque([cycle_result(RuntimeYieldReason.RUN_COMPLETED)])
    )

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[_PreReportFailureCleanupWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            _PreReportFailureCleanupWorkflow.run,
            RunWorkflowInput(run_id="run-report-legacy", session_id="session-report-legacy"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    assert activities.report_attempts == 3
    assert activities.report_failure_cleanup_inputs == []
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


async def test_matched_signal_queue_patch_replays_scalar_completion_history() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(
                    RuntimeYieldReason.TOOL_RUNNING,
                    waiting_object_id="execution-1",
                ),
                cycle_result(RuntimeYieldReason.RUN_COMPLETED),
            ]
        )
    )

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[_PreMatchedSignalQueuesWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            _PreMatchedSignalQueuesWorkflow.run,
            RunWorkflowInput(
                run_id="run-legacy-completion-slot",
                session_id="session-legacy-completion-slot",
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        for _ in range(100):
            waiting = await handle.query(RiftXRunWorkflow.get_status)
            if waiting.waiting_object_id == "execution-1":
                break
            await asyncio.sleep(0.01)
        assert waiting.waiting_object_id == "execution-1"

    # With no Worker polling, both signals are handled in the same Workflow
    # task. Legacy scalar semantics let the unrelated final signal overwrite
    # the matching one, so this activation must not schedule another Cycle.
    await handle.signal(RiftXRunWorkflow.execution_completed, "execution-1")
    await handle.signal(RiftXRunWorkflow.execution_completed, "execution-unrelated")

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[_PreMatchedSignalQueuesWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        waiting = await handle.query(RiftXRunWorkflow.get_status)
        assert waiting.waiting_object_id == "execution-1"
        assert len(activities.cycle_inputs) == 1

    await handle.signal(RiftXRunWorkflow.execution_completed, "execution-1")
    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[_PreMatchedSignalQueuesWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert len(activities.cycle_inputs) == 2
    assert activities.cycle_inputs[1].completed_execution_id == "execution-1"
    history = await handle.fetch_history()
    completion_signal_indexes = [
        index
        for index, event in enumerate(history.events)
        if event.event_type is EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
        and event.workflow_execution_signaled_event_attributes.signal_name == "execution_completed"
    ]
    assert len(completion_signal_indexes) == 3
    first_signal, second_signal = completion_signal_indexes[:2]
    assert not any(
        event.event_type is EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
        for event in history.events[first_signal + 1 : second_signal]
    )
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_matched_signal_queue_patch_replays_approval_without_waiting_id() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(RuntimeYieldReason.APPROVAL_REQUIRED),
                cycle_result(RuntimeYieldReason.RUN_COMPLETED),
            ]
        )
    )

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[_PreMatchedSignalQueuesWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            _PreMatchedSignalQueuesWorkflow.run,
            RunWorkflowInput(
                run_id="run-legacy-approval-without-id",
                session_id="session-legacy-approval-without-id",
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        waiting = await _wait_for_phase(handle, WorkflowPhase.WAITING_APPROVAL)
        assert waiting.waiting_object_id is None
        await handle.signal(RiftXRunWorkflow.approve, "approval-legacy")
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert len(activities.cycle_inputs) == 2
    assert activities.cycle_inputs[1].approval_id == "approval-legacy"
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_second_approval_signal_can_arrive_first_and_survive_restart() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(
                    RuntimeYieldReason.APPROVAL_REQUIRED,
                    waiting_object_id="approval-1",
                ),
                cycle_result(
                    RuntimeYieldReason.APPROVAL_REQUIRED,
                    waiting_object_id="approval-2",
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
            RunWorkflowInput(
                run_id="run-approval-order",
                session_id="session-approval-order",
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, WorkflowPhase.WAITING_APPROVAL)
        await handle.signal(RiftXRunWorkflow.approve, "approval-2")
        waiting = await handle.query(RiftXRunWorkflow.get_status)
        assert waiting.waiting_object_id == "approval-1"
        assert len(activities.cycle_inputs) == 1

    # Both decisions are durable signals even while no Worker is available.
    await handle.signal(RiftXRunWorkflow.approve, "approval-1")

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert [item.approval_id for item in activities.cycle_inputs] == [
        None,
        "approval-1",
        "approval-2",
    ]
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_second_execution_signal_can_arrive_first_without_being_lost() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(
                    RuntimeYieldReason.TOOL_RUNNING,
                    waiting_object_id="execution-1",
                ),
                cycle_result(
                    RuntimeYieldReason.TOOL_RUNNING,
                    waiting_object_id="execution-2",
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
            RunWorkflowInput(
                run_id="run-execution-order",
                session_id="session-execution-order",
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        for _ in range(100):
            waiting = await handle.query(RiftXRunWorkflow.get_status)
            if waiting.waiting_object_id == "execution-1":
                break
            await asyncio.sleep(0.01)
        assert waiting.waiting_object_id == "execution-1"

        await handle.signal(RiftXRunWorkflow.execution_completed, "execution-2")
        waiting = await handle.query(RiftXRunWorkflow.get_status)
        assert waiting.waiting_object_id == "execution-1"
        assert len(activities.cycle_inputs) == 1

        await handle.signal(RiftXRunWorkflow.execution_completed, "execution-1")
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert [item.completed_execution_id for item in activities.cycle_inputs] == [
        None,
        "execution-1",
        "execution-2",
    ]
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_new_workflow_waits_for_initial_instruction_before_preparing() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque([cycle_result(RuntimeYieldReason.RUN_COMPLETED)])
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
            RunWorkflowInput(
                run_id="run-awaiting-instruction",
                session_id="session-awaiting-instruction",
                await_initial_instruction=True,
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, WorkflowPhase.WAITING_INPUT)
        assert activities.conversation_inputs == [
            PrepareConversationInput(
                run_id="run-awaiting-instruction",
                session_id="session-awaiting-instruction",
            )
        ]
        assert activities.prepared_runs == []
        assert activities.cycle_inputs == []

        await handle.signal(RiftXRunWorkflow.user_input, "message-first-instruction")
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert activities.prepared_runs == ["run-awaiting-instruction"]
    assert len(activities.cycle_inputs) == 1
    assert activities.cycle_inputs[0].latest_user_message_id == "message-first-instruction"
    await environment.shutdown()


async def test_signal_with_start_survives_extended_local_preparation_pause() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = RetryInitialPreparationActivities(
        cycle_results=deque([cycle_result(RuntimeYieldReason.RUN_COMPLETED)])
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
            RunWorkflowInput(
                run_id="run-signal-with-start-paused",
                session_id="session-signal-with-start-paused",
                await_initial_instruction=True,
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
            start_signal="user_input",
            start_signal_args=["message-first-instruction"],
        )
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert activities.conversation_attempts == 5
    assert activities.prepared_runs == ["run-signal-with-start-paused"]
    assert len(activities.cycle_inputs) == 1
    assert activities.cycle_inputs[0].latest_user_message_id == "message-first-instruction"
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_user_input_signals_are_fifo_and_duplicate_event_ids_run_only_once() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = GatedFirstCycleActivities(
        cycle_results=deque(
            [
                cycle_result(RuntimeYieldReason.RUN_COMPLETED),
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
            RunWorkflowInput(
                run_id="run-message-fifo",
                session_id="session-message-fifo",
                await_initial_instruction=True,
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
            start_signal="user_input",
            start_signal_args=["message-1"],
        )
        await asyncio.wait_for(activities.first_cycle_started.wait(), timeout=5)
        # message-1 has already been handed to the activity. Retrying the same
        # ambiguous signal must remain a no-op, while a distinct fast-follow
        # instruction is retained for the next cycle.
        await handle.signal(RiftXRunWorkflow.user_input, "message-1")
        await handle.signal(RiftXRunWorkflow.user_input, "message-2")
        await handle.signal(RiftXRunWorkflow.user_input, "message-2")
        activities.release_first_cycle.set()
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert [item.latest_user_message_id for item in activities.cycle_inputs] == [
        "message-1",
        "message-2",
    ]
    assert all(item.defer_run_completion for item in activities.cycle_inputs)
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_user_input_signalled_during_completion_fence_runs_before_close() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = GatedCompletionFenceActivities(
        cycle_results=deque(
            [
                cycle_result(RuntimeYieldReason.RUN_COMPLETED),
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
            RunWorkflowInput(
                run_id="run-message-during-completion-fence",
                session_id="session-message-during-completion-fence",
                await_initial_instruction=True,
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
            start_signal="user_input",
            start_signal_args=["message-1"],
        )
        await asyncio.wait_for(activities.completion_fence_started.wait(), timeout=5)

        # The first Cycle already observed an empty in-memory queue. Inject the
        # next persisted event while the atomic DB fence Activity is in flight;
        # its result reconciles that event even if signal delivery and Activity
        # completion share the same Workflow activation.
        await handle.signal(RiftXRunWorkflow.user_input, "message-2")
        activities.release_completion_fence.set()
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert [item.latest_user_message_id for item in activities.cycle_inputs] == [
        "message-1",
        "message-2",
    ]
    completion_inputs = [item for item in activities.cleanup_inputs if item.completion_fence]
    assert [item.consumed_user_message_ids for item in completion_inputs] == [
        ["message-1"],
        ["message-1", "message-2"],
    ]
    assert all(item.defer_cleanup_event for item in completion_inputs)
    assert activities.cleanup_inputs[-1] == CleanupRunInput(
        run_id="run-message-during-completion-fence",
        final_status="completed",
    )
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_local_cancel_between_conversation_and_preparation_cleans_up_as_cancelled() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(),
        preparation_cancelled=True,
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
            RunWorkflowInput(
                run_id="run-cancel-before-preparation",
                session_id="session-cancel-before-preparation",
                await_initial_instruction=True,
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
            start_signal="user_input",
            start_signal_args=["message-first-instruction"],
        )

    assert result.phase is WorkflowPhase.CANCELLED
    assert activities.prepared_runs == ["run-cancel-before-preparation"]
    assert activities.cycle_inputs == []
    assert activities.cleanup_inputs == [
        CleanupRunInput(run_id="run-cancel-before-preparation", final_status="cancelled")
    ]
    await environment.shutdown()


async def test_new_workflow_can_cancel_before_initial_instruction() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(cycle_results=deque())

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(
                run_id="run-cancel-before-instruction",
                session_id="session-cancel-before-instruction",
                await_initial_instruction=True,
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, WorkflowPhase.WAITING_INPUT)
        await handle.signal(RiftXRunWorkflow.cancel)
        result = await handle.result()

    assert result.phase is WorkflowPhase.CANCELLED
    assert activities.prepared_runs == []
    assert activities.cycle_inputs == []
    assert activities.cleanup_inputs == [
        CleanupRunInput(
            run_id="run-cancel-before-instruction",
            final_status="cancelled",
        )
    ]
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_new_workflow_honors_cancelled_run_before_waiting() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(),
        conversation_cancelled=True,
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
            RunWorkflowInput(
                run_id="run-already-cancelled",
                session_id="session-already-cancelled",
                await_initial_instruction=True,
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result.phase is WorkflowPhase.CANCELLED
    assert activities.prepared_runs == []
    assert activities.cycle_inputs == []
    assert activities.cleanup_inputs == [
        CleanupRunInput(run_id="run-already-cancelled", final_status="cancelled")
    ]
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


async def test_cleanup_retries_past_normal_activity_limit_until_owner_ack() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = RetryCleanupActivities(
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
            RunWorkflowInput(run_id="run-cleanup-retry", session_id="session-cleanup-retry"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result.phase is WorkflowPhase.COMPLETED
    assert activities.cleanup_attempts >= 5
    assert activities.cleanup_inputs[0].final_status == "completed"
    await environment.shutdown()


async def test_durable_cleanup_retry_patch_replays_pre_patch_history() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque([cycle_result(RuntimeYieldReason.RUN_COMPLETED)])
    )

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[_PreDurableCleanupRetryWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            _PreDurableCleanupRetryWorkflow.run,
            RunWorkflowInput(
                run_id="run-pre-durable-cleanup-retry",
                session_id="session-pre-durable-cleanup-retry",
            ),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_pre_patch_cleanup_exhaustion_keeps_intent_for_worker_recovery(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-cleanup-exhaustion.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Legacy cleanup exhaustion")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    run_id = "run-legacy-cleanup-exhaustion"
    await runs.create(
        Run(
            kind="general",
            id=run_id,
            engagement_id="engagement-1",
            node_id="worker-local",
            objective=Objective(description="Recover an exhausted legacy cleanup"),
            workspace_path=str(tmp_path / "workspaces" / run_id),
        )
    )
    await runs.update_status(run_id, RunStatus.PREPARING)
    await runs.update_status(run_id, RunStatus.RUNNING)

    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    fake = FakeActivities(cycle_results=deque([cycle_result(RuntimeYieldReason.FATAL_FAILURE)]))
    safety_stopper = ToggleCleanupSafetyStopper()
    production_activities = RiftXActivities(
        run_repository=runs,
        event_repository=events,
        tool_registry=AsyncMock(),
        safety_stopper=safety_stopper,  # type: ignore[arg-type]
        agent_cycle=AsyncMock(),
        approval_recorder=AsyncMock(),
        report_service=AsyncMock(),
        session_factory=database.session_factory,
    )

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[_PreDurableCleanupRetryWorkflow],
        activities=[
            fake.prepare_conversation,
            fake.prepare,
            fake.run_agent_cycle,
            fake.compact,
            fake.switch_model,
            fake.generate_report,
            production_activities.cleanup_run_activity,
        ],
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            _PreDurableCleanupRetryWorkflow.run,
            RunWorkflowInput(run_id=run_id, session_id=f"{run_id}:primary"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    fenced = await runs.get(run_id)
    intent = await runs.get_finalization_intent(run_id)
    assert safety_stopper.attempts == 3
    assert fenced is not None and fenced.status is RunStatus.COMPLETING
    assert intent is not None and intent.target is RunStatus.FAILED
    assert intent.defer_cleanup_event is False
    assert not any(
        event.event_type == "run.cleaned_up" for event in await events.list_after(run_id)
    )

    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)

    safety_stopper.acknowledge = True
    runtime = TemporalWorkerRuntime(
        worker=AsyncMock(),
        database=database,
        process_supervisor=AsyncMock(),
        terminal_supervisor=AsyncMock(),
        model_provider=AsyncMock(),
        node_service=AsyncMock(),
        node_id="worker-local",
        heartbeat_interval_seconds=0.01,
        run_repository=runs,
        event_repository=events,
        safety_stopper=safety_stopper,  # type: ignore[arg-type]
    )
    reconciler = asyncio.create_task(runtime._safety_reconciler_loop())
    try:
        for _ in range(100):
            recovered = await runs.get(run_id)
            if recovered is not None and recovered.status is RunStatus.FAILED:
                break
            await asyncio.sleep(0.01)
        assert recovered is not None and recovered.status is RunStatus.FAILED
    finally:
        reconciler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reconciler

    timeline = await events.list_after(run_id)
    cleaned = [event for event in timeline if event.event_type == "run.cleaned_up"]
    reconciled = [event for event in timeline if event.event_type == "run.cleanup_reconciled"]
    assert safety_stopper.attempts >= 4
    assert len(cleaned) == 1
    assert reconciled[-1].payload["finalization_target"] == "failed"
    assert reconciled[-1].payload["owner"] == "worker"
    await environment.shutdown()
    await database.dispose()


async def test_pause_cancels_in_flight_agent_cycle_activity() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = BlockingCycleActivities(cycle_results=deque())

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
        max_heartbeat_throttle_interval=timedelta(milliseconds=50),
        default_heartbeat_throttle_interval=timedelta(milliseconds=50),
    ):
        handle = await environment.client.start_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(run_id="run-pause-active", session_id="session-pause-active"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await asyncio.wait_for(activities.cycle_started.wait(), timeout=5)
        await handle.signal(RiftXRunWorkflow.pause)
        await asyncio.wait_for(activities.cycle_cancelled.wait(), timeout=5)
        paused = await _wait_for_phase(handle, WorkflowPhase.PAUSED)
        assert paused.paused is True
        assert len(activities.cycle_inputs) == 1

        await handle.signal(RiftXRunWorkflow.cancel)
        result = await handle.result()

    assert result.phase is WorkflowPhase.CANCELLED
    assert activities.cleanup_inputs[-1].final_status == "cancelled"
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_cancel_cancels_in_flight_agent_cycle_activity() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = BlockingCycleActivities(cycle_results=deque())

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
        max_heartbeat_throttle_interval=timedelta(milliseconds=50),
        default_heartbeat_throttle_interval=timedelta(milliseconds=50),
    ):
        handle = await environment.client.start_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(run_id="run-cancel-active", session_id="session-cancel-active"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await asyncio.wait_for(activities.cycle_started.wait(), timeout=5)
        await handle.signal(RiftXRunWorkflow.cancel)
        await asyncio.wait_for(activities.cycle_cancelled.wait(), timeout=5)
        result = await handle.result()

    assert result.phase is WorkflowPhase.CANCELLED
    assert len(activities.cycle_inputs) == 1
    assert activities.cleanup_inputs[-1].final_status == "cancelled"
    await environment.shutdown()


async def test_cancel_current_execution_releases_tool_wait() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(
                    RuntimeYieldReason.TOOL_RUNNING,
                    waiting_object_id="execution-cancelled",
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
            RunWorkflowInput(run_id="run-cancel-current", session_id="session-cancel-current"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        for _ in range(100):
            status = await handle.query(RiftXRunWorkflow.get_status)
            if status.waiting_object_id == "execution-cancelled":
                break
            await asyncio.sleep(0.01)
        assert status.waiting_object_id == "execution-cancelled"

        await handle.signal(RiftXRunWorkflow.cancel_current_execution)
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert len(activities.cycle_inputs) == 2
    assert activities.cycle_inputs[1].completed_execution_id == "execution-cancelled"
    await environment.shutdown()


async def test_pause_releases_tool_wait_before_resume() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = FakeActivities(
        cycle_results=deque(
            [
                cycle_result(
                    RuntimeYieldReason.TOOL_RUNNING,
                    waiting_object_id="execution-paused",
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
            RunWorkflowInput(run_id="run-pause-tool", session_id="session-pause-tool"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        for _ in range(100):
            status = await handle.query(RiftXRunWorkflow.get_status)
            if status.waiting_object_id == "execution-paused":
                break
            await asyncio.sleep(0.01)
        assert status.waiting_object_id == "execution-paused"

        await handle.signal(RiftXRunWorkflow.pause)
        paused = await _wait_for_phase(handle, WorkflowPhase.PAUSED)
        assert paused.paused is True
        await handle.signal(RiftXRunWorkflow.resume)
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert len(activities.cycle_inputs) == 2
    assert activities.cycle_inputs[1].completed_execution_id == "execution-paused"
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_final_agent_cycle_failure_runs_failed_cleanup() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = AlwaysFailCycleActivities(cycle_results=deque())

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(run_id="run-cycle-failure", session_id="session-cycle-failure"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    assert activities.attempts == 3
    assert activities.cleanup_inputs[-1].final_status == "failed"
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_fatal_failure_cleanup_pauses_until_resume_then_finishes_failed() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = GatedUncleanFailedCleanupActivities(
        cycle_results=deque([cycle_result(RuntimeYieldReason.FATAL_FAILURE)])
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
            RunWorkflowInput(run_id="run-fatal-pause", session_id="session-fatal-pause"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await asyncio.wait_for(activities.failed_cleanup_started.wait(), timeout=5)
        await handle.signal(RiftXRunWorkflow.pause)
        activities.release_failed_cleanup.set()
        paused = await _wait_for_phase(handle, WorkflowPhase.PAUSED)
        assert paused.paused is True
        assert activities.failed_cleanup_attempts == 1

        await handle.signal(RiftXRunWorkflow.resume)
        result = await handle.result()

    assert result.phase is WorkflowPhase.FAILED
    assert activities.failed_cleanup_attempts == 2
    assert [item.final_status for item in activities.cleanup_inputs] == ["failed", "failed"]
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()


async def test_failed_cycle_cleanup_cancel_wins_and_finishes_cancelled() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    activities = AlwaysFailCycleWithGatedCleanupActivities(cycle_results=deque())

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(run_id="run-cycle-cancel", session_id="session-cycle-cancel"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await asyncio.wait_for(activities.failed_cleanup_started.wait(), timeout=5)
        await handle.signal(RiftXRunWorkflow.cancel)
        activities.release_failed_cleanup.set()
        result = await handle.result()

    assert result.phase is WorkflowPhase.CANCELLED
    assert activities.attempts == 3
    assert activities.failed_cleanup_attempts == 1
    cleanup_statuses = [item.final_status for item in activities.cleanup_inputs]
    assert cleanup_statuses[0] == "failed"
    assert cleanup_statuses.count("cancelled") >= 1
    history = await handle.fetch_history()
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
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
        assert len(activities.compact_inputs) == 1
        compact_input = activities.compact_inputs[0]
        assert compact_input.run_id == "run-compact"
        assert compact_input.session_id == "session-compact"
        assert compact_input.max_history_items == 12
        assert compact_input.checkpoint_id is not None
        await handle.signal(RiftXRunWorkflow.user_input, "message-compact")
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    await environment.shutdown()


async def test_model_switch_checkpoints_then_waits_for_original_user_input() -> None:
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
            RunWorkflowInput(run_id="run-switch", session_id="session-switch"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, WorkflowPhase.WAITING_INPUT)
        await handle.signal(RiftXRunWorkflow.switch_model, "model-b")
        for _ in range(100):
            if activities.switch_inputs:
                break
            await asyncio.sleep(0.01)
        assert len(activities.switch_inputs) == 1
        switch_input = activities.switch_inputs[0]
        assert switch_input.run_id == "run-switch"
        assert switch_input.session_id == "session-switch"
        assert switch_input.model_profile == "model-b"
        assert switch_input.checkpoint_id
        await asyncio.sleep(0.05)
        assert len(activities.cycle_inputs) == 1
        await handle.signal(RiftXRunWorkflow.user_input, "message-after-switch")
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert len(activities.cycle_inputs) == 2
    assert activities.cycle_inputs[1].latest_user_message_id == "message-after-switch"
    await environment.shutdown()


async def test_new_worker_retries_inflight_compaction_with_same_checkpoint_id() -> None:
    environment = await _environment()
    task_queue = f"riftx-test-{uuid4()}"
    crashing = BlockingCompactionActivities(
        cycle_results=deque([cycle_result(RuntimeYieldReason.USER_INPUT_REQUIRED)])
    )
    handle: WorkflowHandle[RunWorkflowResult, RunWorkflowStatus]

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=crashing.registered(),
        max_cached_workflows=0,
    ):
        handle = await environment.client.start_workflow(
            RiftXRunWorkflow.run,
            RunWorkflowInput(run_id="run-worker-crash", session_id="session-worker-crash"),
            id=f"workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await _wait_for_phase(handle, WorkflowPhase.WAITING_INPUT)
        await handle.signal(RiftXRunWorkflow.compact, 8)
        await asyncio.wait_for(crashing.compaction_started.wait(), timeout=5)

    assert len(crashing.compact_inputs) == 1
    checkpoint_id = crashing.compact_inputs[0].checkpoint_id
    assert checkpoint_id is not None
    recovered = FakeActivities(
        cycle_results=deque([cycle_result(RuntimeYieldReason.RUN_COMPLETED)])
    )
    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=recovered.registered(),
        max_cached_workflows=0,
    ):
        for _ in range(300):
            if recovered.compact_inputs:
                break
            await asyncio.sleep(0.01)
        assert len(recovered.compact_inputs) == 1
        assert recovered.compact_inputs[0].checkpoint_id == checkpoint_id
        await handle.signal(RiftXRunWorkflow.user_input, "message-after-recovery")
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert recovered.cycle_inputs[0].run_id == "run-worker-crash"
    assert recovered.cycle_inputs[0].session_id == "session-worker-crash"
    await environment.shutdown()
