from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError, ServiceUnavailableError
from riftx.application.services import (
    ApprovalApplicationService,
    DecideApproval,
    RuntimeApprovalRequestRecorder,
)
from riftx.context import ContextCompiler, StableInstructionSource, TranscriptContextSource
from riftx.domain import (
    ApprovalStatus,
    Engagement,
    MessageType,
    Objective,
    Run,
)
from riftx.execution import DeferredExecutionDispatcher, ExecutionService
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyApprovalRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunLeaseRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyRuntimeApprovalRepository,
    SQLAlchemyToolCallIntentRepository,
    SQLAlchemyTranscriptRepository,
    SQLAlchemyUserInputRequestRepository,
)
from riftx.persistence.orm import ApprovalRecord
from riftx.runner import ProcessSupervisor, RunnerPaths
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import (
    AgentEngineEvent,
    AgentEngineEventType,
    AgentEngineState,
)
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import MinimalContextCompiler, RunCycleRequest
from riftx.runtime.types import (
    AgentSession,
    ApprovalDecision,
    ToolCallStatus,
    YieldReason,
)
from riftx.temporal.worker_runtime import _RunEventUserInputResolver


class EngineRun:
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[AgentEngineEvent]:
        for event in self._events:
            yield event

    async def suspend(self) -> AgentEngineState:
        return AgentEngineState(
            engine_type="fake",
            engine_version="1",
            provider="fake",
            model="fake-model",
            serialized_state={"pending_approval": True},
        )

    async def cancel(self) -> None:
        return None


class RestartableEngine:
    def __init__(
        self,
        initial_events: list[AgentEngineEvent],
        resume_events: list[AgentEngineEvent] | None = None,
    ) -> None:
        self._initial_events = initial_events
        self._resume_events = resume_events or []
        self.start_requests: list[object] = []
        self.resume_requests: list[object] = []

    async def start(self, request: object) -> EngineRun:
        self.start_requests.append(request)
        return EngineRun(self._initial_events)

    async def resume(self, request: object) -> EngineRun:
        self.resume_requests.append(request)
        return EngineRun(self._resume_events)


class BlockingSuspendEngineRun(EngineRun):
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        super().__init__(events)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def suspend(self) -> AgentEngineState:
        self.entered.set()
        await self.release.wait()
        return await super().suspend()


class BlockingSuspendEngine(RestartableEngine):
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        super().__init__(events)
        self.run = BlockingSuspendEngineRun(events)

    async def start(self, request: object) -> EngineRun:
        self.start_requests.append(request)
        return self.run


class RecordingWorkflow:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def approve(self, run_id: str, approval_id: str) -> None:
        self.calls.append(("approve", run_id, approval_id))

    async def reject(self, run_id: str, approval_id: str) -> None:
        self.calls.append(("reject", run_id, approval_id))


class FailOnceWorkflow(RecordingWorkflow):
    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    async def approve(self, run_id: str, approval_id: str) -> None:
        self.calls.append(("approve", run_id, approval_id))
        if not self._failed:
            self._failed = True
            raise RuntimeError("injected workflow signal failure")


def engine_event(
    sequence: int,
    event_type: AgentEngineEventType,
    **data: object,
) -> AgentEngineEvent:
    return AgentEngineEvent(sequence=sequence, event_type=event_type, data=data)


async def build_fixture(tmp_path: Path) -> dict[str, object]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'approval-runtime.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Run an approved command"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="fake-model"))
    events = SQLAlchemyRunEventRepository(database.session_factory)
    approvals = SQLAlchemyApprovalRepository(database.session_factory)
    runtime_approvals = SQLAlchemyRuntimeApprovalRepository(database.session_factory)
    intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    transcript = SQLAlchemyTranscriptRepository(database.session_factory)
    user_inputs = SQLAlchemyUserInputRequestRepository(database.session_factory)
    supervisor = ProcessSupervisor(executions, RunnerPaths(tmp_path / "runner"))
    execution_service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=intents,
        runner=supervisor,
        event_repository=events,
    )
    workflow = RecordingWorkflow()
    return {
        "database": database,
        "runs": runs,
        "sessions": sessions,
        "cycles": SQLAlchemyAgentCycleRepository(database.session_factory),
        "steps": SQLAlchemyAgentStepRepository(database.session_factory),
        "providers": SQLAlchemyProviderStateRepository(database.session_factory),
        "events": events,
        "leases": SQLAlchemyRunLeaseRepository(database.session_factory),
        "approvals": approvals,
        "runtime_approvals": runtime_approvals,
        "intents": intents,
        "executions": executions,
        "transcript": transcript,
        "user_inputs": user_inputs,
        "supervisor": supervisor,
        "execution_service": execution_service,
        "workflow": workflow,
    }


def build_coordinator(
    fixture: dict[str, object],
    engine: RestartableEngine,
    *,
    context_compiler: ContextCompiler | MinimalContextCompiler | None = None,
) -> RuntimeCoordinator:
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=fixture["intents"],
        execution_service=fixture["execution_service"],
    )
    return RuntimeCoordinator(
        run_repository=fixture["runs"],
        session_repository=fixture["sessions"],
        cycle_repository=fixture["cycles"],
        step_repository=fixture["steps"],
        provider_state_repository=fixture["providers"],
        event_repository=fixture["events"],
        lease_manager=DatabaseRunLeaseManager(fixture["leases"]),
        context_compiler=context_compiler or transcript_context_compiler(fixture),
        agent_engine=engine,
        deferred_execution_dispatcher=dispatcher,
        approval_repository=fixture["approvals"],
        runtime_approval_repository=fixture["runtime_approvals"],
        approval_recorder=RuntimeApprovalRequestRecorder(
            approval_repository=fixture["approvals"],
            runtime_repository=fixture["runtime_approvals"],
            event_repository=fixture["events"],
        ),
        transcript_repository=fixture["transcript"],
        user_input_repository=fixture["user_inputs"],
    )


def approval_events(tmp_path: Path) -> list[AgentEngineEvent]:
    return [
        engine_event(
            1,
            AgentEngineEventType.TOOL_CALL_READY,
            call_id="stable-call-1",
            tool_id="python",
            approval_level="sensitive",
            approval_required=False,
            arguments={"args": ["approved"]},
            execution={
                "node_id": "local",
                "executor_type": "process",
                "cwd": str(tmp_path),
                "argv": [sys.executable, "-c", "print('approved original snapshot')"],
            },
        ),
        engine_event(2, AgentEngineEventType.RUN_COMPLETED),
    ]


def tool_event(
    tmp_path: Path,
    sequence: int,
    *,
    call_id: str,
    label: str,
    approval_level: str,
) -> AgentEngineEvent:
    return engine_event(
        sequence,
        AgentEngineEventType.TOOL_CALL_READY,
        call_id=call_id,
        tool_id="python",
        approval_level=approval_level,
        approval_required=False,
        arguments={"label": label},
        execution={
            "node_id": "local",
            "executor_type": "process",
            "cwd": str(tmp_path),
            "argv": [sys.executable, "-c", f"print({label!r})"],
        },
    )


def completed_execution_input(
    execution_id: str,
    label: str,
    *,
    artifact_source: bool = False,
) -> dict[str, object]:
    return {
        "id": f"tool-result:{execution_id}",
        "type": "tool_result",
        "content": {"execution_id": execution_id, "output": label},
        "source_refs": (
            [f"artifact://{execution_id}/stdout"]
            if artifact_source
            else [f"execution://{execution_id}"]
        ),
        "priority": 100,
        "required": True,
        "compressible": False,
        "removable": False,
    }


def transcript_context_compiler(fixture: dict[str, object]) -> ContextCompiler:
    return ContextCompiler(
        sources=[TranscriptContextSource(fixture["transcript"])],
        stable_instruction_source=StableInstructionSource(),
    )


def approval_service(fixture: dict[str, object]) -> ApprovalApplicationService:
    return ApprovalApplicationService(
        approval_repository=fixture["approvals"],
        run_repository=fixture["runs"],
        event_repository=fixture["events"],
        workflow_client=fixture["workflow"],
        runtime_approval_repository=fixture["runtime_approvals"],
    )


@pytest.mark.parametrize(
    "fail_stage",
    ["after_public", "after_runtime", "after_grant", "after_event"],
)
async def test_runtime_approval_uow_failpoints_roll_back_every_effect(
    tmp_path: Path,
    fail_stage: str,
) -> None:
    fixture = await build_fixture(tmp_path)
    try:
        proposed = await build_coordinator(
            fixture,
            RestartableEngine(approval_events(tmp_path)),
        ).run_cycle(
            RunCycleRequest(
                run_id="run-1",
                session_id="session-1",
                worker_id=f"worker-{fail_stage}",
                cycle_id=f"cycle-{fail_stage}",
            )
        )
        approval_id = proposed.waiting_object_id
        assert approval_id is not None
        database = fixture["database"]
        assert isinstance(database, Database)

        def failpoint(stage: str) -> None:
            if stage == fail_stage:
                raise RuntimeError(f"injected failure at {stage}")

        failing_approvals = SQLAlchemyApprovalRepository(
            database.session_factory,
            decision_failpoint=failpoint,
        )
        service = ApprovalApplicationService(
            approval_repository=failing_approvals,
            run_repository=fixture["runs"],
            event_repository=fixture["events"],
            workflow_client=fixture["workflow"],
            runtime_approval_repository=fixture["runtime_approvals"],
        )
        with pytest.raises(RuntimeError, match=fail_stage):
            await service.approve(
                approval_id,
                DecideApproval(decided_by="operator", approve_for_run=True),
            )

        public = await fixture["approvals"].get(approval_id)
        runtime = await fixture["runtime_approvals"].get(approval_id)
        assert public is not None and public.status is ApprovalStatus.PENDING
        assert public.decision is None
        assert public.decision_feedback is None
        assert public.decided_by is None
        assert public.decided_at is None
        assert runtime is not None and runtime.status is ApprovalStatus.PENDING
        assert runtime.decision is None
        assert runtime.decided_by is None
        assert runtime.decided_at is None
        assert not await fixture["approvals"].is_granted("run-1", "python")
        events = await fixture["events"].list_after("run-1")
        assert not [
            event for event in events if event.event_type in {"tool.approved", "tool.rejected"}
        ]
        assert fixture["workflow"].calls == []
    finally:
        await fixture["supervisor"].close()
        await fixture["database"].dispose()


async def test_committed_runtime_decision_retries_signal_without_duplicate_effects(
    tmp_path: Path,
) -> None:
    fixture = await build_fixture(tmp_path)
    try:
        proposed = await build_coordinator(
            fixture,
            RestartableEngine(approval_events(tmp_path)),
        ).run_cycle(
            RunCycleRequest(
                run_id="run-1",
                session_id="session-1",
                worker_id="worker-signal-failure",
                cycle_id="cycle-signal-failure",
            )
        )
        approval_id = proposed.waiting_object_id
        assert approval_id is not None
        workflow = FailOnceWorkflow()
        service = ApprovalApplicationService(
            approval_repository=fixture["approvals"],
            run_repository=fixture["runs"],
            event_repository=fixture["events"],
            workflow_client=workflow,
            runtime_approval_repository=fixture["runtime_approvals"],
        )
        command = DecideApproval(
            decided_by="operator",
            reason="Approved for the remaining Run",
            approve_for_run=True,
        )

        with pytest.raises(ServiceUnavailableError) as captured:
            await service.approve(approval_id, command)
        assert captured.value.details["approval_saved"] is True
        await service.approve(approval_id, command)

        public = await fixture["approvals"].get(approval_id)
        runtime = await fixture["runtime_approvals"].get(approval_id)
        assert public is not None and runtime is not None
        assert public.decision is ApprovalDecision.APPROVE_TOOL_FOR_RUN
        assert public.decision_feedback == "Approved for the remaining Run"
        assert public.decided_at == runtime.decided_at
        assert runtime.decision is ApprovalDecision.APPROVE_TOOL_FOR_RUN
        assert await fixture["approvals"].is_granted("run-1", "python")
        events = await fixture["events"].list_after("run-1")
        decision_events = [event for event in events if event.event_type == "tool.approved"]
        assert len(decision_events) == 1
        assert decision_events[0].payload["approve_for_run"] is True
        assert workflow.calls == [
            ("approve", "run-1", approval_id),
            ("approve", "run-1", approval_id),
        ]
    finally:
        await fixture["supervisor"].close()
        await fixture["database"].dispose()


async def test_approve_executes_original_snapshot_after_worker_restart(tmp_path: Path) -> None:
    fixture = await build_fixture(tmp_path)
    first_engine = RestartableEngine(approval_events(tmp_path))
    first = await build_coordinator(fixture, first_engine).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-before-restart",
            cycle_id="cycle-proposal",
        )
    )

    assert first.yield_reason is YieldReason.APPROVAL_REQUIRED
    assert first.waiting_object_id is not None
    assert await fixture["executions"].list("run-1") == []
    runtime_request = await fixture["runtime_approvals"].get(first.waiting_object_id)
    assert runtime_request is not None
    assert runtime_request.provider_state_id == first.provider_state_id
    intent = await fixture["intents"].get(runtime_request.tool_call_intent_id)
    assert intent is not None
    assert intent.status is ToolCallStatus.WAITING_APPROVAL
    assert intent.execution_spec is not None
    assert intent.execution_spec["argv"][-1] == "print('approved original snapshot')"

    command = DecideApproval(decided_by="operator", approve_for_run=False)
    service = approval_service(fixture)
    await service.approve(runtime_request.id, command)
    with pytest.raises(ApplicationConflictError) as captured:
        await service.approve(
            runtime_request.id,
            DecideApproval(
                decided_by="retrying-browser",
                reason="must not replace the saved decision",
                approve_for_run=True,
            ),
        )
    assert captured.value.code == "runtime_approval_decision_conflict"

    saved_runtime_request = await fixture["runtime_approvals"].get(runtime_request.id)
    assert saved_runtime_request is not None
    assert saved_runtime_request.decision is ApprovalDecision.APPROVE_ONCE
    assert saved_runtime_request.decided_by == "operator"
    assert saved_runtime_request.feedback is None
    assert not await fixture["approvals"].is_granted("run-1", "python")
    assert fixture["workflow"].calls == [
        ("approve", "run-1", runtime_request.id),
    ]

    restarted_engine = RestartableEngine([])
    restarted = build_coordinator(fixture, restarted_engine)
    resumed = await restarted.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-after-restart",
            cycle_id="cycle-approved",
            approval_id=runtime_request.id,
        )
    )
    duplicate = await restarted.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-after-restart",
            cycle_id="cycle-approved-duplicate",
            approval_id=runtime_request.id,
        )
    )

    assert resumed.yield_reason is YieldReason.TOOL_RUNNING
    assert duplicate.waiting_execution_id == resumed.waiting_execution_id
    assert restarted_engine.start_requests == []
    assert restarted_engine.resume_requests == []
    assert len(await fixture["executions"].list("run-1")) == 1
    completed = await fixture["execution_service"].wait(
        resumed.waiting_execution_id,
        timeout_seconds=2,
    )
    assert completed.partial_output is not None
    assert "approved original snapshot" in completed.partial_output
    await fixture["supervisor"].close()
    await fixture["database"].dispose()


@pytest.mark.parametrize(
    (
        "status",
        "persisted_reason",
        "retry_command",
        "expected_decision",
        "expected_feedback",
        "workflow_action",
    ),
    [
        (
            ApprovalStatus.APPROVED,
            None,
            DecideApproval(
                decided_by="original-operator",
                approve_for_run=False,
            ),
            ApprovalDecision.APPROVE_ONCE,
            None,
            "approve",
        ),
        (
            ApprovalStatus.REJECTED,
            "Persisted rejection feedback",
            DecideApproval(
                decided_by="original-operator",
                reason="Persisted rejection feedback",
            ),
            ApprovalDecision.REJECT_WITH_FEEDBACK,
            "Persisted rejection feedback",
            "reject",
        ),
    ],
)
async def test_signal_retry_recovers_split_public_and_runtime_decision(
    tmp_path: Path,
    status: ApprovalStatus,
    persisted_reason: str | None,
    retry_command: DecideApproval,
    expected_decision: ApprovalDecision,
    expected_feedback: str | None,
    workflow_action: str,
) -> None:
    fixture = await build_fixture(tmp_path)
    proposed = await build_coordinator(
        fixture,
        RestartableEngine(approval_events(tmp_path)),
    ).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-before-crash",
            cycle_id="cycle-proposal",
        )
    )
    assert proposed.waiting_object_id is not None
    approval_id = proposed.waiting_object_id

    # Model the process dying after the public Approval transaction commits
    # but before the RuntimeApprovalRequest transaction is saved.
    persisted, changed = await fixture["approvals"].decide(
        approval_id,
        status,
        decided_by="original-operator",
        reason=persisted_reason,
    )
    assert changed is True
    assert persisted.status is status
    split_runtime = await fixture["runtime_approvals"].get(approval_id)
    assert split_runtime is not None
    assert split_runtime.status is ApprovalStatus.PENDING

    service = approval_service(fixture)
    if status is ApprovalStatus.APPROVED:
        await service.approve(approval_id, retry_command)
    else:
        await service.reject(approval_id, retry_command)

    recovered = await fixture["runtime_approvals"].get(approval_id)
    assert recovered is not None
    assert recovered.decision is expected_decision
    assert recovered.decided_by == "original-operator"
    assert recovered.feedback == expected_feedback
    assert recovered.decided_at == persisted.decided_at
    assert fixture["workflow"].calls == [(workflow_action, "run-1", approval_id)]
    assert not await fixture["approvals"].is_granted("run-1", "python")

    await fixture["supervisor"].close()
    await fixture["database"].dispose()


async def test_split_once_retry_does_not_infer_scope_from_a_later_run_grant(
    tmp_path: Path,
) -> None:
    fixture = await build_fixture(tmp_path)
    proposed = await build_coordinator(
        fixture,
        RestartableEngine(approval_events(tmp_path)),
    ).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-before-legacy-split",
            cycle_id="cycle-legacy-split",
        )
    )
    assert proposed.waiting_object_id is not None
    approval_id = proposed.waiting_object_id

    persisted, changed = await fixture["approvals"].decide(
        approval_id,
        ApprovalStatus.APPROVED,
        decided_by="original-operator",
    )
    assert changed is True
    assert persisted.decision is ApprovalDecision.APPROVE_ONCE

    # Model a pre-decision-column split write, followed by a different
    # Approval creating the sole Run/Tool grant.  That grant has no source ID
    # and must not widen this Approval during its signal retry.
    async with fixture["database"].session_factory() as session, session.begin():
        legacy = await session.get(ApprovalRecord, approval_id)
        assert legacy is not None
        legacy.decision = None
        legacy.decision_feedback = None
    await fixture["approvals"].grant_for_run(
        "run-1",
        "python",
        created_by="later-operator",
    )

    await approval_service(fixture).approve(
        approval_id,
        DecideApproval(decided_by="original-operator", approve_for_run=False),
    )

    recovered_public = await fixture["approvals"].get(approval_id)
    recovered_runtime = await fixture["runtime_approvals"].get(approval_id)
    assert recovered_public is not None
    assert recovered_public.decision is ApprovalDecision.APPROVE_ONCE
    assert recovered_runtime is not None
    assert recovered_runtime.decision is ApprovalDecision.APPROVE_ONCE
    assert await fixture["approvals"].is_granted("run-1", "python")
    assert fixture["workflow"].calls == [("approve", "run-1", approval_id)]
    events = await fixture["events"].list_after("run-1")
    decision_events = [event for event in events if event.event_type == "tool.approved"]
    assert len(decision_events) == 1
    assert decision_events[0].payload["approve_for_run"] is False

    await fixture["supervisor"].close()
    await fixture["database"].dispose()


async def test_decision_before_provider_checkpoint_preserves_both_writers(
    tmp_path: Path,
) -> None:
    fixture = await build_fixture(tmp_path)
    engine = BlockingSuspendEngine(approval_events(tmp_path))
    cycle_task = asyncio.create_task(
        build_coordinator(fixture, engine).run_cycle(
            RunCycleRequest(
                run_id="run-1",
                session_id="session-1",
                worker_id="worker-blocked-suspend",
                cycle_id="cycle-blocked-suspend",
            )
        )
    )
    try:
        await asyncio.wait_for(engine.run.entered.wait(), timeout=2)
        pending = await fixture["runtime_approvals"].pending_for_run("run-1")
        assert len(pending) == 1
        approval_id = pending[0].id

        await approval_service(fixture).reject(
            approval_id,
            DecideApproval(decided_by="operator", reason="Denied while suspending"),
        )
        decided_before_provider = await fixture["runtime_approvals"].get(approval_id)
        assert decided_before_provider is not None
        assert decided_before_provider.status is ApprovalStatus.REJECTED
        assert decided_before_provider.provider_state_id is None

        engine.run.release.set()
        yielded = await asyncio.wait_for(cycle_task, timeout=2)
        persisted = await fixture["runtime_approvals"].get(approval_id)
        assert persisted is not None
        assert persisted.provider_state_id == yielded.provider_state_id
        assert persisted.status is ApprovalStatus.REJECTED
        assert persisted.decision is ApprovalDecision.REJECT_WITH_FEEDBACK
        assert persisted.feedback == "Denied while suspending"
        assert persisted.decided_by == "operator"
        assert persisted.decided_at == decided_before_provider.decided_at
    finally:
        engine.run.release.set()
        if not cycle_task.done():
            await asyncio.gather(cycle_task, return_exceptions=True)
        await fixture["supervisor"].close()
        await fixture["database"].dispose()


@pytest.mark.parametrize(
    ("persisted_decision", "persisted_actor", "persisted_feedback", "operation", "command"),
    [
        (
            ApprovalDecision.REJECT,
            "operator",
            None,
            "approve",
            DecideApproval(decided_by="operator"),
        ),
        (
            ApprovalDecision.APPROVE_ONCE,
            "other-operator",
            None,
            "approve",
            DecideApproval(decided_by="operator"),
        ),
        (
            ApprovalDecision.REJECT_WITH_FEEDBACK,
            "operator",
            "First feedback",
            "reject",
            DecideApproval(decided_by="operator", reason="Different feedback"),
        ),
        (
            ApprovalDecision.APPROVE_TOOL_FOR_RUN,
            "operator",
            None,
            "approve",
            DecideApproval(decided_by="operator", approve_for_run=False),
        ),
    ],
)
async def test_runtime_approval_conflicting_terminal_tuple_is_stable_conflict(
    tmp_path: Path,
    persisted_decision: ApprovalDecision,
    persisted_actor: str,
    persisted_feedback: str | None,
    operation: str,
    command: DecideApproval,
) -> None:
    fixture = await build_fixture(tmp_path)
    proposed = await build_coordinator(
        fixture,
        RestartableEngine(approval_events(tmp_path)),
    ).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-conflicting-runtime-decision",
            cycle_id="cycle-conflicting-runtime-decision",
        )
    )
    assert proposed.waiting_object_id is not None
    approval_id = proposed.waiting_object_id
    seeded, changed = await fixture["runtime_approvals"].decide_if_pending(
        approval_id,
        persisted_decision,
        decided_by=persisted_actor,
        feedback=persisted_feedback,
        decided_at=datetime(2026, 7, 30, 14, 0, tzinfo=UTC),
    )
    assert changed is True

    with pytest.raises(ApplicationConflictError) as captured:
        await getattr(approval_service(fixture), operation)(approval_id, command)

    assert captured.value.code == "runtime_approval_decision_conflict"
    assert await fixture["runtime_approvals"].get(approval_id) == seeded
    assert fixture["workflow"].calls == []
    events = await fixture["events"].list_after("run-1")
    assert not [event for event in events if event.event_type in {"tool.approved", "tool.rejected"}]
    await fixture["supervisor"].close()
    await fixture["database"].dispose()


async def test_two_ready_intents_execute_serially_and_retain_both_results(
    tmp_path: Path,
) -> None:
    fixture = await build_fixture(tmp_path)
    initial_engine = RestartableEngine(
        [
            tool_event(
                tmp_path,
                1,
                call_id="ready-call-1",
                label="first-ready-result",
                approval_level="never",
            ),
            tool_event(
                tmp_path,
                2,
                call_id="ready-call-2",
                label="second-ready-result",
                approval_level="never",
            ),
            engine_event(3, AgentEngineEventType.RUN_COMPLETED),
        ]
    )
    first = await build_coordinator(fixture, initial_engine).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-initial",
            cycle_id="cycle-ready-proposals",
        )
    )

    assert first.yield_reason is YieldReason.TOOL_RUNNING
    assert first.waiting_execution_id is not None
    assert len(await fixture["executions"].list("run-1")) == 1
    await fixture["execution_service"].wait(
        first.waiting_execution_id,
        timeout_seconds=2,
    )
    first_input = completed_execution_input(
        first.waiting_execution_id,
        "first-ready-result",
        artifact_source=True,
    )

    resumed_engine = RestartableEngine(
        [engine_event(1, AgentEngineEventType.RUN_COMPLETED)],
    )
    resumed = build_coordinator(
        fixture,
        resumed_engine,
        context_compiler=transcript_context_compiler(fixture),
    )
    second = await resumed.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-second",
            cycle_id="cycle-ready-second",
            input_items=[first_input, first_input],
        )
    )

    assert second.yield_reason is YieldReason.TOOL_RUNNING
    assert second.waiting_execution_id is not None
    assert second.waiting_execution_id != first.waiting_execution_id
    assert len(await fixture["executions"].list("run-1")) == 2
    assert resumed_engine.resume_requests == []
    await fixture["execution_service"].wait(
        second.waiting_execution_id,
        timeout_seconds=2,
    )
    final = await resumed.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-final",
            cycle_id="cycle-ready-results",
            input_items=[
                completed_execution_input(
                    second.waiting_execution_id,
                    "second-ready-result",
                )
            ],
        )
    )

    assert final.yield_reason is YieldReason.RUN_COMPLETED
    messages = await fixture["transcript"].list_by_session("session-1")
    tool_results = [
        message for message in messages if message.message_type is MessageType.TOOL_RESULT_REFERENCE
    ]
    assert [message.execution_id for message in tool_results] == [
        first.waiting_execution_id,
        second.waiting_execution_id,
    ]
    serialized_context = json.dumps(
        resumed_engine.start_requests[-1].input_items,
        ensure_ascii=False,
    )
    assert "first-ready-result" in serialized_context
    assert "second-ready-result" in serialized_context
    tool_context_items = [
        item
        for item in resumed_engine.start_requests[-1].input_items
        if item.get("type") == "relevant_tool_results"
    ]
    assert [item["content"]["execution_id"] for item in tool_context_items] == [
        first.waiting_execution_id,
        second.waiting_execution_id,
    ]
    session = await fixture["sessions"].get("session-1")
    assert session is not None and session.provider_state_id is None
    await fixture["supervisor"].close()
    await fixture["database"].dispose()


async def test_two_approvals_accept_second_signal_first_but_execute_in_intent_order(
    tmp_path: Path,
) -> None:
    fixture = await build_fixture(tmp_path)
    proposal_engine = RestartableEngine(
        [
            tool_event(
                tmp_path,
                1,
                call_id="approval-call-1",
                label="first-approved-result",
                approval_level="sensitive",
            ),
            tool_event(
                tmp_path,
                2,
                call_id="approval-call-2",
                label="second-approved-result",
                approval_level="sensitive",
            ),
            engine_event(3, AgentEngineEventType.RUN_COMPLETED),
        ]
    )
    proposed = await build_coordinator(fixture, proposal_engine).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-proposal",
            cycle_id="cycle-approval-proposals",
        )
    )
    intents = await fixture["intents"].pending_for_session("session-1")
    assert len(intents) == 2
    approvals = [await fixture["runtime_approvals"].get_for_intent(intent.id) for intent in intents]
    assert all(approval is not None for approval in approvals)
    first_approval, second_approval = approvals
    assert first_approval is not None and second_approval is not None
    assert proposed.waiting_object_id == first_approval.id

    service = approval_service(fixture)
    decision = DecideApproval(decided_by="operator", approve_for_run=False)
    await service.approve(second_approval.id, decision)
    resumed_engine = RestartableEngine(
        [engine_event(1, AgentEngineEventType.RUN_COMPLETED)],
    )
    resumed = build_coordinator(
        fixture,
        resumed_engine,
        context_compiler=transcript_context_compiler(fixture),
    )
    still_first = await resumed.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-out-of-order",
            cycle_id="cycle-second-approved-first",
            approval_id=second_approval.id,
        )
    )

    assert still_first.yield_reason is YieldReason.APPROVAL_REQUIRED
    assert still_first.waiting_object_id == first_approval.id
    assert await fixture["executions"].list("run-1") == []

    await service.approve(first_approval.id, decision)
    first = await resumed.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-first-approved",
            cycle_id="cycle-first-approved",
            approval_id=first_approval.id,
        )
    )
    assert first.yield_reason is YieldReason.TOOL_RUNNING
    assert first.waiting_execution_id is not None
    executions = await fixture["executions"].list("run-1")
    assert len(executions) == 1
    assert executions[0].tool_call_id == intents[0].id
    await fixture["execution_service"].wait(
        first.waiting_execution_id,
        timeout_seconds=2,
    )

    first_input = completed_execution_input(
        first.waiting_execution_id,
        "first-approved-result",
        artifact_source=True,
    )
    second = await resumed.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-second-approved",
            cycle_id="cycle-second-approved",
            input_items=[first_input, first_input],
        )
    )
    assert second.yield_reason is YieldReason.TOOL_RUNNING
    assert second.waiting_execution_id is not None
    executions = await fixture["executions"].list("run-1")
    assert len(executions) == 2
    assert [execution.tool_call_id for execution in executions] == [
        intents[0].id,
        intents[1].id,
    ]
    await fixture["execution_service"].wait(
        second.waiting_execution_id,
        timeout_seconds=2,
    )

    final = await resumed.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-results",
            cycle_id="cycle-approved-results",
            input_items=[
                completed_execution_input(
                    second.waiting_execution_id,
                    "second-approved-result",
                )
            ],
        )
    )
    assert final.yield_reason is YieldReason.RUN_COMPLETED
    messages = await fixture["transcript"].list_by_session("session-1")
    assert (
        len([message for message in messages if message.message_type is MessageType.APPROVAL]) == 2
    )
    tool_results = [
        message for message in messages if message.message_type is MessageType.TOOL_RESULT_REFERENCE
    ]
    assert [message.execution_id for message in tool_results] == [
        first.waiting_execution_id,
        second.waiting_execution_id,
    ]
    serialized_context = json.dumps(
        resumed_engine.start_requests[-1].input_items,
        ensure_ascii=False,
    )
    assert "first-approved-result" in serialized_context
    assert "second-approved-result" in serialized_context
    compiled_items = resumed_engine.start_requests[-1].input_items
    tool_context_items = [
        item for item in compiled_items if item.get("type") == "relevant_tool_results"
    ]
    assert [item["content"]["execution_id"] for item in tool_context_items] == [
        first.waiting_execution_id,
        second.waiting_execution_id,
    ]
    approval_context_items = [
        item
        for item in compiled_items
        if isinstance(item.get("content"), dict)
        and item["content"].get("type") == "approval_decision"
    ]
    assert [item["content"]["approval_id"] for item in approval_context_items] == [
        second_approval.id,
        first_approval.id,
    ]
    await fixture["supervisor"].close()
    await fixture["database"].dispose()


@pytest.mark.parametrize(
    ("feedback", "expected_decision"),
    [
        (None, ApprovalDecision.REJECT),
        ("Use the authorized staging host.", ApprovalDecision.REJECT_WITH_FEEDBACK),
    ],
)
async def test_rejection_restarts_model_with_durable_decision(
    tmp_path: Path,
    feedback: str | None,
    expected_decision: ApprovalDecision,
) -> None:
    fixture = await build_fixture(tmp_path)
    first = await build_coordinator(
        fixture,
        RestartableEngine(approval_events(tmp_path)),
    ).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-before-restart",
            cycle_id="cycle-proposal",
        )
    )
    approval_id = first.waiting_object_id
    assert approval_id is not None
    await approval_service(fixture).reject(
        approval_id,
        DecideApproval(decided_by="operator", reason=feedback),
    )

    restarted_engine = RestartableEngine(
        [engine_event(1, AgentEngineEventType.RUN_COMPLETED)],
    )
    result = await build_coordinator(fixture, restarted_engine).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-after-restart",
            cycle_id=f"cycle-{expected_decision.value}",
            approval_id=approval_id,
        )
    )

    runtime_request = await fixture["runtime_approvals"].get(approval_id)
    assert runtime_request is not None
    assert runtime_request.status is ApprovalStatus.REJECTED
    assert runtime_request.decision is expected_decision
    intent = await fixture["intents"].get(runtime_request.tool_call_intent_id)
    assert intent is not None and intent.status is ToolCallStatus.REJECTED
    assert result.yield_reason is YieldReason.RUN_COMPLETED
    assert await fixture["executions"].list("run-1") == []
    assert len(restarted_engine.start_requests) == 1
    assert restarted_engine.resume_requests == []
    input_items = restarted_engine.start_requests[0].input_items
    serialized_input = json.dumps(input_items, ensure_ascii=False)
    assert approval_id in serialized_input
    assert expected_decision.value in serialized_input
    if feedback is not None:
        assert feedback in serialized_input
    session = await fixture["sessions"].get("session-1")
    assert session is not None and session.provider_state_id is None
    await fixture["supervisor"].close()
    await fixture["database"].dispose()


async def test_user_input_request_recovers_after_worker_restart(tmp_path: Path) -> None:
    fixture = await build_fixture(tmp_path)
    first = await build_coordinator(
        fixture,
        RestartableEngine(
            [
                engine_event(
                    1,
                    AgentEngineEventType.ASSISTANT_MESSAGE,
                    content="Which authorized target should be tested next?",
                    requires_user_input=True,
                )
            ]
        ),
    ).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-before-restart",
            cycle_id="cycle-user-input",
        )
    )

    assert first.yield_reason is YieldReason.USER_INPUT_REQUIRED
    assert first.waiting_object_id is not None
    pending = await fixture["user_inputs"].get(first.waiting_object_id)
    assert pending is not None
    assert pending.prompt == "Which authorized target should be tested next?"
    assert pending.provider_state_id == first.provider_state_id

    event = await fixture["events"].append(
        "run-1",
        "user.message_queued",
        {"message": "Test staging.example only."},
    )
    resolver = _RunEventUserInputResolver(
        events=fixture["events"],
        sessions=fixture["sessions"],
        transcript=fixture["transcript"],
        requests=fixture["user_inputs"],
    )
    message_id = await resolver.resolve_user_input("run-1", "session-1", event.id)
    retried_message_id = await resolver.resolve_user_input("run-1", "session-1", event.id)
    assert retried_message_id == message_id

    restarted_engine = RestartableEngine(
        [engine_event(1, AgentEngineEventType.RUN_COMPLETED)],
    )
    result = await build_coordinator(fixture, restarted_engine).run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-after-restart",
            cycle_id="cycle-after-user-input",
            latest_user_message_id=message_id,
        )
    )

    answered = await fixture["user_inputs"].get(first.waiting_object_id)
    assert answered is not None
    assert answered.response_message_id == message_id
    assert result.yield_reason is YieldReason.RUN_COMPLETED
    assert len(restarted_engine.start_requests) == 1
    assert restarted_engine.resume_requests == []
    session = await fixture["sessions"].get("session-1")
    assert session is not None and session.provider_state_id is None
    await fixture["supervisor"].close()
    await fixture["database"].dispose()
