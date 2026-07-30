"""Finite durable Agent Runtime cycle coordinator."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Protocol, cast

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import ApprovalRepository
from riftx.application.services import RuntimeApprovalRequestRecorder
from riftx.domain import (
    ApprovalLevel,
    DomainError,
    MessageRole,
    MessageType,
    MessageVisibility,
    Run,
    RunStatus,
    TranscriptMessageDraft,
    requires_approval,
)
from riftx.domain.base import new_id
from riftx.execution import DeferredExecutionDispatcher
from riftx.persistence.repositories import SQLAlchemyRunEventRepository, SQLAlchemyRunRepository
from riftx.persistence.runtime_repositories import (
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRuntimeApprovalRepository,
    SQLAlchemyUserInputRequestRepository,
)
from riftx.persistence.transcript_repositories import SQLAlchemyTranscriptRepository
from riftx.runtime.engine import (
    AgentEngine,
    AgentEngineEvent,
    AgentEngineEventType,
    AgentEngineRequest,
    AgentEngineResumeRequest,
    AgentEngineRun,
    AgentEngineState,
)
from riftx.runtime.events import (
    CONTEXT_COMPILED,
    CYCLE_CREATED,
    CYCLE_FAILED,
    CYCLE_STARTED,
    CYCLE_YIELDED,
    ENGINE_EVENT,
    LEASE_ACQUIRED,
    LEASE_RELEASED,
    SESSION_ACTIVATED,
    STEP_COMPLETED,
    STEP_STARTED,
)
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import (
    ContextCompiler,
    ContextCompileRequest,
    CycleLimits,
    RunCycleRequest,
    RunCycleResult,
)
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ApprovalDecision,
    CycleStatus,
    RuntimeStateMachine,
    SessionStatus,
    StepStatus,
    ToolCallStatus,
    UserInputRequest,
    YieldReason,
)

_STEP_TYPES = {
    AgentEngineEventType.ASSISTANT_MESSAGE: AgentStepType.ASSISTANT_MESSAGE,
    AgentEngineEventType.TOOL_CALL_READY: AgentStepType.TOOL_PROPOSAL,
    AgentEngineEventType.PLAN_UPDATE: AgentStepType.PLAN_UPDATE,
    AgentEngineEventType.SUBAGENT_REQUESTED: AgentStepType.SUBAGENT_DELEGATION,
    AgentEngineEventType.FINAL_OUTPUT: AgentStepType.RUN_COMPLETION,
}


class _ContextUsageRecorder(Protocol):
    async def record_usage(
        self,
        compilation_id: str,
        usage: Mapping[str, object],
    ) -> object: ...


class RuntimeCoordinator:
    def __init__(
        self,
        *,
        run_repository: SQLAlchemyRunRepository,
        session_repository: SQLAlchemyAgentSessionRepository,
        cycle_repository: SQLAlchemyAgentCycleRepository,
        step_repository: SQLAlchemyAgentStepRepository,
        provider_state_repository: SQLAlchemyProviderStateRepository,
        event_repository: SQLAlchemyRunEventRepository,
        lease_manager: DatabaseRunLeaseManager,
        context_compiler: ContextCompiler,
        agent_engine: AgentEngine,
        context_usage_recorder: _ContextUsageRecorder | None = None,
        transcript_repository: SQLAlchemyTranscriptRepository | None = None,
        deferred_execution_dispatcher: DeferredExecutionDispatcher | None = None,
        approval_repository: ApprovalRepository | None = None,
        runtime_approval_repository: SQLAlchemyRuntimeApprovalRepository | None = None,
        approval_recorder: RuntimeApprovalRequestRecorder | None = None,
        user_input_repository: SQLAlchemyUserInputRequestRepository | None = None,
        limits: CycleLimits | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._runs = run_repository
        self._sessions = session_repository
        self._cycles = cycle_repository
        self._steps = step_repository
        self._provider_states = provider_state_repository
        self._events = event_repository
        self._leases = lease_manager
        self._context_compiler = context_compiler
        if context_usage_recorder is not None:
            self._context_usage_recorder = context_usage_recorder
        elif hasattr(context_compiler, "record_usage"):
            self._context_usage_recorder = cast(_ContextUsageRecorder, context_compiler)
        else:
            self._context_usage_recorder = None
        self._agent_engine = agent_engine
        self._transcript = transcript_repository
        self._deferred_executions = deferred_execution_dispatcher
        self._approvals = approval_repository
        self._runtime_approvals = runtime_approval_repository
        self._approval_recorder = approval_recorder
        self._user_inputs = user_input_repository
        self._limits = limits or CycleLimits()
        self._clock = clock
        self._state_machine = RuntimeStateMachine()

    async def run_cycle(self, request: RunCycleRequest) -> RunCycleResult:
        lease = await self._leases.acquire(request.run_id, request.worker_id)
        await self._append(request.run_id, LEASE_ACQUIRED, {"owner_id": request.worker_id})
        cycle: AgentCycle | None = None
        try:
            if request.cycle_id is not None:
                existing = await self._cycles.get(request.cycle_id)
                if existing is not None:
                    return self._completed_cycle_result(request, existing)
            run = await self._runs.get(request.run_id)
            if run is None:
                raise EntityNotFoundError("Run", request.run_id)
            run = await self._ensure_run_running(run)
            session = await self._sessions.get(request.session_id)
            if session is None or session.run_id != request.run_id:
                raise EntityNotFoundError("AgentSession", request.session_id)
            await self._ensure_active_session(session)
            latest_user_message_id = await self._persist_cycle_input(session, request)

            existing_cycles = await self._cycles.list_by_session(session.id)
            cycle = AgentCycle(
                id=request.cycle_id or new_id(),
                run_id=run.id,
                session_id=session.id,
                sequence=len(existing_cycles) + 1,
            )
            await self._cycles.create(cycle)
            await self._append(run.id, CYCLE_CREATED, {"cycle_id": cycle.id})
            self._state_machine.transition_cycle(cycle, CycleStatus.RUNNING)
            await self._cycles.save(cycle)
            await self._append(run.id, CYCLE_STARTED, {"cycle_id": cycle.id})

            if request.approval_id is not None:
                approval_result = await self._resume_approval(
                    run,
                    session,
                    cycle,
                    request,
                )
                if approval_result is not None:
                    return approval_result

            if request.compaction_required:
                return await self._yield_cycle(
                    run.id,
                    session,
                    cycle,
                    YieldReason.COMPACTION_REQUIRED,
                )

            compiled = await self._context_compiler.compile(
                ContextCompileRequest(
                    run_id=run.id,
                    session_id=session.id,
                    agent_id=session.agent_type,
                    model_profile=session.model_profile,
                    latest_user_message_id=latest_user_message_id,
                    objective=run.objective.description,
                    run_contract={
                        "objective": run.objective.description,
                        "success_criteria": [
                            item.model_dump(mode="json") for item in run.success_criteria
                        ],
                        "entry_points": [
                            item.model_dump(mode="json") for item in run.entry_points
                        ],
                        "scope": run.scope.model_dump(mode="json"),
                        "approval_mode": run.approval_mode.value,
                        "node_id": run.node_id,
                        "engagement_id": run.engagement_id,
                        "workspace": run.workspace_path,
                        "current_path": request.current_path or run.workspace_path,
                    },
                    engagement_path=request.engagement_path,
                    workspace_path=run.workspace_path,
                    current_path=request.current_path or run.workspace_path,
                    input_text=request.input_text,
                    input_items=request.input_items,
                )
            )
            await self._append(
                run.id,
                CONTEXT_COMPILED,
                {
                    "cycle_id": cycle.id,
                    "token_estimate": compiled.token_estimate,
                    "context_manifest": compiled.context_manifest,
                },
            )
            engine_request = AgentEngineRequest(
                session_id=session.id,
                model=session.model_profile,
                input_items=compiled.input_items,
                context=compiled,
                max_turns=self._limits.max_model_calls,
            )
            if session.provider_state_id is not None:
                provider_state = await self._provider_states.get(session.provider_state_id)
                if provider_state is None:
                    raise EntityNotFoundError("ProviderState", session.provider_state_id)
                engine_run = await self._agent_engine.resume(
                    AgentEngineResumeRequest(
                        **engine_request.model_dump(),
                        state=AgentEngineState.from_provider_state(provider_state),
                    )
                )
            else:
                engine_run = await self._agent_engine.start(engine_request)
            started_at = self._clock()
            pending_tool = False
            approval_required = False
            assistant_message_seen = False
            step_sequence = 0
            waiting_execution_id: str | None = None
            waiting_approval_id: str | None = None

            async for event in engine_run.events():
                await self._append_engine_event(run.id, cycle.id, event)
                await self._persist_engine_transcript(
                    session,
                    event,
                    skip_final_output=assistant_message_seen,
                )
                if event.event_type is AgentEngineEventType.ASSISTANT_MESSAGE:
                    assistant_message_seen = True
                if self._clock() - started_at >= self._limits.max_duration_seconds:
                    return await self._yield_cycle(
                        run.id,
                        session,
                        cycle,
                        YieldReason.CYCLE_LIMIT_REACHED,
                        engine_run=engine_run,
                    )
                if event.event_type is AgentEngineEventType.RUN_STARTED:
                    cycle.model_call_count += 1
                    await self._cycles.save(cycle)
                    if cycle.model_call_count >= self._limits.max_model_calls:
                        return await self._yield_cycle(
                            run.id,
                            session,
                            cycle,
                            YieldReason.CYCLE_LIMIT_REACHED,
                            engine_run=engine_run,
                        )
                elif event.event_type is AgentEngineEventType.USAGE:
                    if (
                        compiled.compilation_id is not None
                        and self._context_usage_recorder is not None
                    ):
                        await self._context_usage_recorder.record_usage(
                            compiled.compilation_id,
                            event.data,
                        )
                    extra_calls = int(event.data.get("model_calls", 0) or 0)
                    cycle.model_call_count += extra_calls
                    await self._cycles.save(cycle)
                    if cycle.model_call_count >= self._limits.max_model_calls:
                        return await self._yield_cycle(
                            run.id,
                            session,
                            cycle,
                            YieldReason.CYCLE_LIMIT_REACHED,
                            engine_run=engine_run,
                        )
                persisted_step: AgentStep | None = None
                if event.event_type in _STEP_TYPES:
                    step_sequence += 1
                    persisted_step = await self._persist_step(cycle, step_sequence, event)
                if event.event_type is AgentEngineEventType.TOOL_CALL_READY:
                    pending_tool = True
                    event_requires_approval = bool(event.data.get("approval_required", False))
                    tool_id = _event_tool_id(event)
                    approval_level = ApprovalLevel(
                        str(event.data.get("approval_level") or ApprovalLevel.SENSITIVE.value)
                    )
                    if self._approvals is not None:
                        granted = await self._approvals.is_granted(run.id, tool_id)
                        event_requires_approval = requires_approval(
                            run.approval_mode,
                            approval_level,
                            granted_for_run=granted,
                        )
                    approval_required = approval_required or event_requires_approval
                    cycle.tool_call_count += 1
                    await self._cycles.save(cycle)
                    if cycle.tool_call_count >= self._limits.max_tool_calls:
                        return await self._yield_cycle(
                            run.id,
                            session,
                            cycle,
                            YieldReason.CYCLE_LIMIT_REACHED,
                            engine_run=engine_run,
                        )
                    if self._deferred_executions is not None and persisted_step is not None:
                        intent = await self._deferred_executions.prepare(
                            session=session,
                            cycle=cycle,
                            step=persisted_step,
                            event=event,
                            status=(
                                ToolCallStatus.WAITING_APPROVAL
                                if event_requires_approval
                                else ToolCallStatus.READY
                            ),
                        )
                        if event_requires_approval:
                            if self._approval_recorder is None:
                                raise DomainError(
                                    "Runtime approval recorder is required for an approval "
                                    "Tool Call"
                                )
                            request_record = await self._approval_recorder.record(
                                run,
                                session=session,
                                cycle=cycle,
                                step=persisted_step,
                                intent=intent,
                                context_compilation_id=compiled.compilation_id,
                                working_memory_version=_working_memory_version(
                                    compiled.context_manifest
                                ),
                            )
                            if (
                                waiting_approval_id is not None
                                and waiting_approval_id != request_record.id
                            ):
                                raise DomainError(
                                    "one Runtime cycle cannot defer multiple Approvals"
                                )
                            waiting_approval_id = request_record.id
                            continue
                        execution = await self._deferred_executions.execute_intent(intent)
                        if (
                            waiting_execution_id is not None
                            and waiting_execution_id != execution.id
                        ):
                            raise DomainError(
                                "one Runtime cycle cannot defer multiple Executions"
                            )
                        waiting_execution_id = execution.id
                if event.event_type is AgentEngineEventType.ASSISTANT_MESSAGE and event.data.get(
                    "requires_user_input"
                ):
                    waiting_input_id = None
                    if self._user_inputs is not None:
                        input_request = await self._user_inputs.create(
                            UserInputRequest(
                                run_id=run.id,
                                session_id=session.id,
                                cycle_id=cycle.id,
                                prompt=_event_content(event.data),
                                context_compilation_id=compiled.compilation_id,
                                working_memory_version=_working_memory_version(
                                    compiled.context_manifest
                                ),
                            )
                        )
                        waiting_input_id = input_request.id
                        await self._append(
                            run.id,
                            "user.input_required",
                            {
                                "user_input_request_id": input_request.id,
                                "cycle_id": cycle.id,
                                "prompt": input_request.prompt,
                                "context_compilation_id": compiled.compilation_id,
                                "working_memory_version": input_request.working_memory_version,
                            },
                        )
                    return await self._yield_cycle(
                        run.id,
                        session,
                        cycle,
                        YieldReason.USER_INPUT_REQUIRED,
                        engine_run=engine_run,
                        waiting_object_id=waiting_input_id,
                    )
                if event.event_type is AgentEngineEventType.ERROR:
                    reason = (
                        YieldReason.RETRYABLE_FAILURE
                        if event.data.get("retryable", False)
                        else YieldReason.FATAL_FAILURE
                    )
                    return await self._yield_cycle(
                        run.id, session, cycle, reason, engine_run=engine_run
                    )
                if event.event_type is AgentEngineEventType.RUN_COMPLETED:
                    if approval_required:
                        reason = YieldReason.APPROVAL_REQUIRED
                    elif pending_tool:
                        reason = YieldReason.TOOL_RUNNING
                    else:
                        reason = YieldReason.RUN_COMPLETED
                    return await self._yield_cycle(
                        run.id,
                        session,
                        cycle,
                        reason,
                        engine_run=engine_run if reason is not YieldReason.RUN_COMPLETED else None,
                        waiting_execution_id=(
                            waiting_execution_id
                            if reason is YieldReason.TOOL_RUNNING
                            else None
                        ),
                        waiting_object_id=(
                            waiting_approval_id
                            if reason is YieldReason.APPROVAL_REQUIRED
                            else waiting_execution_id
                        ),
                    )

            return await self._yield_cycle(
                run.id,
                session,
                cycle,
                YieldReason.CYCLE_LIMIT_REACHED,
                engine_run=engine_run,
            )
        except Exception as exc:
            if cycle is not None and cycle.status is CycleStatus.RUNNING:
                self._state_machine.transition_cycle(cycle, CycleStatus.FAILED)
                await self._cycles.save(cycle)
                await self._append(
                    request.run_id,
                    CYCLE_FAILED,
                    {"cycle_id": cycle.id, "error_type": type(exc).__name__},
                )
            raise
        finally:
            await lease.release()
            await self._append(request.run_id, LEASE_RELEASED, {"owner_id": request.worker_id})

    def _completed_cycle_result(
        self,
        request: RunCycleRequest,
        cycle: AgentCycle,
    ) -> RunCycleResult:
        if cycle.run_id != request.run_id or cycle.session_id != request.session_id:
            raise DomainError(
                f"Cycle {cycle.id!r} does not belong to Run/Session "
                f"{request.run_id!r}/{request.session_id!r}"
            )
        if cycle.status is not CycleStatus.YIELDED or cycle.yield_reason is None:
            raise DomainError(
                f"Cycle {cycle.id!r} cannot be replayed from status {cycle.status.value!r}"
            )
        return RunCycleResult(
            run_id=cycle.run_id,
            session_id=cycle.session_id,
            cycle_id=cycle.id,
            yield_reason=cycle.yield_reason,
            model_call_count=cycle.model_call_count,
            tool_call_count=cycle.tool_call_count,
            provider_state_id=cycle.checkpoint_id,
            waiting_execution_id=cycle.waiting_object_id,
            waiting_object_id=cycle.waiting_object_id,
        )

    async def _ensure_run_running(self, run: Run) -> Run:
        if run.status is RunStatus.CREATED:
            run = await self._runs.update_status(run.id, RunStatus.INITIALIZING)
        if run.status is RunStatus.INITIALIZING:
            run = await self._runs.update_status(run.id, RunStatus.READY)
        if run.status in {
            RunStatus.READY,
            RunStatus.PREPARING,
            RunStatus.WAITING_TOOL,
            RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_USER,
            RunStatus.PAUSED,
            RunStatus.COMPACTING,
        }:
            run = await self._runs.update_status(run.id, RunStatus.RUNNING)
        if run.status is not RunStatus.RUNNING:
            raise DomainError(f"Run {run.id!r} cannot start a Runtime cycle from {run.status}")
        return run

    async def _transition_run_for_yield(self, run_id: str, reason: YieldReason) -> None:
        targets = {
            YieldReason.TOOL_RUNNING: RunStatus.WAITING_TOOL,
            YieldReason.APPROVAL_REQUIRED: RunStatus.WAITING_APPROVAL,
            YieldReason.USER_INPUT_REQUIRED: RunStatus.WAITING_USER,
            YieldReason.COMPACTION_REQUIRED: RunStatus.COMPACTING,
            YieldReason.FATAL_FAILURE: RunStatus.FAILED,
        }
        if reason is YieldReason.RUN_COMPLETED:
            await self._runs.update_status(run_id, RunStatus.COMPLETING)
            await self._runs.update_status(run_id, RunStatus.COMPLETED)
        elif target := targets.get(reason):
            await self._runs.update_status(run_id, target)

    async def _ensure_active_session(self, session: AgentSession) -> None:
        if session.status is SessionStatus.CREATED:
            self._state_machine.transition_session(session, SessionStatus.ACTIVE)
            await self._sessions.save(session)
            await self._append(session.run_id, SESSION_ACTIVATED, {"session_id": session.id})

    async def _persist_cycle_input(
        self, session: AgentSession, request: RunCycleRequest
    ) -> str | None:
        latest_message_id = request.latest_user_message_id
        if self._transcript is None or latest_message_id is not None:
            return latest_message_id
        drafts: list[TranscriptMessageDraft] = []
        if request.input_text:
            drafts.append(
                TranscriptMessageDraft(
                    agent_id=session.agent_type,
                    role=MessageRole.USER,
                    message_type=MessageType.USER_MESSAGE,
                    content=request.input_text,
                    visibility=MessageVisibility.USER_VISIBLE,
                )
            )
        for item in request.input_items:
            if item.get("role") != MessageRole.USER.value:
                continue
            drafts.append(
                TranscriptMessageDraft(
                    agent_id=session.agent_type,
                    role=MessageRole.USER,
                    message_type=MessageType.USER_MESSAGE,
                    content=_event_content(item),
                    structured_content=item,
                    visibility=MessageVisibility.USER_VISIBLE,
                )
            )
        if not drafts:
            return None
        messages = await self._transcript.append_many(session.id, drafts)
        return messages[-1].id

    async def _persist_engine_transcript(
        self,
        session: AgentSession,
        event: AgentEngineEvent,
        *,
        skip_final_output: bool,
    ) -> None:
        if self._transcript is None:
            return
        if event.event_type is AgentEngineEventType.FINAL_OUTPUT and skip_final_output:
            return
        mapping = {
            AgentEngineEventType.ASSISTANT_MESSAGE: (
                MessageRole.ASSISTANT,
                MessageType.ASSISTANT_MESSAGE,
                MessageVisibility.USER_VISIBLE,
            ),
            AgentEngineEventType.FINAL_OUTPUT: (
                MessageRole.ASSISTANT,
                MessageType.ASSISTANT_MESSAGE,
                MessageVisibility.USER_VISIBLE,
            ),
            AgentEngineEventType.TOOL_CALL_READY: (
                MessageRole.ASSISTANT,
                MessageType.TOOL_CALL,
                MessageVisibility.AGENT_ONLY,
            ),
            AgentEngineEventType.SUBAGENT_REQUESTED: (
                MessageRole.ASSISTANT,
                MessageType.SUBAGENT_DELEGATION,
                MessageVisibility.AGENT_ONLY,
            ),
            AgentEngineEventType.PLAN_UPDATE: (
                MessageRole.ASSISTANT,
                MessageType.ASSISTANT_MESSAGE,
                MessageVisibility.AGENT_ONLY,
            ),
        }
        metadata = mapping.get(event.event_type)
        if metadata is None:
            return
        role, message_type, visibility = metadata
        tool_call_id = event.data.get("call_id")
        await self._transcript.append(
            session.id,
            TranscriptMessageDraft(
                agent_id=session.agent_type,
                role=role,
                message_type=message_type,
                content=_event_content(event.data),
                structured_content=event.data,
                tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
                visibility=visibility,
            ),
        )

    async def _persist_step(
        self, cycle: AgentCycle, sequence: int, event: AgentEngineEvent
    ) -> AgentStep:
        step = AgentStep(
            cycle_id=cycle.id,
            sequence=sequence,
            step_type=_STEP_TYPES[event.event_type],
            input_refs=[str(value) for value in event.data.get("input_refs", [])],
            output_refs=[str(value) for value in event.data.get("output_refs", [])],
        )
        await self._steps.create(step)
        self._state_machine.transition_step(step, StepStatus.RUNNING)
        await self._steps.save(step)
        await self._append(
            cycle.run_id,
            STEP_STARTED,
            {"cycle_id": cycle.id, "step_id": step.id, "step_type": step.step_type.value},
        )
        self._state_machine.transition_step(step, StepStatus.COMPLETED)
        await self._steps.save(step)
        await self._append(
            cycle.run_id,
            STEP_COMPLETED,
            {"cycle_id": cycle.id, "step_id": step.id, "step_type": step.step_type.value},
        )
        return step

    async def _yield_cycle(
        self,
        run_id: str,
        session: AgentSession,
        cycle: AgentCycle,
        reason: YieldReason,
        *,
        engine_run: AgentEngineRun | None = None,
        waiting_execution_id: str | None = None,
        waiting_object_id: str | None = None,
    ) -> RunCycleResult:
        provider_state_id = session.provider_state_id
        if engine_run is not None:
            state = await engine_run.suspend()
            provider_state = state.to_provider_state(session.id)
            await self._provider_states.create(provider_state)
            provider_state_id = provider_state.id
            session.provider_state_id = provider_state.id
        session.turn_count += cycle.model_call_count
        session.model_call_count += cycle.model_call_count
        session.tool_call_count += cycle.tool_call_count
        if self._transcript is not None:
            await self._transcript.append(
                session.id,
                TranscriptMessageDraft(
                    agent_id=session.agent_type,
                    role=MessageRole.SYSTEM,
                    message_type=MessageType.CHECKPOINT_BOUNDARY,
                    structured_content={
                        "cycle_id": cycle.id,
                        "yield_reason": reason.value,
                        "provider_state_id": provider_state_id,
                        "waiting_execution_id": waiting_execution_id,
                        "waiting_object_id": waiting_object_id,
                    },
                    visibility=MessageVisibility.INTERNAL_STATE,
                ),
            )
        await self._sessions.save(session)
        self._state_machine.transition_cycle(
            cycle,
            CycleStatus.YIELDED,
            yield_reason=reason,
        )
        cycle.waiting_object_id = waiting_object_id or waiting_execution_id
        cycle.checkpoint_id = provider_state_id or session.latest_checkpoint_id
        await self._cycles.save(cycle)
        await self._transition_run_for_yield(run_id, reason)
        await self._append(
            run_id,
            CYCLE_YIELDED,
            {
                "cycle_id": cycle.id,
                "yield_reason": reason.value,
                "model_call_count": cycle.model_call_count,
                "tool_call_count": cycle.tool_call_count,
                "waiting_execution_id": waiting_execution_id,
                "waiting_object_id": cycle.waiting_object_id,
            },
        )
        if (
            reason is YieldReason.APPROVAL_REQUIRED
            and cycle.waiting_object_id is not None
            and self._runtime_approvals is not None
        ):
            approval = await self._runtime_approvals.get(cycle.waiting_object_id)
            if approval is not None and approval.provider_state_id != provider_state_id:
                approval.provider_state_id = provider_state_id
                await self._runtime_approvals.save(approval)
        if (
            reason is YieldReason.USER_INPUT_REQUIRED
            and cycle.waiting_object_id is not None
            and self._user_inputs is not None
        ):
            input_request = await self._user_inputs.get(cycle.waiting_object_id)
            if (
                input_request is not None
                and input_request.provider_state_id != provider_state_id
            ):
                input_request.provider_state_id = provider_state_id
                await self._user_inputs.save(input_request)
        return RunCycleResult(
            run_id=run_id,
            session_id=session.id,
            cycle_id=cycle.id,
            yield_reason=reason,
            model_call_count=cycle.model_call_count,
            tool_call_count=cycle.tool_call_count,
            provider_state_id=provider_state_id,
            waiting_object_id=cycle.waiting_object_id,
            waiting_execution_id=waiting_execution_id,
        )

    async def _resume_approval(
        self,
        run: Run,
        session: AgentSession,
        cycle: AgentCycle,
        request: RunCycleRequest,
    ) -> RunCycleResult | None:
        approval_id = request.approval_id
        if (
            approval_id is None
            or self._runtime_approvals is None
            or self._deferred_executions is None
        ):
            raise DomainError("Runtime approval recovery dependencies are unavailable")
        approval = await self._runtime_approvals.get(approval_id)
        if approval is None:
            raise EntityNotFoundError("RuntimeApprovalRequest", approval_id)
        if approval.run_id != run.id or approval.session_id != session.id:
            raise DomainError("Runtime Approval does not belong to this Run and Session")
        if approval.decision in {
            ApprovalDecision.APPROVE_ONCE,
            ApprovalDecision.APPROVE_TOOL_FOR_RUN,
        }:
            execution = await self._deferred_executions.execute_approved_intent(
                approval.tool_call_intent_id
            )
            return await self._yield_cycle(
                run.id,
                session,
                cycle,
                YieldReason.TOOL_RUNNING,
                waiting_execution_id=execution.id,
                waiting_object_id=execution.id,
            )
        if approval.decision in {
            ApprovalDecision.REJECT,
            ApprovalDecision.REJECT_WITH_FEEDBACK,
        }:
            await self._deferred_executions.reject_intent(approval.tool_call_intent_id)
            request.input_items.append(
                {
                    "type": "approval_decision",
                    "approval_id": approval.id,
                    "decision": approval.decision.value,
                    "feedback": approval.feedback,
                    "tool_call_intent_id": approval.tool_call_intent_id,
                    "source_refs": [f"approval://{approval.id}"],
                }
            )
            return None
        raise DomainError(f"Runtime Approval {approval.id!r} has no durable decision")

    async def _append_engine_event(
        self, run_id: str, cycle_id: str, event: AgentEngineEvent
    ) -> None:
        await self._append(
            run_id,
            ENGINE_EVENT,
            {
                "cycle_id": cycle_id,
                "engine_sequence": event.sequence,
                "event_type": event.event_type.value,
                "data": event.data,
            },
        )

    async def _append(self, run_id: str, event_type: str, payload: dict[str, object]) -> None:
        await self._events.append(run_id, event_type, payload)


def _event_content(data: dict[str, object]) -> str:
    for key in ("text", "output", "content", "message", "delta"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _event_tool_id(event: AgentEngineEvent) -> str:
    value = event.data.get("tool_id") or event.data.get("name")
    arguments = event.data.get("arguments")
    if value == "run_registered_tool" and isinstance(arguments, dict):
        value = arguments.get("tool_id")
    if not isinstance(value, str) or not value:
        raise DomainError("Tool Call event is missing a tool ID")
    return value


def _working_memory_version(manifest: Mapping[str, object]) -> int | None:
    categories = manifest.get("categories")
    if not isinstance(categories, Mapping):
        return None
    for category in categories.values():
        if not isinstance(category, Mapping):
            continue
        refs = category.get("source_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, str) or not ref.startswith("working-memory://"):
                continue
            _, separator, version = ref.rpartition("/versions/")
            if separator and version.isdigit() and int(version) >= 1:
                return int(version)
    return None
