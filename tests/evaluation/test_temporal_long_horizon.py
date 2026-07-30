from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from temporalio import activity
from temporalio.client import WorkflowHandle
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
    SwitchModelInput,
    SwitchModelResult,
    WorkflowPhase,
)


@dataclass
class LongHorizonActivities:
    cycle_results: deque[RunAgentCycleActivityResult]
    cycle_inputs: list[RunAgentCycleActivityInput] = field(default_factory=list)
    compact_inputs: list[CompactContextInput] = field(default_factory=list)
    switch_inputs: list[SwitchModelInput] = field(default_factory=list)

    @activity.defn(name="prepare_run_activity")
    async def prepare(self, item: PrepareRunInput) -> PrepareRunResult:
        return PrepareRunResult(run_id=item.run_id)

    @activity.defn(name="run_agent_cycle_activity")
    async def run_cycle(
        self,
        item: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        self.cycle_inputs.append(item)
        result = self.cycle_results.popleft()
        return RunAgentCycleActivityResult(
            run_id=item.run_id,
            session_id=item.session_id,
            cycle_id=item.cycle_id,
            yield_reason=result.yield_reason,
            waiting_object_id=result.waiting_object_id,
            checkpoint_id=result.checkpoint_id,
        )

    @activity.defn(name="compact_context_activity")
    async def compact(self, item: CompactContextInput) -> CompactContextResult:
        self.compact_inputs.append(item)
        return CompactContextResult(
            compacted=True,
            retained_items=item.max_history_items,
            checkpoint_id=item.checkpoint_id,
        )

    @activity.defn(name="switch_model_activity")
    async def switch_model(self, item: SwitchModelInput) -> SwitchModelResult:
        self.switch_inputs.append(item)
        return SwitchModelResult(
            checkpoint_id=item.checkpoint_id,
            previous_model_profile="model-a",
            model_profile=item.model_profile,
            context_compilation_id="qa-switch-compilation",
        )

    @activity.defn(name="generate_report_activity")
    async def report(self, item: GenerateReportInput) -> GenerateReportResult:
        return GenerateReportResult(report_id=f"report-{item.run_id}")

    @activity.defn(name="cleanup_run_activity")
    async def cleanup(self, item: CleanupRunInput) -> CleanupRunResult:
        return CleanupRunResult()

    def registered(self) -> list[object]:
        return [
            self.prepare,
            self.run_cycle,
            self.compact,
            self.switch_model,
            self.report,
            self.cleanup,
        ]


def _result(reason: RuntimeYieldReason, waiting_object_id: str | None = None):
    return RunAgentCycleActivityResult(
        run_id="placeholder",
        session_id="placeholder",
        cycle_id="placeholder",
        yield_reason=reason,
        waiting_object_id=waiting_object_id,
    )


async def _wait_for_execution(
    handle: WorkflowHandle[RunWorkflowResult, RunWorkflowStatus],
    execution_id: str,
) -> RunWorkflowStatus:
    for _ in range(500):
        status = await handle.query(RiftXRunWorkflow.get_status)
        if (
            status.phase is WorkflowPhase.AGENT_CYCLE
            and status.yield_reason is RuntimeYieldReason.TOOL_RUNNING
            and status.waiting_object_id == execution_id
        ):
            return status
        await asyncio.sleep(0.005)
    raise AssertionError(f"workflow did not wait for {execution_id}")


async def _wait_for_count(items: list[object], count: int) -> None:
    for _ in range(500):
        if len(items) >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"activity count did not reach {count}")


def _history_payload_sizes(value: object) -> list[int]:
    sizes: list[int] = []
    if isinstance(value, dict):
        data = value.get("data")
        metadata = value.get("metadata")
        if isinstance(data, str) and isinstance(metadata, dict):
            try:
                sizes.append(len(base64.b64decode(data, validate=True)))
            except ValueError:
                sizes.append(len(data.encode()))
        for child in value.values():
            sizes.extend(_history_payload_sizes(child))
    elif isinstance(value, list):
        for child in value:
            sizes.extend(_history_payload_sizes(child))
    return sizes


async def test_temporal_history_stays_identifier_only_across_100_tools_and_restart() -> None:
    environment = await WorkflowEnvironment.start_time_skipping()
    task_queue = f"riftx-qa-01-{uuid4()}"
    activities = LongHorizonActivities(
        cycle_results=deque(
            [
                _result(RuntimeYieldReason.TOOL_RUNNING, f"qa-execution-{index:03d}")
                for index in range(100)
            ]
            + [_result(RuntimeYieldReason.RUN_COMPLETED)]
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
            RunWorkflowInput(run_id="qa-temporal-run", session_id="qa-temporal-session"),
            id=f"qa-01-{uuid4()}",
            task_queue=task_queue,
        )
        for index in range(50):
            execution_id = f"qa-execution-{index:03d}"
            await _wait_for_execution(handle, execution_id)
            if index == 24:
                await handle.signal(RiftXRunWorkflow.compact, 25)
            if index == 49:
                await handle.signal(RiftXRunWorkflow.switch_model, "model-b")
            await handle.signal(RiftXRunWorkflow.execution_completed, execution_id)
            if index == 24:
                await _wait_for_count(activities.compact_inputs, 1)
            if index == 49:
                await _wait_for_count(activities.switch_inputs, 1)
        # Ensure the Workflow is durably waiting on the next persisted ID before
        # stopping the first Worker process.
        await _wait_for_execution(handle, "qa-execution-050")

    async with Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[RiftXRunWorkflow],
        activities=activities.registered(),
        max_cached_workflows=0,
    ):
        for index in range(50, 100):
            execution_id = f"qa-execution-{index:03d}"
            await _wait_for_execution(handle, execution_id)
            if index == 74:
                await handle.signal(RiftXRunWorkflow.compact, 12)
            await handle.signal(RiftXRunWorkflow.execution_completed, execution_id)
            if index == 74:
                await _wait_for_count(activities.compact_inputs, 2)
        result = await handle.result()

    assert result.phase is WorkflowPhase.COMPLETED
    assert result.report_id == "report-qa-temporal-run"
    assert len(activities.cycle_inputs) == 101
    assert len(activities.compact_inputs) == 2
    assert len(activities.switch_inputs) == 1
    assert activities.switch_inputs[0].model_profile == "model-b"
    resumed_ids = [
        item.completed_execution_id
        for item in activities.cycle_inputs[1:]
        if item.completed_execution_id is not None
    ]
    assert resumed_ids == [f"qa-execution-{index:03d}" for index in range(100)]

    # Temporal payload contracts carry identifiers only. Large Tool/Web/Browser
    # content remains in SQL/Artifact storage and therefore cannot enter History.
    payloads = [
        json.dumps(asdict(item), separators=(",", ":")).encode()
        for item in activities.cycle_inputs
    ]
    assert max(map(len, payloads)) < 1024
    history = await handle.fetch_history()
    history_dict = history.to_json_dict()
    history_json = json.dumps(history_dict, separators=(",", ":"))
    history_payload_sizes = _history_payload_sizes(history_dict)
    assert history_payload_sizes
    assert max(history_payload_sizes) < 64 * 1024
    assert "UNTRUSTED_EXTERNAL_CONTENT" not in history_json
    assert "x" * (1024 * 1024) not in history_json
    await Replayer(workflows=[RiftXRunWorkflow]).replay_workflow(history)
    await environment.shutdown()
