"""Finite durable Agent Runtime cycle coordinator."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from time import monotonic
from typing import NoReturn, Protocol, cast

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    PentestBudgetExceededError,
    pentest_budget_exhaustion_details,
)
from riftx.application.ports import ApprovalRepository
from riftx.application.services import (
    CLOSURE_EVALUATED_EVENT_TYPE,
    ClosureVerifierApplicationService,
    CreateTerminal,
    RunSafetyStopService,
    RuntimeApprovalRequestRecorder,
    TerminalApplicationService,
    closure_event_id,
    closure_event_payload,
    stop_resources_payload,
)
from riftx.domain import (
    ApprovalLevel,
    DomainError,
    ExecutorType,
    MessageRole,
    MessageType,
    MessageVisibility,
    Run,
    RunKind,
    RunStatus,
    TranscriptMessageDraft,
    requires_approval,
)
from riftx.domain.base import new_id
from riftx.execution import DeferredExecutionDispatcher, DeferredExecutionSpec
from riftx.hooks import HookBus, HookDecision, HookPoint, HookRequest
from riftx.observer import SupervisorDisposition, SupervisorReport
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
    OBSERVER_INSPECTED,
    SESSION_ACTIVATED,
    STEP_COMPLETED,
    STEP_STARTED,
)
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import (
    ContextCompiler,
    ContextCompileRequest,
    ContextPurpose,
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
    ToolCallIntent,
    ToolCallStatus,
    UserInputRequest,
    YieldReason,
)
from riftx.tools.policy import (
    AGENT_TOOL_POLICIES,
    AgentToolAuthorization,
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


class SubagentBatchExecutor(Protocol):
    async def execute(
        self,
        parent_session_id: str,
        requests: list[dict[str, object]],
    ) -> object: ...


class BudgetExhaustionHandler(Protocol):
    def __call__(self, run_id: str) -> Awaitable[object]: ...


class RuntimeObserver(Protocol):
    async def inspect(
        self,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        limits: CycleLimits,
        elapsed_seconds: float,
        available_tool_ids: Collection[str],
        available_skill_ids: Collection[str] = (),
    ) -> SupervisorReport: ...


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
        terminal_service: TerminalApplicationService | None = None,
        safety_stopper: RunSafetyStopService | None = None,
        hooks: HookBus | None = None,
        observer: RuntimeObserver | None = None,
        closure_verifier: ClosureVerifierApplicationService | None = None,
        subagent_executor: SubagentBatchExecutor | None = None,
        budget_exhaustion_handler: BudgetExhaustionHandler | None = None,
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
        self._context_usage_recorder: _ContextUsageRecorder | None
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
        self._terminal_service = terminal_service
        self._safety_stopper = safety_stopper
        self._hooks = hooks
        self._observer = observer
        self._closure_verifier = closure_verifier
        self._subagent_executor = subagent_executor
        self._budget_exhaustion_handler = budget_exhaustion_handler
        self._limits = limits or CycleLimits()
        self._clock = clock
        self._state_machine = RuntimeStateMachine()

    def bind_subagent_executor(self, executor: SubagentBatchExecutor) -> None:
        """Finish the intentional coordinator/orchestrator composition cycle once."""

        if self._subagent_executor is not None:
            raise DomainError("Subagent executor is already configured")
        self._subagent_executor = executor

    async def _require_agent_cycle_admission(
        self,
        run_id: str,
        session_id: str,
        *,
        allow_missing_session: bool = False,
        activity: bool = False,
    ) -> tuple[Run, AgentSession | None]:
        """Prove Session ownership and RunKind before any Runtime side effect."""

        session = await self._sessions.get(session_id)
        if session is None:
            if not allow_missing_session:
                raise EntityNotFoundError("AgentSession", session_id)
        elif session.run_id != run_id:
            # Preserve the owner-proof error precedence even for a Code Audit Run.
            raise EntityNotFoundError("AgentSession", session_id)

        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        _require_agent_cycle_effect_policy(run, activity=activity)
        return run, session

    async def run_cycle(self, request: RunCycleRequest) -> RunCycleResult:
        run, session = await self._require_agent_cycle_admission(
            request.run_id,
            request.session_id,
        )
        assert session is not None
        if request.subagent_mode and session.parent_session_id is None:
            raise DomainError("Subagent cycle mode requires a child AgentSession")
        if not request.subagent_mode and session.parent_session_id is not None:
            raise DomainError("Child AgentSession requires Subagent cycle mode")
        lease = None
        if not request.subagent_mode:
            lease = await self._leases.acquire(request.run_id, request.worker_id)
            await self._append(request.run_id, LEASE_ACQUIRED, {"owner_id": request.worker_id})
        cycle: AgentCycle | None = None
        try:
            if request.cycle_id is not None:
                existing = await self._cycles.get(request.cycle_id)
                if existing is not None:
                    completed = self._completed_cycle_result(request, existing)
                    if (
                        existing.session_id == request.session_id
                        and not request.subagent_mode
                        and existing.yield_reason
                        in {YieldReason.RUN_COMPLETED, YieldReason.FATAL_FAILURE}
                    ):
                        # A previous non-deferred attempt may have persisted
                        # the Cycle and COMPLETING fence before a stop
                        # controller failed. Resume only safety finalization;
                        # never re-run the model for the same durable Cycle ID.
                        await self._transition_run_for_yield(
                            request.run_id,
                            existing.yield_reason,
                            defer_run_completion=request.defer_run_completion,
                        )
                    return completed
            run = await self._ensure_run_running(run)
            await self._ensure_active_session(session)
            has_new_canonical_input = (
                request.latest_user_message_id is not None
                or request.input_text is not None
                or bool(request.input_items)
            )
            persisted_tool_results = await self._persist_tool_result_inputs(
                session,
                request.input_items,
            )
            if persisted_tool_results:
                request.input_items = [
                    item
                    for item in request.input_items
                    if _execution_id_from_tool_result_input(item) not in persisted_tool_results
                ]
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
            cycle_started_at = self._clock() if self._observer is not None else 0.0

            provider_approval_item: dict[str, object] | None = None
            if request.approval_id is not None:
                provider_approval_item = await self._resume_approval(
                    run,
                    session,
                    cycle,
                    request,
                )
                has_new_canonical_input |= provider_approval_item is None

            pending_result = await self._yield_for_next_pending_intent(
                run,
                session,
                cycle,
            )
            if pending_result is not None:
                return pending_result

            if request.compaction_required:
                return await self._yield_cycle(
                    run.id,
                    session,
                    cycle,
                    YieldReason.COMPACTION_REQUIRED,
                )

            delegation = await self._subagent_delegation(session)
            run_contract: dict[str, object] = {
                "objective": run.objective.description,
                "success_criteria": [item.model_dump(mode="json") for item in run.success_criteria],
                "entry_points": [item.model_dump(mode="json") for item in run.entry_points],
                "scope": run.scope.model_dump(mode="json"),
                "approval_mode": run.approval_mode.value,
                "node_id": run.node_id,
                "engagement_id": run.engagement_id,
                "workspace": run.workspace_path,
                "current_path": request.current_path or run.workspace_path,
            }
            if delegation is not None:
                run_contract = {
                    "task_id": delegation.get("task_id", ""),
                    "run_contract_summary": delegation.get("run_contract_summary", ""),
                    "relevant_scope": delegation.get("relevant_scope", []),
                    "expected_output_schema": delegation.get("expected_output_schema", {}),
                    "constraints": delegation.get("constraints", []),
                    "stop_conditions": delegation.get("stop_conditions", []),
                    "workspace": delegation.get("workspace", run.workspace_path),
                    "node_id": run.node_id,
                    "engagement_id": run.engagement_id,
                }
            context_request = ContextCompileRequest(
                run_id=run.id,
                session_id=session.id,
                agent_id=session.agent_type,
                purpose=(
                    ContextPurpose.SUBAGENT_DELEGATION
                    if delegation is not None
                    else ContextPurpose.PRIMARY_REASONING
                ),
                model_profile=session.model_profile,
                latest_user_message_id=latest_user_message_id,
                objective=(
                    str(delegation.get("task") or "")
                    if delegation is not None
                    else run.objective.description
                ),
                run_contract=run_contract,
                engagement_path=request.engagement_path,
                workspace_path=run.workspace_path,
                current_path=request.current_path or run.workspace_path,
                input_text=request.input_text,
                input_items=request.input_items,
                selected_fact_ids=_string_list(
                    delegation.get("selected_fact_ids") if delegation is not None else None
                ),
                selected_memory_ids=_string_list(
                    delegation.get("selected_memory_ids") if delegation is not None else None
                ),
            )
            before_context = await self._dispatch_hook(
                HookPoint.BEFORE_CONTEXT_COMPILE,
                run_id=run.id,
                session_id=session.id,
                cycle_id=cycle.id,
                payload={
                    "input_text": context_request.input_text,
                    "input_items": context_request.input_items,
                },
            )
            context_request.input_text = _optional_string(before_context.get("input_text"))
            context_request.input_items = _object_list(before_context.get("input_items"))
            compiled = await self._context_compiler.compile(context_request)
            await self._dispatch_hook(
                HookPoint.AFTER_CONTEXT_COMPILE,
                run_id=run.id,
                session_id=session.id,
                cycle_id=cycle.id,
                payload={
                    "compilation_id": compiled.compilation_id,
                    "token_estimate": compiled.token_estimate,
                    "context_manifest": compiled.context_manifest,
                },
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
            observer_result = await self._inspect_observer(
                session=session,
                cycle=cycle,
                elapsed_seconds=(
                    self._clock() - cycle_started_at if self._observer is not None else 0.0
                ),
                available_tool_ids=_compiled_capability_ids(
                    compiled.available_tools,
                    keys=("name", "id"),
                ),
                available_skill_ids=_compiled_capability_ids(
                    compiled.available_skills,
                    keys=("id", "name"),
                ),
                phase="pre_model",
                defer_run_completion=request.defer_run_completion,
            )
            if observer_result is not None:
                return observer_result
            engine_request = AgentEngineRequest(
                session_id=session.id,
                model=session.model_profile,
                input_items=compiled.input_items,
                context=compiled,
                max_turns=self._limits.max_model_calls,
            )
            before_model = await self._dispatch_hook(
                HookPoint.BEFORE_MODEL_CALL,
                run_id=run.id,
                session_id=session.id,
                cycle_id=cycle.id,
                payload={
                    "model": engine_request.model,
                    "input_items": engine_request.input_items,
                    "max_turns": engine_request.max_turns,
                },
            )
            engine_request.input_items = _object_list(before_model.get("input_items"))
            max_turns = before_model.get("max_turns")
            if isinstance(max_turns, int):
                engine_request.max_turns = max_turns
            if run.kind is RunKind.PENTEST:
                if compiled.compilation_id is None:
                    raise ApplicationConflictError(
                        "pentest_token_usage_unavailable",
                        "Pentest model usage requires a durable Context Compilation",
                        details={"run_id": run.id, "cycle_id": cycle.id},
                    )
                try:
                    cycle = await self._cycles.claim_pentest_model_call(
                        run_id=run.id,
                        cycle_id=cycle.id,
                        compilation_id=compiled.compilation_id,
                    )
                except PentestBudgetExceededError as exc:
                    await self._raise_pentest_budget_exhausted(run.id, exc)
            if session.provider_state_id is not None and not has_new_canonical_input:
                provider_state = await self._provider_states.get(session.provider_state_id)
                if provider_state is None:
                    raise EntityNotFoundError("ProviderState", session.provider_state_id)
                resume_input_items = [
                    item
                    for item in engine_request.input_items
                    if item.get("type") != "approval_decision"
                ]
                if provider_approval_item is not None:
                    resume_input_items.append(provider_approval_item)
                engine_run = await self._agent_engine.resume(
                    AgentEngineResumeRequest(
                        **engine_request.model_dump(exclude={"input_items"}),
                        input_items=resume_input_items,
                        state=AgentEngineState.from_provider_state(provider_state),
                    )
                )
            else:
                if session.provider_state_id is not None:
                    superseded_provider_state_id = session.provider_state_id
                    session.provider_state_id = None
                    await self._sessions.save(session)
                    await self._append(
                        run.id,
                        "runtime.provider_state_superseded",
                        {
                            "provider_state_id": superseded_provider_state_id,
                            "reason": "new_canonical_input",
                        },
                    )
                engine_run = await self._agent_engine.start(engine_request)
            started_at = self._clock()
            pending_tool = False
            approval_required = False
            assistant_message_seen = False
            step_sequence = 0
            subagent_requests: list[dict[str, object]] = []
            counted_tool_calls: set[str] = set()

            async for event in cast(AsyncIterator[AgentEngineEvent], engine_run.events()):
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
                    if run.kind is not RunKind.PENTEST:
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
                    model_calls = event.data.get("model_calls", 0)
                    extra_calls = model_calls if isinstance(model_calls, int) else 0
                    if run.kind is not RunKind.PENTEST:
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
                if event.event_type in {
                    AgentEngineEventType.TOOL_CALL_STARTED,
                    AgentEngineEventType.TOOL_CALL_READY,
                }:
                    call_id = _optional_string(event.data.get("call_id"))
                    call_key = (
                        f"call:{call_id}" if call_id is not None else f"event:{event.sequence}"
                    )
                    if call_key not in counted_tool_calls:
                        counted_tool_calls.add(call_key)
                        if run.kind is not RunKind.PENTEST:
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
                if event.event_type is AgentEngineEventType.TOOL_CALL_READY:
                    hook_result = await self._dispatch_hook(
                        HookPoint.BEFORE_TOOL_EXECUTION,
                        run_id=run.id,
                        session_id=session.id,
                        cycle_id=cycle.id,
                        step_id=persisted_step.id if persisted_step is not None else None,
                        payload=event.data,
                        allow_require_approval=True,
                    )
                    event.data = hook_result
                    pending_tool = True
                    event_requires_approval = bool(event.data.get("approval_required", False))
                    hook_requires_approval = bool(hook_result.pop("_hook_require_approval", False))
                    tool_id = _event_tool_id(event)
                    explicit_control = _is_explicit_control_event(event)
                    approval_level = ApprovalLevel(
                        str(event.data.get("approval_level") or ApprovalLevel.SENSITIVE.value)
                    )
                    if explicit_control:
                        event_requires_approval = True
                    elif self._approvals is not None:
                        granted = await self._approvals.is_granted(run.id, tool_id)
                        event_requires_approval = hook_requires_approval or requires_approval(
                            run.approval_mode,
                            approval_level,
                            granted_for_run=granted,
                        )
                    else:
                        event_requires_approval = event_requires_approval or hook_requires_approval
                    approval_required = approval_required or event_requires_approval
                    if self._deferred_executions is not None and persisted_step is not None:
                        intent = (
                            await self._deferred_executions.prepare_control(
                                session=session,
                                cycle=cycle,
                                step=persisted_step,
                                event=event,
                            )
                            if explicit_control
                            else await self._deferred_executions.prepare(
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
                        )
                        if event_requires_approval:
                            await self._dispatch_hook(
                                HookPoint.APPROVAL_REQUIRED,
                                run_id=run.id,
                                session_id=session.id,
                                cycle_id=cycle.id,
                                step_id=persisted_step.id,
                                payload={
                                    "tool_call_intent_id": intent.id,
                                    "tool_id": intent.tool_id,
                                    "approval_level": intent.approval_level.value,
                                },
                            )
                            if self._approval_recorder is None:
                                raise DomainError(
                                    "Runtime approval recorder is required for an approval "
                                    "Tool Call"
                                )
                            await self._approval_recorder.record(
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
                        observer_result = await self._inspect_observer(
                            session=session,
                            cycle=cycle,
                            elapsed_seconds=(
                                self._clock() - cycle_started_at
                                if self._observer is not None
                                else 0.0
                            ),
                            available_tool_ids=_compiled_capability_ids(
                                compiled.available_tools,
                                keys=("name", "id"),
                            ),
                            available_skill_ids=_compiled_capability_ids(
                                compiled.available_skills,
                                keys=("id", "name"),
                            ),
                            phase="tool_intent",
                            engine_run=engine_run,
                            defer_run_completion=request.defer_run_completion,
                        )
                        if observer_result is not None:
                            return observer_result
                        if event_requires_approval:
                            continue
                if event.event_type is AgentEngineEventType.SUBAGENT_REQUESTED:
                    subagent_requests.append(dict(event.data))
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
                    await self._dispatch_hook(
                        HookPoint.AFTER_MODEL_CALL,
                        run_id=run.id,
                        session_id=session.id,
                        cycle_id=cycle.id,
                        payload={"status": "error", "event": event.data},
                    )
                    reason = (
                        YieldReason.RETRYABLE_FAILURE
                        if event.data.get("retryable", False)
                        else YieldReason.FATAL_FAILURE
                    )
                    return await self._yield_cycle(
                        run.id,
                        session,
                        cycle,
                        reason,
                        engine_run=engine_run,
                        defer_run_completion=request.defer_run_completion,
                    )
                if event.event_type is AgentEngineEventType.RUN_COMPLETED:
                    await self._dispatch_hook(
                        HookPoint.AFTER_MODEL_CALL,
                        run_id=run.id,
                        session_id=session.id,
                        cycle_id=cycle.id,
                        payload={
                            "status": "completed",
                            "model_call_count": cycle.model_call_count,
                            "tool_call_count": cycle.tool_call_count,
                        },
                    )
                    if subagent_requests:
                        if self._subagent_executor is None:
                            raise DomainError(
                                "Subagent delegation requested but no executor is configured"
                            )
                        await self._subagent_executor.execute(
                            session.id,
                            subagent_requests,
                        )
                        return await self._yield_cycle(
                            run.id,
                            session,
                            cycle,
                            YieldReason.CYCLE_LIMIT_REACHED,
                        )
                    if pending_tool:
                        pending_result = await self._yield_for_next_pending_intent(
                            run,
                            session,
                            cycle,
                            engine_run=engine_run,
                        )
                        if pending_result is not None:
                            return pending_result
                        if self._deferred_executions is None:
                            return await self._yield_cycle(
                                run.id,
                                session,
                                cycle,
                                (
                                    YieldReason.APPROVAL_REQUIRED
                                    if approval_required
                                    else YieldReason.TOOL_RUNNING
                                ),
                                engine_run=engine_run,
                            )
                    return await self._yield_cycle(
                        run.id,
                        session,
                        cycle,
                        YieldReason.RUN_COMPLETED,
                        defer_run_completion=request.defer_run_completion,
                    )

            if pending_tool:
                pending_result = await self._yield_for_next_pending_intent(
                    run,
                    session,
                    cycle,
                    engine_run=engine_run,
                )
                if pending_result is not None:
                    return pending_result

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
            if lease is not None:
                await lease.release()
                await self._append(
                    request.run_id,
                    LEASE_RELEASED,
                    {"owner_id": request.worker_id},
                )

    async def _inspect_observer(
        self,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        elapsed_seconds: float,
        available_tool_ids: Collection[str],
        available_skill_ids: Collection[str],
        phase: str,
        engine_run: AgentEngineRun | None = None,
        defer_run_completion: bool = False,
    ) -> RunCycleResult | None:
        if self._observer is None:
            return None
        report = await self._observer.inspect(
            session=session,
            cycle=cycle,
            limits=self._limits,
            elapsed_seconds=elapsed_seconds,
            available_tool_ids=available_tool_ids,
            available_skill_ids=available_skill_ids,
        )
        await self._append(
            session.run_id,
            OBSERVER_INSPECTED,
            {
                "cycle_id": cycle.id,
                "phase": phase,
                "disposition": report.disposition.value,
                "yield_reason": (
                    report.yield_reason.value if report.yield_reason is not None else None
                ),
                "signals": [
                    {
                        "code": signal.code,
                        "check": signal.check.value,
                        "severity": signal.severity.value,
                        "refs": list(signal.refs),
                    }
                    for signal in report.signals
                ],
            },
        )
        if report.disposition is SupervisorDisposition.CONTINUE:
            return None
        reason = (
            YieldReason.FATAL_FAILURE
            if report.disposition is SupervisorDisposition.BLOCK
            else report.yield_reason
        )
        if reason is None:
            raise DomainError("Observer yielded without a durable Yield Reason")
        return await self._yield_cycle(
            session.run_id,
            session,
            cycle,
            reason,
            engine_run=engine_run,
            waiting_object_id=_observer_waiting_object_id(report),
            defer_run_completion=defer_run_completion,
        )

    async def _dispatch_hook(
        self,
        point: HookPoint,
        *,
        run_id: str,
        session_id: str | None,
        cycle_id: str | None,
        payload: dict[str, object],
        step_id: str | None = None,
        allow_require_approval: bool = False,
    ) -> dict[str, object]:
        if self._hooks is None:
            return dict(payload)
        outcome = await self._hooks.dispatch(
            HookRequest(
                point=point,
                run_id=run_id,
                session_id=session_id,
                cycle_id=cycle_id,
                step_id=step_id,
                payload=payload,
            )
        )
        for emitted in outcome.emitted_events:
            event_type = emitted.get("event_type")
            if isinstance(event_type, str) and event_type:
                event_payload = {
                    key: value for key, value in emitted.items() if key != "event_type"
                }
                await self._append(run_id, event_type, event_payload)
        if outcome.decision is HookDecision.BLOCK:
            raise DomainError(f"Runtime Hook blocked {point.value}")
        result = dict(outcome.payload)
        if outcome.additional_context and "input_items" in result:
            items = _object_list(result.get("input_items"))
            items.extend(
                {
                    "type": "hook_context",
                    "content": content,
                    "source_refs": [f"hook://{point.value}"],
                }
                for content in outcome.additional_context
            )
            result["input_items"] = items
        if outcome.decision is HookDecision.REQUIRE_APPROVAL:
            if not allow_require_approval:
                raise DomainError(f"Runtime Hook requires unsupported approval at {point.value}")
            result["_hook_require_approval"] = True
        return result

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
            RunStatus.COMPACTING,
        }:
            run = await self._runs.update_status(run.id, RunStatus.RUNNING)
        if run.status is not RunStatus.RUNNING:
            raise DomainError(f"Run {run.id!r} cannot start a Runtime cycle from {run.status}")
        return run

    async def _transition_run_for_yield(
        self,
        run_id: str,
        reason: YieldReason,
        *,
        defer_run_completion: bool = False,
    ) -> None:
        targets = {
            YieldReason.TOOL_RUNNING: RunStatus.WAITING_TOOL,
            YieldReason.TERMINAL_OPEN: RunStatus.WAITING_TOOL,
            YieldReason.APPROVAL_REQUIRED: RunStatus.WAITING_APPROVAL,
            YieldReason.USER_INPUT_REQUIRED: RunStatus.WAITING_USER,
            YieldReason.COMPACTION_REQUIRED: RunStatus.COMPACTING,
        }
        terminal_target = {
            YieldReason.RUN_COMPLETED: RunStatus.COMPLETED,
            YieldReason.FATAL_FAILURE: RunStatus.FAILED,
        }.get(reason)
        if terminal_target is not None:
            if defer_run_completion:
                # The Temporal cleanup Activity owns the atomic message fence
                # and final three-family physical-stop gate.
                return
            await self._finalize_compat_yield(run_id, terminal_target)
        elif target := targets.get(reason):
            await self._runs.update_status(run_id, target)

    async def _finalize_compat_yield(self, run_id: str, target: RunStatus) -> None:
        """Safely finalize pre-deferred or standalone Runtime cycles."""

        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        if run.status in {RunStatus.PAUSING, RunStatus.CANCELLING}:
            return
        if run.status is not target:
            if run.status is not RunStatus.COMPLETING and not run.can_transition_to(
                RunStatus.COMPLETING
            ):
                return
            # Persist the terminal target in the same transaction as the
            # COMPLETING admission fence.  A failed first stop attempt can
            # then be recovered by a fresh coordinator or the reconciler
            # without having to infer whether this Run meant COMPLETED or
            # FAILED from an in-memory cycle result.
            run = await self._runs.fence_finalization(run.id, target)
        if target is RunStatus.COMPLETED and self._closure_verifier is not None:
            report = await self._closure_verifier.verify(run.id)
            await self._events.append(
                run.id,
                CLOSURE_EVALUATED_EVENT_TYPE,
                closure_event_payload(report),
                event_id=closure_event_id(report),
            )

        # A coordinator assembled without every resource controller cannot
        # prove physical termination. COMPLETING is therefore the only safe
        # result; production Worker assembly always injects the strict gate.
        if self._safety_stopper is None:
            return
        stop_result = await self._safety_stopper.stop_run(run.id, drain=True)
        if not stop_result.succeeded:
            raise DomainError(
                "Runtime completion could not confirm every Run effect stopped: "
                f"{stop_resources_payload(stop_result)!r}"
            )

        run = await self._runs.get(run.id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        if run.status in {RunStatus.PAUSING, RunStatus.CANCELLING}:
            return
        if run.status is not target and run.can_transition_to(target):
            run = await self._runs.update_status(run.id, target)
        if run.status is not target:
            raise DomainError(
                f"Run {run.id!r} could not finalize as {target.value!r} from {run.status.value!r}"
            )

    async def _ensure_active_session(self, session: AgentSession) -> None:
        if session.status is SessionStatus.CREATED:
            self._state_machine.transition_session(session, SessionStatus.ACTIVE)
            await self._sessions.save(session)
            await self._append(session.run_id, SESSION_ACTIVATED, {"session_id": session.id})

    async def _subagent_delegation(
        self,
        session: AgentSession,
    ) -> dict[str, object] | None:
        if session.parent_session_id is None:
            return None
        if self._transcript is None:
            raise DomainError("Subagent Context requires its independent Transcript")
        messages = await self._transcript.list_by_session(session.id)
        delegation = next(
            (
                item.structured_content
                for item in messages
                if item.message_type is MessageType.SUBAGENT_DELEGATION
                and item.structured_content is not None
            ),
            None,
        )
        if delegation is None:
            raise DomainError(f"Subagent session {session.id!r} has no Delegation Packet")
        return dict(delegation)

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

    async def _persist_tool_result_inputs(
        self,
        session: AgentSession,
        input_items: list[dict[str, object]],
    ) -> set[str]:
        """Retain completed Tool Results across cycles, idempotently by Execution ID."""

        if self._transcript is None:
            return set()
        messages = await self._transcript.list_by_session(session.id)
        persisted_execution_ids = {
            message.execution_id
            for message in messages
            if message.message_type is MessageType.TOOL_RESULT_REFERENCE
            and message.execution_id is not None
        }
        retained_execution_ids: set[str] = set()
        drafts: list[TranscriptMessageDraft] = []
        for item in input_items:
            execution_id = _execution_id_from_tool_result_input(item)
            if execution_id is None:
                continue
            retained_execution_ids.add(execution_id)
            if execution_id in persisted_execution_ids:
                continue
            structured_content = {**item, "execution_id": execution_id}
            drafts.append(
                TranscriptMessageDraft(
                    agent_id=session.agent_type,
                    role=MessageRole.TOOL,
                    message_type=MessageType.TOOL_RESULT_REFERENCE,
                    structured_content=structured_content,
                    tool_call_id=_optional_string(item.get("tool_call_id")),
                    execution_id=execution_id,
                    visibility=MessageVisibility.AGENT_ONLY,
                )
            )
            persisted_execution_ids.add(execution_id)
        if drafts:
            await self._transcript.append_many(session.id, drafts)
        return retained_execution_ids

    async def _persist_step(
        self, cycle: AgentCycle, sequence: int, event: AgentEngineEvent
    ) -> AgentStep:
        step = AgentStep(
            cycle_id=cycle.id,
            sequence=sequence,
            step_type=_STEP_TYPES[event.event_type],
            input_refs=_reference_list(event.data.get("input_refs")),
            output_refs=_reference_list(event.data.get("output_refs")),
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
        defer_run_completion: bool = False,
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
        self._state_machine.transition_cycle(
            cycle,
            CycleStatus.YIELDED,
            yield_reason=reason,
        )
        cycle.waiting_object_id = waiting_object_id or waiting_execution_id
        cycle.checkpoint_id = provider_state_id or session.latest_checkpoint_id
        await self._cycles.save_yield(session, cycle)
        if session.parent_session_id is None:
            await self._transition_run_for_yield(
                run_id,
                reason,
                defer_run_completion=defer_run_completion,
            )
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
            await self._runtime_approvals.set_provider_state_id(
                cycle.waiting_object_id,
                provider_state_id,
            )
        if (
            reason is YieldReason.USER_INPUT_REQUIRED
            and cycle.waiting_object_id is not None
            and self._user_inputs is not None
        ):
            input_request = await self._user_inputs.get(cycle.waiting_object_id)
            if input_request is not None and input_request.provider_state_id != provider_state_id:
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

    async def _yield_for_next_pending_intent(
        self,
        run: Run,
        session: AgentSession,
        cycle: AgentCycle,
        *,
        engine_run: AgentEngineRun | None = None,
    ) -> RunCycleResult | None:
        """Expose or launch only the earliest unresolved Tool Call Intent."""

        if self._deferred_executions is None:
            return None
        for intent in await self._deferred_executions.pending_intents(session.id):
            if intent.run_id != run.id:
                raise DomainError(
                    f"Tool Call Intent {intent.id!r} does not belong to Run {run.id!r}"
                )
            if intent.status is ToolCallStatus.WAITING_APPROVAL:
                if self._runtime_approvals is None:
                    raise DomainError("Runtime approval repository is unavailable")
                approval = await self._runtime_approvals.get_for_intent(intent.id)
                if approval is None:
                    raise DomainError(
                        f"Tool Call Intent {intent.id!r} has no durable Approval request"
                    )
                if approval.decision is None:
                    return await self._yield_cycle(
                        run.id,
                        session,
                        cycle,
                        YieldReason.APPROVAL_REQUIRED,
                        engine_run=engine_run,
                        waiting_object_id=approval.id,
                    )
                await self._apply_approval_decision(
                    run,
                    session,
                    cycle,
                    approval.id,
                )
                if approval.decision in {
                    ApprovalDecision.REJECT,
                    ApprovalDecision.REJECT_WITH_FEEDBACK,
                }:
                    continue
                intent.status = ToolCallStatus.READY

            if intent.status not in {
                ToolCallStatus.READY,
                ToolCallStatus.EXECUTING,
            }:
                continue
            if intent.execution_spec is None:
                continue
            first_dispatch = intent.status is ToolCallStatus.READY
            try:
                reason, execution_id = await self._execute_prepared_intent(run, intent)
            except PentestBudgetExceededError as exc:
                await self._raise_pentest_budget_exhausted(run.id, exc)
            if first_dispatch:
                await self._dispatch_hook(
                    HookPoint.AFTER_TOOL_EXECUTION,
                    run_id=run.id,
                    session_id=session.id,
                    cycle_id=cycle.id,
                    step_id=intent.step_id,
                    payload={
                        "tool_call_intent_id": intent.id,
                        "tool_id": intent.tool_id,
                        "execution_id": execution_id,
                        "yield_reason": reason.value,
                    },
                )
            return await self._yield_cycle(
                run.id,
                session,
                cycle,
                reason,
                engine_run=engine_run,
                waiting_execution_id=execution_id,
                waiting_object_id=execution_id,
            )
        return None

    async def _resume_approval(
        self,
        run: Run,
        session: AgentSession,
        cycle: AgentCycle,
        request: RunCycleRequest,
    ) -> dict[str, object] | None:
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
        intent = await self._deferred_executions.get_intent(
            approval.tool_call_intent_id
        )
        if intent is None:
            raise EntityNotFoundError("ToolCallIntent", approval.tool_call_intent_id)
        provider_control = intent.execution_spec is None
        item = await self._apply_approval_decision(
            run,
            session,
            cycle,
            approval.id,
        )
        if (provider_control or self._transcript is None) and not any(
            existing.get("approval_id") == approval.id for existing in request.input_items
        ):
            request.input_items.append(item)
        return item if provider_control else None

    async def _apply_approval_decision(
        self,
        run: Run,
        session: AgentSession,
        cycle: AgentCycle,
        approval_id: str,
    ) -> dict[str, object]:
        if self._runtime_approvals is None or self._deferred_executions is None:
            raise DomainError("Runtime approval recovery dependencies are unavailable")
        approval = await self._runtime_approvals.get(approval_id)
        if approval is None:
            raise EntityNotFoundError("RuntimeApprovalRequest", approval_id)
        if approval.run_id != run.id or approval.session_id != session.id:
            raise DomainError("Runtime Approval does not belong to this Run and Session")
        intent = await self._deferred_executions.get_intent(
            approval.tool_call_intent_id
        )
        if intent is None:
            raise EntityNotFoundError("ToolCallIntent", approval.tool_call_intent_id)
        if approval.decision in {
            ApprovalDecision.APPROVE_ONCE,
            ApprovalDecision.APPROVE_TOOL_FOR_RUN,
        }:
            await self._deferred_executions.approve_intent(approval.tool_call_intent_id)
        elif approval.decision in {
            ApprovalDecision.REJECT,
            ApprovalDecision.REJECT_WITH_FEEDBACK,
        }:
            await self._deferred_executions.reject_intent(approval.tool_call_intent_id)
        else:
            raise DomainError(f"Runtime Approval {approval.id!r} has no durable decision")
        item: dict[str, object] = {
            "type": "approval_decision",
            "approval_id": approval.id,
            "decision": approval.decision.value,
            "feedback": approval.feedback,
            "tool_call_intent_id": approval.tool_call_intent_id,
            "engine_call_id": intent.engine_call_id,
            "tool_id": intent.tool_id,
            "source_refs": [f"approval://{approval.id}"],
        }
        await self._persist_approval_decision(session, item)
        await self._dispatch_hook(
            HookPoint.APPROVAL_RESOLVED,
            run_id=run.id,
            session_id=session.id,
            cycle_id=cycle.id,
            payload={
                "approval_id": approval.id,
                "decision": approval.decision.value,
                "tool_call_intent_id": approval.tool_call_intent_id,
            },
        )
        return item

    async def _persist_approval_decision(
        self,
        session: AgentSession,
        item: dict[str, object],
    ) -> None:
        if self._transcript is None:
            return
        approval_id = _optional_string(item.get("approval_id"))
        messages = await self._transcript.list_by_session(session.id)
        if any(
            message.message_type is MessageType.APPROVAL
            and isinstance(message.structured_content, dict)
            and message.structured_content.get("approval_id") == approval_id
            for message in messages
        ):
            return
        await self._transcript.append(
            session.id,
            TranscriptMessageDraft(
                agent_id=session.agent_type,
                role=MessageRole.SYSTEM,
                message_type=MessageType.APPROVAL,
                structured_content=item,
                visibility=MessageVisibility.AGENT_ONLY,
            ),
        )

    async def _execute_prepared_intent(
        self,
        run: Run,
        intent: ToolCallIntent,
    ) -> tuple[YieldReason, str]:
        if self._deferred_executions is None:
            raise DomainError("Deferred execution dispatcher is unavailable")
        deferred_executions = self._deferred_executions
        spec = DeferredExecutionSpec.model_validate(intent.execution_spec or {})
        if spec.executor_type is not ExecutorType.PTY:
            execution = await deferred_executions.execute_intent(intent)
            return YieldReason.TOOL_RUNNING, execution.id
        if self._terminal_service is None:
            raise DomainError("Terminal service is unavailable for an interactive Tool Call")
        digest_source = (
            intent.id
            if spec.attempt_group == "initial"
            else "\x1f".join((intent.id, spec.attempt_group))
        )
        digest = hashlib.sha256(digest_source.encode()).hexdigest()[:40]
        terminal_id = f"terminal:{digest}"
        execution_id = f"terminal-exec:{digest}"
        execution_key = f"terminal:{terminal_id}"
        terminal_command = CreateTerminal(
            session_id=terminal_id,
            execution_id=execution_id,
            execution_key=execution_key,
            agent_session_id=intent.session_id,
            tool_call_id=intent.id,
            attempt_group=spec.attempt_group,
            argv=spec.argv,
            tool_id=intent.tool_id,
            tool_version=spec.tool_version,
            cwd=str(spec.cwd),
            env=spec.env,
        )
        launch_request = await self._terminal_service.materialize_launch_request(
            run.id,
            terminal_command,
        )
        claim = await deferred_executions.claim_intent_execution(
            intent,
            execution_key=execution_key,
            attempt_group=spec.attempt_group,
        )

        async def admission_guard() -> None:
            await deferred_executions.require_current_intent_execution_claim(
                intent.id,
                execution_key=execution_key,
                attempt_group=spec.attempt_group,
            )

        try:
            view = await self._terminal_service.create(
                run.id,
                terminal_command,
                effect_guard=admission_guard,
                launch_request=launch_request,
            )
        except BaseException:
            await asyncio.shield(
                self._deferred_executions.settle_failed_intent_execution_start(
                    claim,
                    launch_request=launch_request,
                )
            )
            raise
        await self._deferred_executions.sync_intent_execution(claim.intent, view.execution)
        return YieldReason.TERMINAL_OPEN, view.execution.id

    async def _raise_pentest_budget_exhausted(
        self,
        run_id: str,
        error: PentestBudgetExceededError,
    ) -> NoReturn:
        details = pentest_budget_exhaustion_details(run_id, error)
        await self._append(run_id, "pentest.budget_exhausted", details)
        if self._budget_exhaustion_handler is not None:
            await self._budget_exhaustion_handler(run_id)
        raise ApplicationConflictError(
            "pentest_budget_exhausted",
            "Pentest execution budget is exhausted",
            details=details,
        ) from error

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


def _require_agent_cycle_effect_policy(run: Run, *, activity: bool) -> None:
    """Apply the exact Runtime or Activity rule after authoritative Run lookup."""

    # Lazy imports avoid the catalog's managed-entrypoint validation cycle.
    from riftx.application.run_kind_effects import (
        EffectMode,
        EffectOrigin,
        PolicyDenialReason,
        RunEffectOwnership,
        RunKindEffectPolicyDenied,
        require_run_kind_effect_policy,
    )

    operation = "activity.agent_cycle" if activity else "runtime.agent_cycle"
    origin = EffectOrigin.TEMPORAL_ACTIVITY if activity else EffectOrigin.APPLICATION_SERVICE
    try:
        require_run_kind_effect_policy(
            operation,
            origin,
            ownership=RunEffectOwnership(run_id=run.id, run_kind=run.kind),
            effect="host_execution",
            mode=EffectMode.NORMAL,
        )
    except RunKindEffectPolicyDenied as exc:
        code = (
            "run_kind_operation_unsupported"
            if exc.reason is PolicyDenialReason.RUN_KIND_UNSUPPORTED
            else "run_kind_effect_policy_denied"
        )
        raise ApplicationConflictError(
            code,
            "The requested Agent Runtime effect is not admitted for this Run owner",
        ) from None
    except (TypeError, ValueError):
        raise ApplicationConflictError(
            "run_kind_effect_policy_denied",
            "The requested Agent Runtime effect is not admitted for this Run owner",
        ) from None


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


def _is_explicit_control_event(event: AgentEngineEvent) -> bool:
    if event.data.get("approval_policy") != "explicit":
        return False
    policy = AGENT_TOOL_POLICIES.get(_event_tool_id(event))
    return bool(
        policy is not None
        and policy.approval_required
        and policy.authorization is AgentToolAuthorization.DYNAMIC_APPROVAL
    )


def _execution_id_from_tool_result_input(item: Mapping[str, object]) -> str | None:
    item_id = item.get("id")
    item_type = item.get("type")
    if item_type not in {"tool_result", "execution_completion"} and not (
        isinstance(item_id, str) and item_id.startswith("tool-result:")
    ):
        return None
    value = item.get("execution_id")
    if isinstance(value, str) and value:
        return value
    content = item.get("content")
    if isinstance(content, Mapping):
        value = content.get("execution_id")
        if isinstance(value, str) and value:
            return value
    if isinstance(item_id, str) and item_id.startswith("tool-result:"):
        value = item_id.removeprefix("tool-result:")
        if value:
            return value
    refs = item.get("source_refs")
    if not isinstance(refs, list):
        return None
    for ref in refs:
        if isinstance(ref, str) and ref.startswith("execution://"):
            execution_id = ref.removeprefix("execution://")
            if execution_id:
                return execution_id
    return None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _object_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _reference_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


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


def _compiled_capability_ids(
    payloads: Collection[Mapping[str, object]],
    *,
    keys: tuple[str, ...],
) -> tuple[str, ...]:
    values: set[str] = set()
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                values.add(value)
                break
    return tuple(sorted(values))


def _observer_waiting_object_id(report: SupervisorReport) -> str | None:
    if report.yield_reason is None:
        return None
    prefix = {
        YieldReason.APPROVAL_REQUIRED: "approval:",
        YieldReason.USER_INPUT_REQUIRED: "user-input:",
    }.get(report.yield_reason)
    if prefix is None:
        return None
    return next(
        (
            ref.removeprefix(prefix)
            for signal in report.signals
            if signal.yield_reason is report.yield_reason
            for ref in signal.refs
            if ref.startswith(prefix)
        ),
        None,
    )
