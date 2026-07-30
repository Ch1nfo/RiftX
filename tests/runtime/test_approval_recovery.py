from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from riftx.application.services import (
    ApprovalApplicationService,
    DecideApproval,
    RuntimeApprovalRequestRecorder,
)
from riftx.domain import (
    ApprovalStatus,
    Engagement,
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


class RecordingWorkflow:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def approve(self, run_id: str, approval_id: str) -> None:
        self.calls.append(("approve", run_id, approval_id))

    async def reject(self, run_id: str, approval_id: str) -> None:
        self.calls.append(("reject", run_id, approval_id))


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
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Run an approved command"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(
        AgentSession(id="session-1", run_id="run-1", model_profile="fake-model")
    )
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
        context_compiler=MinimalContextCompiler(),
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


def approval_service(fixture: dict[str, object]) -> ApprovalApplicationService:
    return ApprovalApplicationService(
        approval_repository=fixture["approvals"],
        run_repository=fixture["runs"],
        event_repository=fixture["events"],
        workflow_client=fixture["workflow"],
        runtime_approval_repository=fixture["runtime_approvals"],
    )


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
    await service.approve(runtime_request.id, command)

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
    ("feedback", "expected_decision"),
    [
        (None, ApprovalDecision.REJECT),
        ("Use the authorized staging host.", ApprovalDecision.REJECT_WITH_FEEDBACK),
    ],
)
async def test_rejection_resumes_model_with_durable_decision(
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
        [],
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
    assert len(restarted_engine.resume_requests) == 1
    input_items = restarted_engine.resume_requests[0].input_items
    serialized_input = json.dumps(input_items, ensure_ascii=False)
    assert approval_id in serialized_input
    assert expected_decision.value in serialized_input
    if feedback is not None:
        assert feedback in serialized_input
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
        [],
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
    assert len(restarted_engine.resume_requests) == 1
    await fixture["supervisor"].close()
    await fixture["database"].dispose()
