from __future__ import annotations

import asyncio
import hashlib
import shlex
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import RuntimeApprovalRequestRecorder
from riftx.domain import (
    ApprovalLevel,
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    RunKind,
)
from riftx.execution import (
    DeferredExecutionDispatcher,
    DeferredExecutionSpec,
    ExecutionService,
    build_execution_key,
    build_tool_call_intent_id,
)
from riftx.observer import (
    SupervisorCheck,
    SupervisorDisposition,
    SupervisorReport,
    SupervisorSeverity,
    SupervisorSignal,
)
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
)
from riftx.runner import ExecutionLaunchRequest, ProcessSupervisor, RunnerPaths
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import (
    AgentEngineEvent,
    AgentEngineEventType,
    AgentEngineState,
)
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import MinimalContextCompiler, RunCycleRequest
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    RuntimeApprovalRequest,
    ToolCallIntent,
    ToolCallStatus,
    YieldReason,
)


class DeferredEngineRun:
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[AgentEngineEvent]:
        for item in self._events:
            yield item

    async def suspend(self) -> AgentEngineState:
        return AgentEngineState(
            engine_type="fake",
            engine_version="1",
            provider="fake",
            model="fake-model",
            serialized_state={"deferred": True},
        )

    async def cancel(self) -> None:
        return None


class DeferredEngine:
    def __init__(self, events: list[AgentEngineEvent]) -> None:
        self._events = events

    async def start(self, request: object) -> DeferredEngineRun:
        return DeferredEngineRun(self._events)

    async def resume(self, request: object) -> DeferredEngineRun:
        return DeferredEngineRun(self._events)


class ApprovalYieldObserver:
    def __init__(
        self,
        tool_calls: SQLAlchemyToolCallIntentRepository,
        approvals: SQLAlchemyRuntimeApprovalRepository,
    ) -> None:
        self._tool_calls = tool_calls
        self._approvals = approvals
        self.calls = 0

    async def inspect(self, **kwargs: object) -> SupervisorReport:
        self.calls += 1
        session = kwargs["session"]
        cycle = kwargs["cycle"]
        assert isinstance(session, AgentSession)
        assert isinstance(cycle, AgentCycle)
        intents = await self._tool_calls.recent_for_session(session.id)
        if not intents:
            return SupervisorReport(
                run_id=session.run_id,
                session_id=session.id,
                cycle_id=cycle.id,
                disposition=SupervisorDisposition.CONTINUE,
            )
        approval = await self._approvals.get_for_intent(intents[-1].id)
        assert approval is not None
        return SupervisorReport(
            run_id=session.run_id,
            session_id=session.id,
            cycle_id=cycle.id,
            disposition=SupervisorDisposition.YIELD,
            yield_reason=YieldReason.APPROVAL_REQUIRED,
            signals=(
                SupervisorSignal(
                    code="approval_pending",
                    check=SupervisorCheck.APPROVAL,
                    severity=SupervisorSeverity.INFO,
                    summary="Wait for durable approval",
                    refs=(f"approval:{approval.id}",),
                    yield_reason=YieldReason.APPROVAL_REQUIRED,
                ),
            ),
        )


class RecordingRunner:
    def __init__(self, executions: SQLAlchemyExecutionRepository) -> None:
        self._executions = executions
        self.launches = 0

    async def start(
        self,
        request: ExecutionLaunchRequest,
        *,
        effect_guard=None,
    ) -> Execution:
        if effect_guard is not None:
            await effect_guard()
        execution = Execution(
            execution_key=request.execution_key,
            launch_fingerprint=request.launch_fingerprint,
            run_id=request.run_id,
            session_id=request.session_id,
            tool_call_id=request.tool_call_id,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
            executor_type=request.executor_type,
            argv=request.argv,
            command_text=request.command_text,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            status=ExecutionStatus.QUEUED,
            stdout_path=str(request.cwd / "stdout.log"),
            stderr_path=str(request.cwd / "stderr.log"),
        )
        execution, created = await self._executions.create_if_absent(execution)
        if not created:
            return execution
        self.launches += 1
        execution.transition_to(ExecutionStatus.STARTING)
        execution.transition_to(ExecutionStatus.RUNNING)
        return await self._executions.save(execution)


@dataclass
class DurableDispatcherFixture:
    database: Database
    dispatcher: DeferredExecutionDispatcher
    tool_calls: SQLAlchemyToolCallIntentRepository
    executions: SQLAlchemyExecutionRepository
    public_approvals: SQLAlchemyApprovalRepository
    runtime_approvals: SQLAlchemyRuntimeApprovalRepository
    approval_recorder: RuntimeApprovalRequestRecorder
    runner: RecordingRunner
    run: Run
    session: AgentSession
    cycles: dict[str, AgentCycle]
    steps: dict[str, AgentStep]
    workspace: Path


@dataclass
class CodeAuditDispatcherFixture:
    database: Database
    dispatcher: DeferredExecutionDispatcher
    tool_calls: SQLAlchemyToolCallIntentRepository
    runner: RecordingRunner
    run: Run
    session: AgentSession
    cycle: AgentCycle
    step: AgentStep
    workspace: Path


async def build_code_audit_dispatcher(tmp_path: Path) -> CodeAuditDispatcherFixture:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'code-audit-dispatch.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-audit", name="Authorized")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    run = await runs.create(
        Run(
            kind=RunKind.CODE_AUDIT,
            id="run-audit",
            engagement_id="engagement-audit",
            node_id="local",
            objective=Objective(description="Reject generic deferred execution"),
            workspace_path=str(tmp_path),
        )
    )
    session = AgentSession(
        id="session-audit",
        run_id=run.id,
        model_profile="fake-model",
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(session)
    cycle = AgentCycle(
        id="cycle-audit",
        run_id=run.id,
        session_id=session.id,
        sequence=1,
    )
    await SQLAlchemyAgentCycleRepository(database.session_factory).create(cycle)
    step = AgentStep(
        id="step-audit",
        cycle_id=cycle.id,
        sequence=1,
        step_type=AgentStepType.TOOL_PROPOSAL,
    )
    await SQLAlchemyAgentStepRepository(database.session_factory).create(step)
    tool_calls = SQLAlchemyToolCallIntentRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    runner = RecordingRunner(executions)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=ExecutionService(
            execution_repository=executions,
            session_repository=sessions,
            tool_call_repository=tool_calls,
            runner=runner,  # type: ignore[arg-type]
            run_repository=runs,
        ),
    )
    return CodeAuditDispatcherFixture(
        database=database,
        dispatcher=dispatcher,
        tool_calls=tool_calls,
        runner=runner,
        run=run,
        session=session,
        cycle=cycle,
        step=step,
        workspace=tmp_path,
    )


@pytest.fixture
async def durable_dispatcher(tmp_path: Path) -> AsyncIterator[DurableDispatcherFixture]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'durable-dispatch.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-durable", name="Authorized")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    run = await runs.create(
        Run(
            kind="general",
            id="run-durable",
            engagement_id="engagement-durable",
            node_id="local",
            objective=Objective(description="Exercise durable Tool Call identity"),
            workspace_path=str(tmp_path),
        )
    )
    session = AgentSession(
        id="session-durable",
        run_id="run-durable",
        model_profile="fake-model",
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(session)
    cycles = {
        "cycle-1": AgentCycle(
            id="cycle-1",
            run_id=session.run_id,
            session_id=session.id,
            sequence=1,
        ),
        "cycle-2": AgentCycle(
            id="cycle-2",
            run_id=session.run_id,
            session_id=session.id,
            sequence=2,
        ),
    }
    cycle_repository = SQLAlchemyAgentCycleRepository(database.session_factory)
    for cycle in cycles.values():
        await cycle_repository.create(cycle)
    steps = {
        "cycle-1": AgentStep(
            id="step-1",
            cycle_id="cycle-1",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        ),
        "cycle-2": AgentStep(
            id="step-2",
            cycle_id="cycle-2",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        ),
    }
    step_repository = SQLAlchemyAgentStepRepository(database.session_factory)
    for step in steps.values():
        await step_repository.create(step)
    tool_calls = SQLAlchemyToolCallIntentRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    public_approvals = SQLAlchemyApprovalRepository(database.session_factory)
    runtime_approvals = SQLAlchemyRuntimeApprovalRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    approval_recorder = RuntimeApprovalRequestRecorder(
        approval_repository=public_approvals,
        runtime_repository=runtime_approvals,
        event_repository=events,
    )
    runner = RecordingRunner(executions)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=ExecutionService(
            execution_repository=executions,
            session_repository=sessions,
            tool_call_repository=tool_calls,
            runner=runner,  # type: ignore[arg-type]
            run_repository=runs,
        ),
    )
    yield DurableDispatcherFixture(
        database=database,
        dispatcher=dispatcher,
        tool_calls=tool_calls,
        executions=executions,
        public_approvals=public_approvals,
        runtime_approvals=runtime_approvals,
        approval_recorder=approval_recorder,
        runner=runner,
        run=run,
        session=session,
        cycles=cycles,
        steps=steps,
        workspace=tmp_path,
    )
    await database.dispose()


def deferred_event(
    workspace: Path,
    *,
    arguments: dict[str, object] | None = None,
    execution_updates: dict[str, object] | None = None,
) -> AgentEngineEvent:
    execution: dict[str, object] = {
        "node_id": "local",
        "executor_type": "process",
        "cwd": str(workspace),
        "argv": [sys.executable, "-c", "print('durable')"],
    }
    execution.update(execution_updates or {})
    return AgentEngineEvent(
        sequence=1,
        event_type=AgentEngineEventType.TOOL_CALL_READY,
        data={
            "call_id": "provider-call-reused",
            "tool_id": "run_shell",
            "arguments": arguments or {"command": "durable"},
            "reason": "test durable identity",
            "target_summary": "local fixture",
            "approval_level": ApprovalLevel.SENSITIVE.value,
            "execution": execution,
        },
    )


def approved_control_event() -> AgentEngineEvent:
    return AgentEngineEvent(
        sequence=1,
        event_type=AgentEngineEventType.TOOL_CALL_READY,
        data={
            "call_id": "patch-call",
            "tool_id": "apply_patch",
            "arguments": {"path": "src/app.py"},
            "approval_level": ApprovalLevel.ALWAYS.value,
            "approval_policy": "explicit",
            "approval_required": True,
        },
    )


def legacy_intent(
    fixture: DurableDispatcherFixture,
    *,
    cycle_id: str,
    event: AgentEngineEvent,
    status: ToolCallStatus,
) -> ToolCallIntent:
    call_id = str(event.data["call_id"])
    spec = DeferredExecutionSpec.model_validate(event.data["execution"])
    identity = "\x1f".join((fixture.session.run_id, fixture.session.id, call_id))
    return ToolCallIntent(
        id=f"tool-call:v1:{hashlib.sha256(identity.encode()).hexdigest()}",
        run_id=fixture.session.run_id,
        session_id=fixture.session.id,
        cycle_id=cycle_id,
        step_id=fixture.steps[cycle_id].id,
        tool_id=str(event.data["tool_id"]),
        arguments=dict(event.data["arguments"]),
        command_preview=spec.command_text or shlex.join(spec.argv),
        reason=str(event.data["reason"]),
        target_summary=str(event.data["target_summary"]),
        approval_level=ApprovalLevel(str(event.data["approval_level"])),
        status=status,
        engine_call_id=call_id,
        execution_spec=spec.model_dump(mode="json"),
    )


async def prepare(
    fixture: DurableDispatcherFixture,
    *,
    cycle_id: str,
    event: AgentEngineEvent,
    status: ToolCallStatus = ToolCallStatus.READY,
) -> ToolCallIntent:
    return await fixture.dispatcher.prepare(
        session=fixture.session,
        cycle=fixture.cycles[cycle_id],
        step=fixture.steps[cycle_id],
        event=event,
        status=status,
    )


async def test_provider_control_intent_requires_approval_and_settles_once(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    fixture = durable_dispatcher
    intent = await fixture.dispatcher.prepare_control(
        session=fixture.session,
        cycle=fixture.cycles["cycle-1"],
        step=fixture.steps["cycle-1"],
        event=approved_control_event(),
    )

    assert intent.execution_spec is None
    assert intent.status is ToolCallStatus.WAITING_APPROVAL
    await fixture.dispatcher.approve_intent(intent.id)
    with pytest.raises(ApplicationConflictError) as mismatched:
        await fixture.dispatcher.begin_control_intent(
            run_id=fixture.run.id,
            session_id=fixture.session.id,
            engine_call_id="patch-call",
            tool_name="apply_patch",
            arguments={"path": "foreign.py"},
        )
    assert mismatched.value.code == "control_tool_intent_mismatch"
    claimed = await fixture.dispatcher.begin_control_intent(
        run_id=fixture.run.id,
        session_id=fixture.session.id,
        engine_call_id="patch-call",
        tool_name="apply_patch",
        arguments={"path": "src/app.py"},
    )
    assert claimed is not None
    assert claimed.id == intent.id
    assert claimed.status is ToolCallStatus.EXECUTING
    with pytest.raises(ApplicationConflictError, match="exactly-once"):
        await fixture.dispatcher.begin_control_intent(
            run_id=fixture.run.id,
            session_id=fixture.session.id,
            engine_call_id="patch-call",
            tool_name="apply_patch",
            arguments={"path": "src/app.py"},
        )

    await fixture.dispatcher.finish_control_intent(
        run_id=fixture.run.id,
        session_id=fixture.session.id,
        engine_call_id="patch-call",
        succeeded=True,
    )

    settled = await fixture.tool_calls.get(intent.id)
    assert settled is not None and settled.status is ToolCallStatus.COMPLETED


async def test_provider_control_intent_can_persist_a_deterministic_execution_claim(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    fixture = durable_dispatcher
    intent = await fixture.dispatcher.prepare_control(
        session=fixture.session,
        cycle=fixture.cycles["cycle-1"],
        step=fixture.steps["cycle-1"],
        event=approved_control_event(),
    )
    await fixture.dispatcher.approve_intent(intent.id)

    claimed = await fixture.dispatcher.begin_control_intent(
        run_id=fixture.run.id,
        session_id=fixture.session.id,
        engine_call_id="patch-call",
        tool_name="apply_patch",
        arguments={"path": "src/app.py"},
        attempt_group="mcp",
    )

    assert claimed is not None
    execution_key = build_execution_key(
        run_id=fixture.run.id,
        session_id=fixture.session.id,
        tool_call_id=intent.id,
        attempt_group="mcp",
    )
    assert await fixture.tool_calls.execution_claim_is_current(
        intent.id,
        execution_key=execution_key,
        attempt_group="mcp",
    )


async def record_approval(
    fixture: DurableDispatcherFixture,
    *,
    cycle_id: str,
    intent: ToolCallIntent,
    context_compilation_id: str | None = None,
    working_memory_version: int | None = None,
) -> RuntimeApprovalRequest:
    cycle = fixture.cycles[cycle_id]
    return await fixture.approval_recorder.record(
        fixture.run,
        session=fixture.session,
        cycle=cycle,
        step=fixture.steps[cycle_id],
        intent=intent,
        context_compilation_id=context_compilation_id,
        working_memory_version=working_memory_version or cycle.sequence,
    )


async def test_code_audit_prepare_denies_before_intent_persistence_and_runner(
    tmp_path: Path,
) -> None:
    fixture = await build_code_audit_dispatcher(tmp_path)
    event = deferred_event(fixture.workspace)
    expected_id = build_tool_call_intent_id(
        run_id=fixture.run.id,
        session_id=fixture.session.id,
        cycle_id=fixture.cycle.id,
        engine_call_id=str(event.data["call_id"]),
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await fixture.dispatcher.prepare(
            session=fixture.session,
            cycle=fixture.cycle,
            step=fixture.step,
            event=event,
        )

    assert captured.value.code == "run_kind_operation_unsupported"
    assert await fixture.tool_calls.get(expected_id) is None
    assert fixture.runner.launches == 0

    with pytest.raises(ApplicationConflictError) as owner_error:
        await fixture.dispatcher.prepare(
            session=fixture.session,
            cycle=fixture.cycle.model_copy(update={"session_id": "session-foreign"}),
            step=fixture.step,
            event=event,
        )
    assert owner_error.value.code == "deferred_execution_identity_mismatch"
    assert await fixture.tool_calls.get(expected_id) is None
    await fixture.database.dispose()


async def test_code_audit_durable_intent_effects_deny_without_mutation(
    tmp_path: Path,
) -> None:
    fixture = await build_code_audit_dispatcher(tmp_path)
    spec = DeferredExecutionSpec(
        node_id="local",
        executor_type=ExecutorType.PROCESS,
        cwd=fixture.workspace,
        argv=[sys.executable, "--version"],
    )
    intent = await fixture.tool_calls.create(
        ToolCallIntent(
            id="tool-call-audit",
            run_id=fixture.run.id,
            session_id=fixture.session.id,
            cycle_id=fixture.cycle.id,
            step_id=fixture.step.id,
            tool_id="scanner",
            status=ToolCallStatus.WAITING_APPROVAL,
            execution_spec=spec.model_dump(mode="json"),
        )
    )
    execution = Execution(
        id="execution-audit",
        execution_key="execution-key-audit",
        run_id=fixture.run.id,
        session_id=fixture.session.id,
        tool_call_id=intent.id,
        attempt_group="initial",
        node_id="local",
        executor_type=ExecutorType.PROCESS,
        cwd=str(fixture.workspace),
        status=ExecutionStatus.RUNNING,
        stdout_path=str(fixture.workspace / "stdout.log"),
        stderr_path=str(fixture.workspace / "stderr.log"),
    )

    operations = (
        fixture.dispatcher.execute_intent(intent),
        fixture.dispatcher.claim_intent_execution(
            intent,
            execution_key=execution.execution_key,
            attempt_group="initial",
        ),
        fixture.dispatcher.sync_intent_execution(intent, execution),
        fixture.dispatcher.approve_intent(intent.id),
        fixture.dispatcher.reject_intent(intent.id),
        fixture.dispatcher.mark_intent_executing(intent),
    )
    for operation in operations:
        with pytest.raises(ApplicationConflictError) as captured:
            await operation
        assert captured.value.code == "run_kind_operation_unsupported"

    durable = await fixture.tool_calls.get(intent.id)
    assert durable is not None and durable.status is ToolCallStatus.WAITING_APPROVAL
    assert fixture.runner.launches == 0

    forged = intent.model_copy(update={"run_id": "run-foreign"})
    with pytest.raises(ApplicationConflictError) as owner_error:
        await fixture.dispatcher.mark_intent_executing(forged)
    assert owner_error.value.code == "tool_call_identity_mismatch"
    await fixture.database.dispose()


async def test_v2_exact_prepare_replay_reuses_one_immutable_intent(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    event = deferred_event(durable_dispatcher.workspace)

    first = await prepare(durable_dispatcher, cycle_id="cycle-1", event=event)
    replayed = await prepare(durable_dispatcher, cycle_id="cycle-1", event=event)

    assert first.id == build_tool_call_intent_id(
        run_id=durable_dispatcher.session.run_id,
        session_id=durable_dispatcher.session.id,
        cycle_id="cycle-1",
        engine_call_id="provider-call-reused",
    )
    assert first.id.startswith("tool-call:v2:")
    assert replayed == first
    assert len(await durable_dispatcher.tool_calls.pending_for_session(first.session_id)) == 1


async def test_v2_prepare_concurrently_deduplicates_exact_logical_call(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    event = deferred_event(durable_dispatcher.workspace)

    prepared = await asyncio.gather(
        *(prepare(durable_dispatcher, cycle_id="cycle-1", event=event) for _ in range(8))
    )

    assert len({intent.id for intent in prepared}) == 1
    persisted = await durable_dispatcher.tool_calls.pending_for_session(
        durable_dispatcher.session.id
    )
    assert len(persisted) == 1
    assert persisted[0].id == prepared[0].id


async def test_reused_provider_call_across_cycles_has_two_complete_approval_bridges(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    event = deferred_event(durable_dispatcher.workspace)

    first = await prepare(
        durable_dispatcher,
        cycle_id="cycle-1",
        event=event,
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    first_request = await record_approval(
        durable_dispatcher,
        cycle_id="cycle-1",
        intent=first,
    )
    second = await prepare(
        durable_dispatcher,
        cycle_id="cycle-2",
        event=event,
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    second_request = await record_approval(
        durable_dispatcher,
        cycle_id="cycle-2",
        intent=second,
    )

    assert first.id != second.id
    assert first.engine_call_id == second.engine_call_id == "provider-call-reused"
    assert first_request.id != second_request.id
    assert first_request.tool_call_intent_id == first.id
    assert second_request.tool_call_intent_id == second.id
    public_approvals = await durable_dispatcher.public_approvals.list(durable_dispatcher.run.id)
    assert {approval.id for approval in public_approvals} == {
        first_request.id,
        second_request.id,
    }
    for request, intent, cycle_id in (
        (first_request, first, "cycle-1"),
        (second_request, second, "cycle-2"),
    ):
        approval = await durable_dispatcher.public_approvals.get(request.id)
        assert approval is not None
        tool_call = await durable_dispatcher.public_approvals.get_tool_call(approval.tool_call_id)
        assert tool_call is not None
        assert tool_call.sdk_call_id == intent.id
        assert tool_call.agent_step_id == durable_dispatcher.steps[cycle_id].id
        runtime_request = await durable_dispatcher.runtime_approvals.get(request.id)
        assert runtime_request is not None
        assert runtime_request.tool_call_intent_id == intent.id

    replayed = await prepare(
        durable_dispatcher,
        cycle_id="cycle-1",
        event=event,
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    replayed_request = await record_approval(
        durable_dispatcher,
        cycle_id="cycle-1",
        intent=replayed,
    )
    assert replayed.id == first.id
    assert replayed_request.id == first_request.id
    assert len(await durable_dispatcher.public_approvals.list(durable_dispatcher.run.id)) == 2


async def test_legacy_same_cycle_pending_approval_replay_keeps_original_bridge(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    event = deferred_event(durable_dispatcher.workspace)
    legacy = legacy_intent(
        durable_dispatcher,
        cycle_id="cycle-1",
        event=event,
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    await durable_dispatcher.tool_calls.create(legacy)
    original_request = await record_approval(
        durable_dispatcher,
        cycle_id="cycle-1",
        intent=legacy,
    )

    replayed = await prepare(
        durable_dispatcher,
        cycle_id="cycle-1",
        event=event,
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    replayed_request = await record_approval(
        durable_dispatcher,
        cycle_id="cycle-1",
        intent=replayed,
    )

    assert replayed.id == legacy.id
    assert replayed.status is ToolCallStatus.WAITING_APPROVAL
    assert replayed_request.id == original_request.id
    assert replayed_request.tool_call_intent_id == legacy.id
    approval = await durable_dispatcher.public_approvals.get(original_request.id)
    assert approval is not None
    tool_call = await durable_dispatcher.public_approvals.get_tool_call(approval.tool_call_id)
    assert tool_call is not None
    assert tool_call.sdk_call_id == legacy.engine_call_id
    assert len(await durable_dispatcher.public_approvals.list(durable_dispatcher.run.id)) == 1
    v2_id = build_tool_call_intent_id(
        run_id=legacy.run_id,
        session_id=legacy.session_id,
        cycle_id=legacy.cycle_id,
        engine_call_id=legacy.engine_call_id or "",
    )
    assert await durable_dispatcher.tool_calls.get(v2_id) is None


async def test_cross_cycle_legacy_intent_is_preserved_and_v2_bridge_is_distinct(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    event = deferred_event(durable_dispatcher.workspace)
    legacy = legacy_intent(
        durable_dispatcher,
        cycle_id="cycle-1",
        event=event,
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    await durable_dispatcher.tool_calls.create(legacy)
    legacy_request = await record_approval(
        durable_dispatcher,
        cycle_id="cycle-1",
        intent=legacy,
    )

    current = await prepare(
        durable_dispatcher,
        cycle_id="cycle-2",
        event=event,
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    current_request = await record_approval(
        durable_dispatcher,
        cycle_id="cycle-2",
        intent=current,
    )

    assert current.id.startswith("tool-call:v2:")
    assert current.id != legacy.id
    assert await durable_dispatcher.tool_calls.get(legacy.id) == legacy
    assert legacy_request.id != current_request.id
    assert legacy_request.tool_call_intent_id == legacy.id
    assert current_request.tool_call_intent_id == current.id
    legacy_approval = await durable_dispatcher.public_approvals.get(legacy_request.id)
    current_approval = await durable_dispatcher.public_approvals.get(current_request.id)
    assert legacy_approval is not None and current_approval is not None
    legacy_tool_call = await durable_dispatcher.public_approvals.get_tool_call(
        legacy_approval.tool_call_id
    )
    current_tool_call = await durable_dispatcher.public_approvals.get_tool_call(
        current_approval.tool_call_id
    )
    assert legacy_tool_call is not None and current_tool_call is not None
    assert legacy_tool_call.sdk_call_id == "provider-call-reused"
    assert current_tool_call.sdk_call_id == current.id


@pytest.mark.parametrize("existing_kind", ["v2", "legacy"])
@pytest.mark.parametrize("drifted_field", ["arguments", "execution_spec"])
async def test_existing_intent_snapshot_drift_is_a_hard_conflict(
    durable_dispatcher: DurableDispatcherFixture,
    existing_kind: str,
    drifted_field: str,
) -> None:
    original = deferred_event(durable_dispatcher.workspace)
    if existing_kind == "v2":
        await prepare(durable_dispatcher, cycle_id="cycle-1", event=original)
    else:
        await durable_dispatcher.tool_calls.create(
            legacy_intent(
                durable_dispatcher,
                cycle_id="cycle-1",
                event=original,
                status=ToolCallStatus.READY,
            )
        )
    drifted = (
        deferred_event(
            durable_dispatcher.workspace,
            arguments={"command": "drifted"},
        )
        if drifted_field == "arguments"
        else deferred_event(
            durable_dispatcher.workspace,
            execution_updates={"timeout_seconds": 7},
        )
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await prepare(durable_dispatcher, cycle_id="cycle-1", event=drifted)

    assert captured.value.code == "tool_call_identity_mismatch"
    assert drifted_field in captured.value.details["mismatched_fields"]


async def test_legacy_execution_dispatch_replay_stays_bound_to_v1_intent(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    event = deferred_event(durable_dispatcher.workspace)
    legacy = legacy_intent(
        durable_dispatcher,
        cycle_id="cycle-1",
        event=event,
        status=ToolCallStatus.READY,
    )
    await durable_dispatcher.tool_calls.create(legacy)

    first = await durable_dispatcher.dispatcher.dispatch(
        session=durable_dispatcher.session,
        cycle=durable_dispatcher.cycles["cycle-1"],
        step=durable_dispatcher.steps["cycle-1"],
        event=event,
    )
    replayed = await durable_dispatcher.dispatcher.dispatch(
        session=durable_dispatcher.session,
        cycle=durable_dispatcher.cycles["cycle-1"],
        step=durable_dispatcher.steps["cycle-1"],
        event=event,
    )

    assert replayed.id == first.id
    assert first.tool_call_id == legacy.id
    assert durable_dispatcher.runner.launches == 1
    executions = await durable_dispatcher.executions.list(legacy.run_id)
    assert len(executions) == 1
    assert executions[0].tool_call_id == legacy.id


async def test_reused_approval_bridge_validates_public_and_runtime_identity(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    event = deferred_event(durable_dispatcher.workspace)
    legacy = legacy_intent(
        durable_dispatcher,
        cycle_id="cycle-1",
        event=event,
        status=ToolCallStatus.WAITING_APPROVAL,
    )
    await durable_dispatcher.tool_calls.create(legacy)
    await record_approval(
        durable_dispatcher,
        cycle_id="cycle-1",
        intent=legacy,
    )

    drifted = legacy.model_copy(update={"arguments": {"command": "drifted"}})
    with pytest.raises(ApplicationConflictError) as public_conflict:
        await record_approval(
            durable_dispatcher,
            cycle_id="cycle-1",
            intent=drifted,
        )
    assert public_conflict.value.code == "approval_bridge_identity_mismatch"
    assert "tool_call.arguments" in public_conflict.value.details["mismatched_fields"]

    with pytest.raises(ApplicationConflictError) as runtime_conflict:
        await record_approval(
            durable_dispatcher,
            cycle_id="cycle-1",
            intent=legacy,
            working_memory_version=99,
        )
    assert runtime_conflict.value.code == "approval_bridge_identity_mismatch"
    assert "working_memory_version" in runtime_conflict.value.details["mismatched_fields"]


async def test_runtime_retry_yields_same_deferred_execution_without_relaunch(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'deferred.db'}")
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
            objective=Objective(description="Run one deferred tool"),
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="fake-model"))
    tool_calls = SQLAlchemyToolCallIntentRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = ProcessSupervisor(executions, RunnerPaths(tmp_path / "runner"))
    execution_service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=supervisor,
        run_repository=runs,
    )
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=execution_service,
    )
    events = [
        AgentEngineEvent(
            sequence=1,
            event_type=AgentEngineEventType.TOOL_CALL_READY,
            data={
                "call_id": "call-stable-1",
                "tool_id": "run_shell",
                "arguments": {"command": "deferred"},
                "execution": {
                    "node_id": "local",
                    "executor_type": "process",
                    "cwd": str(tmp_path),
                    "argv": [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(0.15); print('deferred done')",
                    ],
                },
            },
        ),
        AgentEngineEvent(sequence=2, event_type=AgentEngineEventType.RUN_COMPLETED),
    ]
    coordinator = RuntimeCoordinator(
        run_repository=runs,
        session_repository=sessions,
        cycle_repository=SQLAlchemyAgentCycleRepository(database.session_factory),
        step_repository=SQLAlchemyAgentStepRepository(database.session_factory),
        provider_state_repository=SQLAlchemyProviderStateRepository(database.session_factory),
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        lease_manager=DatabaseRunLeaseManager(
            SQLAlchemyRunLeaseRepository(database.session_factory)
        ),
        context_compiler=MinimalContextCompiler(),
        agent_engine=DeferredEngine(events),
        deferred_execution_dispatcher=dispatcher,
    )

    first = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-1")
    )
    retried = await coordinator.run_cycle(
        RunCycleRequest(run_id="run-1", session_id="session-1", worker_id="worker-2")
    )

    assert first.yield_reason is YieldReason.TOOL_RUNNING
    assert first.waiting_execution_id is not None
    assert retried.yield_reason is YieldReason.TOOL_RUNNING
    assert retried.waiting_execution_id == first.waiting_execution_id
    persisted = await executions.list("run-1")
    assert len(persisted) == 1
    intent = await tool_calls.get(persisted[0].tool_call_id or "")
    assert intent is not None
    assert intent.status in {ToolCallStatus.EXECUTING, ToolCallStatus.COMPLETED}
    assert intent.execution_spec is not None
    assert intent.execution_spec["argv"] == events[0].data["execution"]["argv"]

    completed = await execution_service.wait(first.waiting_execution_id, timeout_seconds=2)
    assert completed.execution.status is ExecutionStatus.COMPLETED
    await supervisor.close()
    await database.dispose()


async def test_runtime_observer_yields_with_durable_approval_identity(
    durable_dispatcher: DurableDispatcherFixture,
) -> None:
    database = durable_dispatcher.database
    runs = SQLAlchemyRunRepository(database.session_factory)
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    observer = ApprovalYieldObserver(
        durable_dispatcher.tool_calls,
        durable_dispatcher.runtime_approvals,
    )
    coordinator = RuntimeCoordinator(
        run_repository=runs,
        session_repository=sessions,
        cycle_repository=SQLAlchemyAgentCycleRepository(database.session_factory),
        step_repository=SQLAlchemyAgentStepRepository(database.session_factory),
        provider_state_repository=SQLAlchemyProviderStateRepository(database.session_factory),
        event_repository=events,
        lease_manager=DatabaseRunLeaseManager(
            SQLAlchemyRunLeaseRepository(database.session_factory)
        ),
        context_compiler=MinimalContextCompiler(),
        agent_engine=DeferredEngine(
            [
                deferred_event(durable_dispatcher.workspace),
                AgentEngineEvent(
                    sequence=2,
                    event_type=AgentEngineEventType.RUN_COMPLETED,
                ),
            ]
        ),
        deferred_execution_dispatcher=durable_dispatcher.dispatcher,
        approval_repository=durable_dispatcher.public_approvals,
        runtime_approval_repository=durable_dispatcher.runtime_approvals,
        approval_recorder=durable_dispatcher.approval_recorder,
        observer=observer,
    )

    result = await coordinator.run_cycle(
        RunCycleRequest(
            run_id=durable_dispatcher.run.id,
            session_id=durable_dispatcher.session.id,
            worker_id="observer-worker",
            cycle_id="observer-approval-cycle",
        )
    )

    pending = await durable_dispatcher.runtime_approvals.pending_for_run(
        durable_dispatcher.run.id
    )
    assert len(pending) == 1
    assert result.yield_reason is YieldReason.APPROVAL_REQUIRED
    assert result.waiting_object_id == pending[0].id
    assert pending[0].provider_state_id == result.provider_state_id
    assert observer.calls == 2
    observer_events = [
        item
        for item in await events.list_after(durable_dispatcher.run.id)
        if item.event_type == "runtime.observer_inspected"
    ]
    assert [item.payload["phase"] for item in observer_events] == [
        "pre_model",
        "tool_intent",
    ]
