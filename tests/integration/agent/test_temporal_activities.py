from __future__ import annotations

import asyncio
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from temporalio.exceptions import ApplicationError

from riftx.agent import (
    AgentCycleOutput,
    AgentCycleResult,
    AgentCycleStatus,
    AgentInterruption,
    RiftXAgentContext,
    RiftXDatabaseSession,
)
from riftx.application.services import (
    ApprovalRequestRecorder,
    ArtifactApplicationService,
    ReportApplicationService,
    RunSafetyStopService,
)
from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    InvalidStateTransitionError,
    Objective,
    Run,
    RunStatus,
)
from riftx.persistence import (
    Database,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyReportRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import ProcessSupervisor, RunnerPaths
from riftx.temporal import (
    AgentCycleActivityInput,
    AgentCycleActivityStatus,
    CleanupReportFailureInput,
    CleanupRunInput,
    CompactContextInput,
    GenerateReportInput,
    PrepareConversationInput,
    PrepareRunInput,
)
from riftx.temporal.activities import RiftXActivities
from riftx.tools import ToolRegistry

FIXTURE = Path(__file__).parents[2] / "tools" / "fixtures" / "fake_tool.py"


@dataclass
class FakeAgentCycle:
    results: deque[AgentCycleResult]
    calls: list[dict[str, object]] = field(default_factory=list)

    async def run(
        self,
        context: RiftXAgentContext,
        *,
        input_text: str | None = None,
        checkpoint_id: str | None = None,
        approval_decisions: dict[str, bool] | None = None,
    ) -> AgentCycleResult:
        self.calls.append(
            {
                "context": context,
                "input_text": input_text,
                "checkpoint_id": checkpoint_id,
                "approval_decisions": approval_decisions,
            }
        )
        return self.results.popleft()


@dataclass(frozen=True)
class EmptyStopResult:
    attempted_ids: tuple[str, ...] = ()
    node_ids: dict[str, str] = field(default_factory=dict)
    observed_statuses: dict[str, str] = field(default_factory=dict)
    confirmed_statuses: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


class EmptyRunResourceStopper:
    async def stop_run(self, run_id: str) -> EmptyStopResult:
        assert run_id
        return EmptyStopResult()


class MutableRunResourceStopper:
    def __init__(self, runs: SQLAlchemyRunRepository, result: EmptyStopResult) -> None:
        self._runs = runs
        self.result = result
        self.observed_run_statuses: list[RunStatus] = []

    async def stop_run(self, run_id: str) -> EmptyStopResult:
        run = await self._runs.get(run_id)
        assert run is not None
        self.observed_run_statuses.append(run.status)
        return self.result


class ToggleExecutionAckRunner:
    def __init__(self, executions: SQLAlchemyExecutionRepository) -> None:
        self._executions = executions
        self.acknowledge = False
        self.observed_statuses: list[ExecutionStatus] = []

    async def cancel(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        assert execution is not None
        self.observed_statuses.append(execution.status)
        if self.acknowledge:
            execution.transition_to(ExecutionStatus.CANCELLED)
            await self._executions.save(execution)
        return execution


async def _runtime(
    tmp_path: Path,
    cycle: FakeAgentCycle,
) -> tuple[
    Database,
    SQLAlchemyRunRepository,
    SQLAlchemyRunEventRepository,
    RiftXActivities,
    ProcessSupervisor,
]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Temporal activities")
    )
    run_repository = SQLAlchemyRunRepository(database.session_factory)
    await run_repository.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Temporal activity test"),
            workspace_path=str(tmp_path / "workspace" / "run-1"),
        )
    )
    config_path = tmp_path / "tools.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "registered_only",
                "tools": {
                    "custom": {
                        "command": [sys.executable, str(FIXTURE)],
                        "capabilities": ["verify"],
                    }
                },
            }
        )
    )
    registry = ToolRegistry(config_path, node_id="node-1")
    execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = ProcessSupervisor(
        execution_repository,
        RunnerPaths(tmp_path / "state"),
        termination_grace_seconds=0.1,
    )
    event_repository = SQLAlchemyRunEventRepository(database.session_factory)
    approval_repository = SQLAlchemyApprovalRepository(database.session_factory)
    artifact_repository = SQLAlchemyArtifactRepository(database.session_factory)
    finding_repository = SQLAlchemyFindingRepository(database.session_factory)
    report_repository = SQLAlchemyReportRepository(database.session_factory)
    artifact_service = ArtifactApplicationService(
        run_repository=run_repository,
        execution_repository=execution_repository,
        artifact_repository=artifact_repository,
        event_repository=event_repository,
        paths=RunnerPaths(tmp_path / "state"),
    )
    activities = RiftXActivities(
        run_repository=run_repository,
        event_repository=event_repository,
        tool_registry=registry,
        safety_stopper=RunSafetyStopService(
            execution_repository=execution_repository,
            execution_runner=supervisor,
            resource_stoppers={
                "browser_sessions": EmptyRunResourceStopper(),
                "target_http_requests": EmptyRunResourceStopper(),
            },
            execution_cancel_timeout_seconds=0.1,
            execution_cancel_poll_seconds=0.001,
        ),
        agent_cycle=cycle,
        approval_recorder=ApprovalRequestRecorder(
            approval_repository=approval_repository,
            event_repository=event_repository,
            tool_registry=registry,
        ),
        report_service=ReportApplicationService(
            run_repository=run_repository,
            finding_repository=finding_repository,
            artifact_repository=artifact_repository,
            report_repository=report_repository,
            event_repository=event_repository,
            artifact_service=artifact_service,
        ),
        session_factory=database.session_factory,
    )
    return database, run_repository, event_repository, activities, supervisor


async def test_conversation_context_waits_before_preparing_run(tmp_path: Path) -> None:
    cycle = FakeAgentCycle(deque())
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)

    context = await activities.prepare_conversation_activity(
        PrepareConversationInput(run_id="run-1", session_id="run-1:primary")
    )
    retried_context = await activities.prepare_conversation_activity(
        PrepareConversationInput(run_id="run-1", session_id="run-1:primary")
    )
    waiting_run = await run_repository.get("run-1")
    waiting_events = await events.list_after("run-1")

    assert context.run_id == "run-1"
    assert retried_context.run_id == "run-1"
    assert waiting_run is not None and waiting_run.status is RunStatus.WAITING_USER
    context_events = [
        event for event in waiting_events if event.event_type == "conversation.context_ready"
    ]
    assert len(context_events) == 1
    assert context_events[0].payload == {
        "session_id": "run-1:primary",
        "status": "waiting_user",
        "objective": "Temporal activity test",
        "success_criteria": [],
        "entry_points": [],
        "scope": {
            "cidrs": [],
            "ips": [],
            "domains": [],
            "url_prefixes": [],
            "asset_tags": [],
            "exclusions": [],
            "starts_at": None,
            "ends_at": None,
        },
        "approval_mode": "balanced",
        "model_profile": None,
        "agent_started": False,
    }

    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    running_run = await run_repository.get("run-1")
    status_changes = [
        event.payload
        for event in await events.list_after("run-1")
        if event.event_type == "run.status_changed"
    ]

    assert running_run is not None and running_run.status is RunStatus.RUNNING
    assert status_changes[:3] == [
        {"from": "created", "to": "waiting_user"},
        {"from": "waiting_user", "to": "preparing"},
        {"from": "preparing", "to": "running"},
    ]

    deferred_cleanup = await activities.cleanup_run_activity(
        CleanupRunInput(run_id="run-1", final_status="cancelled")
    )
    cancelling_run = await run_repository.get("run-1")
    assert deferred_cleanup.cleaned is False
    assert cancelling_run is not None and cancelling_run.status is RunStatus.CANCELLING
    assert not any(
        event.event_type == "run.cleaned_up" for event in await events.list_after("run-1")
    )

    # Only the Control Plane's complete safety-stop gate may make cancellation
    # terminal.  Once that durable state exists, Workflow cleanup can record its
    # own completion without inventing physical-stop evidence.
    await run_repository.update_status("run-1", RunStatus.CANCELLED)
    confirmed_cleanup = await activities.cleanup_run_activity(
        CleanupRunInput(run_id="run-1", final_status="cancelled")
    )
    assert confirmed_cleanup.cleaned is True
    assert any(event.event_type == "run.cleaned_up" for event in await events.list_after("run-1"))
    await supervisor.close()
    await database.dispose()


async def test_conversation_context_honors_cancel_before_activity_runs(tmp_path: Path) -> None:
    cycle = FakeAgentCycle(deque())
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await run_repository.update_status("run-1", RunStatus.CANCELLING)
    await run_repository.update_status("run-1", RunStatus.CANCELLED)

    result = await activities.prepare_conversation_activity(
        PrepareConversationInput(run_id="run-1", session_id="run-1:primary")
    )

    assert result.cancelled is True
    assert not any(
        event.event_type == "conversation.context_ready"
        for event in await events.list_after("run-1")
    )
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("fence_status", [RunStatus.PAUSING, RunStatus.CANCELLING])
async def test_failed_cleanup_fence_rejects_pause_but_yields_to_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fence_status: RunStatus,
) -> None:
    cycle = FakeAgentCycle(deque())
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    original_stop_run = activities._safety_stopper.stop_run

    async def gated_execution_cleanup(run_id: str, *, drain: bool = True) -> object:
        assert run_id == "run-1"
        cleanup_started.set()
        await release_cleanup.wait()
        return await original_stop_run(run_id, drain=drain)

    monkeypatch.setattr(activities._safety_stopper, "stop_run", gated_execution_cleanup)
    cleanup_task = asyncio.create_task(
        activities.cleanup_run_activity(CleanupRunInput(run_id="run-1", final_status="failed"))
    )
    await cleanup_started.wait()
    if fence_status is RunStatus.PAUSING:
        with pytest.raises(InvalidStateTransitionError):
            await run_repository.update_status("run-1", fence_status)
    else:
        await run_repository.update_status("run-1", fence_status)
    release_cleanup.set()

    result = await cleanup_task
    fenced_run = await run_repository.get("run-1")
    run_events = await events.list_after("run-1")

    intent = await run_repository.get_finalization_intent("run-1")
    assert intent is not None and intent.target is RunStatus.FAILED
    if fence_status is RunStatus.PAUSING:
        assert result.cleaned is True
        assert fenced_run is not None and fenced_run.status is RunStatus.FAILED
        assert any(event.event_type == "run.cleaned_up" for event in run_events)
    else:
        assert result.cleaned is False
        assert fenced_run is not None and fenced_run.status is RunStatus.CANCELLING
        assert not any(event.event_type == "run.cleaned_up" for event in run_events)
        assert not any(
            event.event_type == "run.status_changed" and event.payload.get("to") == "failed"
            for event in run_events
        )
    await supervisor.close()
    await database.dispose()


async def test_failed_cleanup_records_intent_while_pause_fence_owns_run(tmp_path: Path) -> None:
    cycle = FakeAgentCycle(deque())
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    await run_repository.update_status("run-1", RunStatus.PAUSING)

    result = await activities.cleanup_run_activity(
        CleanupRunInput(run_id="run-1", final_status="failed")
    )

    fenced_run = await run_repository.get("run-1")
    intent = await run_repository.get_finalization_intent("run-1")
    assert result.cleaned is False
    assert fenced_run is not None and fenced_run.status is RunStatus.PAUSING
    assert intent is not None and intent.target is RunStatus.FAILED
    assert not any(
        event.event_type == "run.cleaned_up" for event in await events.list_after("run-1")
    )
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("fence_status", [RunStatus.PAUSING, RunStatus.CANCELLING])
async def test_failed_cleanup_cannot_overwrite_a_safety_fence_committed_after_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fence_status: RunStatus,
) -> None:
    cycle = FakeAgentCycle(deque())
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    cleanup_reached_finalization_commit = asyncio.Event()
    release_finalization_commit = asyncio.Event()
    original_update_status = run_repository.update_status
    original_commit_finalization = run_repository.commit_finalization

    async def gated_commit_finalization(
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool = False,
    ) -> Run:
        if target is RunStatus.FAILED:
            cleanup_reached_finalization_commit.set()
            await release_finalization_commit.wait()
        return await original_commit_finalization(
            run_id,
            target,
            defer_cleanup_event=defer_cleanup_event,
        )

    monkeypatch.setattr(run_repository, "commit_finalization", gated_commit_finalization)
    cleanup_task = asyncio.create_task(
        activities.cleanup_run_activity(CleanupRunInput(run_id="run-1", final_status="failed"))
    )
    await cleanup_reached_finalization_commit.wait()
    if fence_status is RunStatus.PAUSING:
        with pytest.raises(InvalidStateTransitionError):
            await original_update_status("run-1", fence_status)
    else:
        await original_update_status("run-1", fence_status)
    release_finalization_commit.set()

    result = await cleanup_task
    fenced_run = await run_repository.get("run-1")
    run_events = await events.list_after("run-1")

    if fence_status is RunStatus.PAUSING:
        assert result.cleaned is True
        assert fenced_run is not None and fenced_run.status is RunStatus.FAILED
        assert any(event.event_type == "run.cleaned_up" for event in run_events)
    else:
        assert result.cleaned is False
        assert fenced_run is not None and fenced_run.status is fence_status
        assert not any(event.event_type == "run.cleaned_up" for event in run_events)
        assert not any(
            event.event_type == "run.status_changed" and event.payload.get("to") == "failed"
            for event in run_events
        )
    await supervisor.close()
    await database.dispose()


async def test_initial_preparation_is_retryable_while_locally_paused(tmp_path: Path) -> None:
    cycle = FakeAgentCycle(deque())
    database, run_repository, _, activities, supervisor = await _runtime(tmp_path, cycle)
    await run_repository.update_status("run-1", RunStatus.WAITING_USER)
    await run_repository.update_status("run-1", RunStatus.PAUSING)
    await run_repository.update_status("run-1", RunStatus.PAUSED)

    with pytest.raises(ApplicationError) as conversation_paused:
        await activities.prepare_conversation_activity(
            PrepareConversationInput(run_id="run-1", session_id="run-1:primary")
        )
    with pytest.raises(ApplicationError) as preparation_paused:
        await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))

    assert conversation_paused.value.non_retryable is False
    assert preparation_paused.value.non_retryable is False

    await run_repository.update_status("run-1", RunStatus.WAITING_USER)
    conversation = await activities.prepare_conversation_activity(
        PrepareConversationInput(run_id="run-1", session_id="run-1:primary")
    )
    prepared = await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))

    assert conversation.cancelled is False
    assert prepared.prepared is True
    resumed = await run_repository.get("run-1")
    assert resumed is not None and resumed.status is RunStatus.RUNNING
    await supervisor.close()
    await database.dispose()


async def test_temporal_activities_prepare_interrupt_resume_and_complete(tmp_path: Path) -> None:
    cycle = FakeAgentCycle(
        deque(
            [
                AgentCycleResult(
                    status=AgentCycleStatus.INTERRUPTED,
                    checkpoint_id="checkpoint-1",
                    interruptions=[AgentInterruption(call_id="call-1", tool_name="run_shell")],
                ),
                AgentCycleResult(
                    status=AgentCycleStatus.COMPLETED,
                    output=AgentCycleOutput(
                        assistant_message="Done",
                        plan_summary="Complete after approval",
                        run_summary="Verified",
                        completed=True,
                    ),
                ),
            ]
        )
    )
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)

    prepared = await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    first = await activities.agent_cycle_activity(
        AgentCycleActivityInput(run_id="run-1", agent_step_id="step-1")
    )
    waiting_run = await run_repository.get("run-1")
    second = await activities.agent_cycle_activity(
        AgentCycleActivityInput(
            run_id="run-1",
            agent_step_id="step-2",
            checkpoint_id="checkpoint-1",
            approval_decisions={"call-1": True},
            user_messages=["continue"],
        )
    )
    completed_run = await run_repository.get("run-1")

    assert prepared.prepared is True
    assert (tmp_path / "workspace" / "run-1").is_dir()
    assert first.status is AgentCycleActivityStatus.WAITING_APPROVAL
    assert first.pending_approvals[0].call_id == "call-1"
    assert waiting_run is not None and waiting_run.status is RunStatus.WAITING_APPROVAL
    assert second.status is AgentCycleActivityStatus.COMPLETED
    assert completed_run is not None and completed_run.status is RunStatus.COMPLETED
    assert cycle.calls[1]["checkpoint_id"] == "checkpoint-1"
    assert cycle.calls[1]["approval_decisions"] == {"call-1": True}
    assert cycle.calls[1]["input_text"] == "continue"
    event_types = [event.event_type for event in await events.list_after("run-1")]
    assert "run.prepared" in event_types

    report_result = await activities.generate_report_activity(GenerateReportInput(run_id="run-1"))
    assert report_result.report_id is not None
    report_result_retry = await activities.generate_report_activity(
        GenerateReportInput(run_id="run-1")
    )
    assert report_result_retry.report_id == report_result.report_id
    generated_events = [
        event
        for event in await events.list_after("run-1")
        if event.event_type == "report.generated"
    ]
    assert len(generated_events) == 3
    await activities.cleanup_run_activity(CleanupRunInput(run_id="run-1", final_status="completed"))
    await supervisor.close()
    await database.dispose()


async def test_legacy_activity_retry_resumes_stop_gate_without_rerunning_model(
    tmp_path: Path,
) -> None:
    cycle = FakeAgentCycle(
        deque(
            [
                AgentCycleResult(
                    status=AgentCycleStatus.COMPLETED,
                    output=AgentCycleOutput(
                        assistant_message="Done",
                        plan_summary="Completed",
                        run_summary="Verified",
                        completed=True,
                    ),
                )
            ]
        )
    )
    database, runs, _, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    stopper = MutableRunResourceStopper(
        runs,
        EmptyStopResult(
            attempted_ids=("browser-1",),
            node_ids={"browser-1": "node-1"},
            observed_statuses={"browser-1": "active"},
            failures={"browser-1": "owner ACK pending"},
        ),
    )
    activities._safety_stopper._resource_stoppers["browser_sessions"] = stopper
    activities._safety_stopper._resource_stop_max_passes = 1
    input = AgentCycleActivityInput(run_id="run-1", agent_step_id="legacy-step")

    with pytest.raises(ApplicationError) as unconfirmed:
        await activities.agent_cycle_activity(input)
    fenced = await runs.get("run-1")
    stopper.result = EmptyStopResult(
        attempted_ids=("browser-1",),
        node_ids={"browser-1": "node-1"},
        observed_statuses={"browser-1": "closed"},
        confirmed_statuses={"browser-1": "closed"},
    )
    result = await activities.agent_cycle_activity(input)
    completed = await runs.get("run-1")

    assert unconfirmed.value.type == "cleanup_stop_unconfirmed"
    assert fenced is not None and fenced.status is RunStatus.COMPLETING
    assert result.status is AgentCycleActivityStatus.COMPLETED
    assert completed is not None and completed.status is RunStatus.COMPLETED
    assert len(cycle.calls) == 1
    assert stopper.observed_run_statuses == [RunStatus.COMPLETING, RunStatus.COMPLETING]
    await supervisor.close()
    await database.dispose()


async def test_temporal_activity_defers_completion_until_workflow_cleanup(
    tmp_path: Path,
) -> None:
    cycle = FakeAgentCycle(
        deque(
            [
                AgentCycleResult(
                    status=AgentCycleStatus.COMPLETED,
                    output=AgentCycleOutput(
                        assistant_message="Done",
                        plan_summary="Completed",
                        run_summary="Verified",
                        completed=True,
                    ),
                )
            ]
        )
    )
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))

    result = await activities.agent_cycle_activity(
        AgentCycleActivityInput(
            run_id="run-1",
            agent_step_id="step-deferred-completion",
            defer_run_completion=True,
        )
    )
    still_running = await run_repository.get("run-1")
    late_message = await events.append_user_message("run-1", "One more instruction")
    blocked_completion = await activities.cleanup_run_activity(
        CleanupRunInput(
            run_id="run-1",
            final_status="completed",
            completion_fence=True,
            consumed_user_message_ids=[],
        )
    )
    after_blocked_completion = await run_repository.get("run-1")
    completed_cleanup = await activities.cleanup_run_activity(
        CleanupRunInput(
            run_id="run-1",
            final_status="completed",
            completion_fence=True,
            consumed_user_message_ids=[late_message.id],
        )
    )
    completed = await run_repository.get("run-1")
    transitions = [
        event.payload
        for event in await events.list_after("run-1")
        if event.event_type == "run.status_changed"
    ]

    assert result.status is AgentCycleActivityStatus.COMPLETED
    assert still_running is not None and still_running.status is RunStatus.RUNNING
    assert blocked_completion.cleaned is False
    assert blocked_completion.pending_user_message_ids == [late_message.id]
    assert after_blocked_completion is not None
    assert after_blocked_completion.status is RunStatus.RUNNING
    assert completed_cleanup.cleaned is True
    assert completed is not None and completed.status is RunStatus.COMPLETED
    assert transitions[-2:] == [
        {"from": "running", "to": "completing"},
        {"from": "completing", "to": "completed"},
    ]
    await supervisor.close()
    await database.dispose()


async def test_report_failure_cleanup_releases_deferred_boundary_idempotently(
    tmp_path: Path,
) -> None:
    cycle = FakeAgentCycle(deque())
    database, runs, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))

    fenced = await activities.cleanup_run_activity(
        CleanupRunInput(
            run_id="run-1",
            final_status="completed",
            completion_fence=True,
            defer_cleanup_event=True,
        )
    )
    before_failure = await events.list_after("run-1")
    first = await activities.cleanup_report_failure_activity(
        CleanupReportFailureInput(run_id="run-1")
    )
    retried = await activities.cleanup_report_failure_activity(
        CleanupReportFailureInput(run_id="run-1")
    )

    run = await runs.get("run-1")
    timeline = await events.list_after("run-1")
    report_failures = [
        event for event in timeline if event.event_type == "report.generation_failed"
    ]
    cleaned = [event for event in timeline if event.event_type == "run.cleaned_up"]
    assert fenced.cleaned is True
    assert all(event.event_type != "run.cleaned_up" for event in before_failure)
    assert first.cleaned is True
    assert retried.cleaned is True
    assert run is not None and run.status is RunStatus.COMPLETED
    assert len(report_failures) == 1
    assert report_failures[0].payload == {
        "version": 1,
        "stage": "generate_report_activity",
        "outcome": "failed",
    }
    assert len(cleaned) == 1
    assert report_failures[0].sequence < cleaned[0].sequence
    await supervisor.close()
    await database.dispose()


async def test_legacy_completion_activity_cannot_overwrite_a_confirmed_pause(
    tmp_path: Path,
) -> None:
    cycle = FakeAgentCycle(deque())
    database, runs, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    await runs.update_status("run-1", RunStatus.PAUSING)
    await runs.update_status("run-1", RunStatus.PAUSED)

    finalized = await activities._finalize_compat_run("run-1", RunStatus.COMPLETED)

    current = await runs.get("run-1")
    assert finalized is False
    assert current is not None and current.status is RunStatus.PAUSED
    assert await runs.get_finalization_intent("run-1") is None
    assert all(event.event_type != "run.cleaned_up" for event in await events.list_after("run-1"))
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("final_status", ["completed", "failed"])
@pytest.mark.parametrize(
    ("resource_type", "active_status", "confirmed_status"),
    [
        ("browser_sessions", "active", "closed"),
        ("target_http_requests", "executing", "cancelled"),
    ],
)
async def test_cleanup_requires_each_resource_family_ack_before_terminal_status(
    tmp_path: Path,
    final_status: str,
    resource_type: str,
    active_status: str,
    confirmed_status: str,
) -> None:
    cycle = FakeAgentCycle(deque())
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    resource_id = f"{resource_type}-1"
    stopper = MutableRunResourceStopper(
        run_repository,
        EmptyStopResult(
            attempted_ids=(resource_id,),
            node_ids={resource_id: "node-1"},
            observed_statuses={resource_id: active_status},
            failures={resource_id: "owner has not acknowledged stop"},
        ),
    )
    activities._safety_stopper._resource_stoppers[resource_type] = stopper
    activities._safety_stopper._resource_stop_max_passes = 1

    with pytest.raises(ApplicationError) as unconfirmed:
        await activities.cleanup_run_activity(
            CleanupRunInput(run_id="run-1", final_status=final_status)
        )

    fenced = await run_repository.get("run-1")
    assert unconfirmed.value.type == "cleanup_stop_unconfirmed"
    assert fenced is not None and fenced.status is RunStatus.COMPLETING
    assert stopper.observed_run_statuses == [RunStatus.COMPLETING]
    assert not any(
        event.event_type == "run.cleaned_up" for event in await events.list_after("run-1")
    )

    stopper.result = EmptyStopResult(
        attempted_ids=(resource_id,),
        node_ids={resource_id: "node-1"},
        observed_statuses={resource_id: confirmed_status},
        confirmed_statuses={resource_id: confirmed_status},
    )
    completed = await activities.cleanup_run_activity(
        CleanupRunInput(run_id="run-1", final_status=final_status)
    )
    terminal = await run_repository.get("run-1")
    assert completed.cleaned is True
    assert terminal is not None and terminal.status is RunStatus(final_status)
    timeline = await events.list_after("run-1")
    cleaned = [event for event in timeline if event.event_type == "run.cleaned_up"]
    assert len(cleaned) == 1
    assert cleaned[0].payload == {
        "version": 1,
        "status": final_status,
        "stop_confirmed": True,
    }
    stop_confirmed = [
        event for event in timeline if event.event_type == "run.cleanup_stop_confirmed"
    ]
    assert len(stop_confirmed) == 1
    assert stop_confirmed[0].payload["stop_resources"][resource_type]["confirmed_statuses"] == {
        resource_id: confirmed_status
    }
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("execution_status", [ExecutionStatus.FAILED, ExecutionStatus.LOST])
async def test_failed_cleanup_waits_for_failed_or_lost_execution_cancel_ack(
    tmp_path: Path,
    execution_status: ExecutionStatus,
) -> None:
    cycle = FakeAgentCycle(deque())
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    execution = Execution(
        id=f"execution-{execution_status.value}",
        execution_key=f"run-1:{execution_status.value}",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        argv=["fixture"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{execution_status.value}.stdout"),
        stderr_path=str(tmp_path / f"{execution_status.value}.stderr"),
        status=execution_status,
    )
    await executions.create_if_absent(execution)
    runner = ToggleExecutionAckRunner(executions)
    activities._safety_stopper = RunSafetyStopService(
        execution_repository=executions,
        execution_runner=runner,  # type: ignore[arg-type]
        resource_stoppers={
            "browser_sessions": EmptyRunResourceStopper(),
            "target_http_requests": EmptyRunResourceStopper(),
        },
        execution_cancel_timeout_seconds=0,
        execution_cancel_poll_seconds=0.001,
        execution_cancel_max_passes=1,
        resource_stop_max_passes=1,
    )

    with pytest.raises(ApplicationError) as unconfirmed:
        await activities.cleanup_run_activity(
            CleanupRunInput(run_id="run-1", final_status="failed")
        )
    fenced = await run_repository.get("run-1")
    assert unconfirmed.value.type == "cleanup_stop_unconfirmed"
    assert fenced is not None and fenced.status is RunStatus.COMPLETING
    assert await executions.get(execution.id) == execution
    assert not any(
        event.event_type == "run.cleaned_up" for event in await events.list_after("run-1")
    )

    runner.acknowledge = True
    result = await activities.cleanup_run_activity(
        CleanupRunInput(run_id="run-1", final_status="failed")
    )
    terminal = await run_repository.get("run-1")
    stopped = await executions.get(execution.id)
    assert result.cleaned is True
    assert terminal is not None and terminal.status is RunStatus.FAILED
    assert stopped is not None and stopped.status is ExecutionStatus.CANCELLED
    assert runner.observed_statuses == [execution_status, execution_status]
    await supervisor.close()
    await database.dispose()


async def test_compact_context_activity_keeps_latest_messages(tmp_path: Path) -> None:
    cycle = FakeAgentCycle(deque())
    database, _, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    session = RiftXDatabaseSession("run-1", database.session_factory)
    await session.add_items([{"role": "user", "content": f"message-{index}"} for index in range(5)])

    result = await activities.compact_context_activity(
        CompactContextInput(run_id="run-1", max_history_items=2)
    )

    assert result.compacted is True
    assert result.retained_items == 2
    assert await session.get_items() == [
        {"role": "user", "content": "message-3"},
        {"role": "user", "content": "message-4"},
    ]
    event_types = [event.event_type for event in await events.list_after("run-1")]
    assert "agent.context_compacted" in event_types
    await supervisor.close()
    await database.dispose()


async def test_interrupted_activity_retry_records_one_durable_approval(tmp_path: Path) -> None:
    interruption = AgentCycleResult(
        status=AgentCycleStatus.INTERRUPTED,
        checkpoint_id="checkpoint-retry",
        interruptions=[
            AgentInterruption(
                call_id="call-retry",
                tool_name="run_shell",
                arguments='{"script":"printf retry","reason":"Validate retry safety."}',
            )
        ],
    )
    cycle = FakeAgentCycle(deque([interruption, interruption]))
    database, _, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))

    await activities.agent_cycle_activity(
        AgentCycleActivityInput(run_id="run-1", agent_step_id="step-retry")
    )
    await activities.agent_cycle_activity(
        AgentCycleActivityInput(run_id="run-1", agent_step_id="step-retry")
    )

    approvals = await SQLAlchemyApprovalRepository(database.session_factory).list("run-1")
    approval_events = [
        event
        for event in await events.list_after("run-1")
        if event.event_type == "tool.approval_required"
    ]
    assert len(approvals) == 1
    assert approvals[0].command[-1] == "printf retry"
    assert approvals[0].reason == "Validate retry safety."
    assert len(approval_events) == 1

    await supervisor.close()
    await database.dispose()
