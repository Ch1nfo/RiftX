"""Temporal Activities bridging durable workflow state to RiftX services."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from riftx.agent import (
    AgentCycleResult,
    AgentCycleStatus,
    RiftXAgentContext,
    RiftXDatabaseSession,
)
from riftx.application.errors import RepositoryConflictError
from riftx.application.finalization import (
    REPORT_GENERATION_FAILED_EVENT_TYPE,
    cleanup_event_id,
    cleanup_event_payload,
    report_failure_event_id,
    report_failure_event_payload,
)
from riftx.application.ports import (
    RunEventRepository,
    RunRepository,
)
from riftx.application.services import (
    CLOSURE_EVALUATED_EVENT_TYPE,
    ApprovalInterruption,
    ApprovalRequestRecorder,
    ClosureVerifierApplicationService,
    GenerateReports,
    ReportApplicationService,
    RunSafetyStopService,
    closure_event_id,
    closure_event_payload,
    stop_resources_payload,
)
from riftx.context.compaction import (
    CompactContextCommand,
    ContextCompactionManager,
    SwitchModelCommand,
)
from riftx.domain import InvalidStateTransitionError, ReportFormat, Run, RunKind, RunStatus
from riftx.tools import ToolRegistry

from .models import (
    AgentCycleActivityInput,
    AgentCycleActivityResult,
    AgentCycleActivityStatus,
    CleanupReportFailureInput,
    CleanupRunInput,
    CleanupRunResult,
    CompactContextInput,
    CompactContextResult,
    GenerateReportInput,
    GenerateReportResult,
    PendingApproval,
    PrepareConversationInput,
    PrepareConversationResult,
    PrepareRunInput,
    PrepareRunResult,
    RunAgentCycleActivityInput,
    RunAgentCycleActivityResult,
    RuntimeYieldReason,
    SwitchModelInput,
    SwitchModelResult,
)


class AgentCycleRunner(Protocol):
    async def run(
        self,
        context: RiftXAgentContext,
        *,
        input_text: str | None = None,
        checkpoint_id: str | None = None,
        approval_decisions: dict[str, bool] | None = None,
    ) -> AgentCycleResult: ...


class RiftXActivities:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        event_repository: RunEventRepository,
        tool_registry: ToolRegistry,
        safety_stopper: RunSafetyStopService,
        agent_cycle: AgentCycleRunner,
        approval_recorder: ApprovalRequestRecorder,
        closure_verifier: ClosureVerifierApplicationService,
        report_service: ReportApplicationService,
        session_factory: async_sessionmaker[AsyncSession],
        compaction_manager: ContextCompactionManager | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._event_repository = event_repository
        self._tool_registry = tool_registry
        self._safety_stopper = safety_stopper
        self._agent_cycle = agent_cycle
        self._approval_recorder = approval_recorder
        self._closure_verifier = closure_verifier
        self._report_service = report_service
        self._session_factory = session_factory
        self._compaction_manager = compaction_manager

    @activity.defn(name="prepare_conversation_activity")
    async def prepare_conversation_activity(
        self,
        input: PrepareConversationInput,
    ) -> PrepareConversationResult:
        run = await self._require_run(input.run_id)
        if run.status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
            return PrepareConversationResult(run_id=run.id, cancelled=True)
        if run.status in {RunStatus.PAUSING, RunStatus.PAUSED}:
            raise ApplicationError(
                f"run {run.id!r} is paused before its initial instruction can start"
            )
        if run.status is RunStatus.CREATED:
            try:
                run = await self._run_repository.update_status(run.id, RunStatus.WAITING_USER)
            except InvalidStateTransitionError:
                run = await self._require_run(input.run_id)
                if run.status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
                    return PrepareConversationResult(run_id=run.id, cancelled=True)
                raise
        if run.status is not RunStatus.WAITING_USER:
            raise ApplicationError(
                f"run {run.id!r} cannot wait for initial instruction from status "
                f"{run.status.value!r}",
                non_retryable=True,
            )

        existing_events = await self._event_repository.list_after(run.id, limit=1_000)
        if not any(event.event_type == "conversation.context_ready" for event in existing_events):
            await self._event_repository.append(
                run.id,
                "conversation.context_ready",
                {
                    "session_id": input.session_id,
                    "status": run.status.value,
                    "objective": run.objective.description,
                    "success_criteria": [
                        item.model_dump(mode="json") for item in run.success_criteria
                    ],
                    "entry_points": [item.model_dump(mode="json") for item in run.entry_points],
                    "scope": run.scope.model_dump(mode="json"),
                    "approval_mode": run.approval_mode.value,
                    "model_profile": run.model_profile,
                    "agent_started": False,
                },
            )
        return PrepareConversationResult(run_id=run.id)

    @activity.defn(name="prepare_run_activity")
    async def prepare_run_activity(self, input: PrepareRunInput) -> PrepareRunResult:
        run = await self._require_run(input.run_id)
        if run.status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
            return PrepareRunResult(run_id=run.id, prepared=False)
        if run.node_id != self._tool_registry.node_id:
            raise ApplicationError(
                f"run node {run.node_id!r} does not match worker node "
                f"{self._tool_registry.node_id!r}",
                non_retryable=True,
            )
        await asyncio.to_thread(Path(run.workspace_path).mkdir, parents=True, exist_ok=True)
        await self._tool_registry.reload_if_changed()
        run = await self._move_to_running(run)
        await self._event_repository.append(
            run.id,
            "run.prepared",
            {
                "node_id": run.node_id,
                "tool_generation": self._tool_registry.snapshot.generation,
                "available_tool_ids": [item.id for item in self._tool_registry.available_tools()],
            },
        )
        return PrepareRunResult(run_id=run.id)

    @activity.defn(name="agent_cycle_activity")
    async def agent_cycle_activity(
        self,
        input: AgentCycleActivityInput,
    ) -> AgentCycleActivityResult:
        run = await self._require_run(input.run_id)
        if not input.defer_run_completion and run.status in {
            RunStatus.COMPLETING,
            RunStatus.COMPLETED,
        }:
            # A legacy Activity retry may resume after its first attempt
            # established the fence but failed to obtain every stop ACK. Do
            # not invoke the model again: resume only the idempotent safety
            # finalization step.
            if not await self._finalize_compat_run(run.id, RunStatus.COMPLETED):
                raise ApplicationError(
                    f"run {run.id!r} completion lost to a control fence",
                    type="cleanup_stop_unconfirmed",
                )
            return AgentCycleActivityResult(status=AgentCycleActivityStatus.COMPLETED)
        if run.status is RunStatus.COMPLETED:
            return AgentCycleActivityResult(status=AgentCycleActivityStatus.COMPLETED)
        if run.status is RunStatus.WAITING_APPROVAL:
            run = await self._run_repository.update_status(run.id, RunStatus.RUNNING)
        elif run.status is not RunStatus.RUNNING:
            run = await self._move_to_running(run)

        if input.cancel_current_execution:
            stop_result = await self._safety_stopper.stop_run(run.id, drain=False)
            if not stop_result.succeeded:
                raise ApplicationError(
                    "current Run effects could not be confirmed stopped: "
                    f"{stop_resources_payload(stop_result)!r}",
                    type="execution_cancel_unconfirmed",
                )

        context = RiftXAgentContext.from_run(
            run,
            self._tool_registry,
            agent_step_id=input.agent_step_id,
        )
        input_text = "\n\n".join(input.user_messages) or None
        decisions = input.approval_decisions or None
        result = await _await_with_heartbeats(
            self._agent_cycle.run(
                context,
                input_text=input_text,
                checkpoint_id=input.checkpoint_id,
                approval_decisions=decisions,
            ),
            heartbeat_detail=f"agent-cycle:{run.id}:{input.agent_step_id}",
        )

        if result.status is AgentCycleStatus.INTERRUPTED:
            await self._approval_recorder.record(
                run,
                agent_step_id=input.agent_step_id,
                checkpoint_id=result.checkpoint_id,
                interruptions=[
                    ApprovalInterruption(
                        call_id=item.call_id,
                        tool_name=item.tool_name,
                        arguments=item.arguments,
                    )
                    for item in result.interruptions
                ],
            )
            await self._run_repository.update_status(run.id, RunStatus.WAITING_APPROVAL)
            return AgentCycleActivityResult(
                status=AgentCycleActivityStatus.WAITING_APPROVAL,
                checkpoint_id=result.checkpoint_id,
                pending_approvals=[
                    PendingApproval(
                        call_id=item.call_id,
                        tool_name=item.tool_name,
                        arguments=item.arguments,
                    )
                    for item in result.interruptions
                ],
            )

        output = result.output
        if output is None:
            raise ApplicationError("completed Agent Cycle did not return output")
        if output.completed:
            if not input.defer_run_completion:
                # Pre-deferred Workflow histories still schedule this legacy
                # Activity before report/cleanup. Preserve that command order
                # while requiring the same three-family physical-stop gate as
                # modern cleanup before exposing a terminal Run status.
                if not await self._finalize_compat_run(run.id, RunStatus.COMPLETED):
                    raise ApplicationError(
                        f"run {run.id!r} completion lost to a control fence",
                        type="cleanup_stop_unconfirmed",
                    )
            status = AgentCycleActivityStatus.COMPLETED
        elif output.needs_input:
            await self._run_repository.update_status(run.id, RunStatus.PAUSED)
            status = AgentCycleActivityStatus.NEEDS_INPUT
        else:
            status = AgentCycleActivityStatus.CONTINUE
        return AgentCycleActivityResult(
            status=status,
            summary=output.run_summary or output.assistant_message,
        )

    @activity.defn(name="run_agent_cycle_activity")
    async def run_agent_cycle_activity(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult:
        """Compatibility bridge while the production Worker moves to RuntimeCoordinator."""

        legacy = await self.agent_cycle_activity(
            AgentCycleActivityInput(
                run_id=input.run_id,
                agent_step_id=input.cycle_id,
                checkpoint_id=None,
                approval_decisions=(
                    {input.approval_id: True} if input.approval_id is not None else {}
                ),
                user_messages=(
                    [input.latest_user_message_id]
                    if input.latest_user_message_id is not None
                    else []
                ),
                defer_run_completion=input.defer_run_completion,
            )
        )
        reason = {
            AgentCycleActivityStatus.CONTINUE: RuntimeYieldReason.CYCLE_LIMIT_REACHED,
            AgentCycleActivityStatus.WAITING_APPROVAL: RuntimeYieldReason.APPROVAL_REQUIRED,
            AgentCycleActivityStatus.NEEDS_INPUT: RuntimeYieldReason.USER_INPUT_REQUIRED,
            AgentCycleActivityStatus.COMPLETED: RuntimeYieldReason.RUN_COMPLETED,
        }[legacy.status]
        waiting_object_id = (
            legacy.pending_approvals[0].call_id
            if legacy.pending_approvals
            else legacy.active_execution_id
        )
        return RunAgentCycleActivityResult(
            run_id=input.run_id,
            session_id=input.session_id,
            cycle_id=input.cycle_id,
            yield_reason=reason,
            waiting_object_id=waiting_object_id,
            checkpoint_id=legacy.checkpoint_id,
        )

    @activity.defn(name="compact_context_activity")
    async def compact_context_activity(
        self,
        input: CompactContextInput,
    ) -> CompactContextResult:
        await self._require_run(input.run_id)
        if (
            self._compaction_manager is not None
            and input.session_id is not None
            and input.checkpoint_id is not None
        ):
            result = await self._compaction_manager.compact(
                CompactContextCommand(
                    run_id=input.run_id,
                    session_id=input.session_id,
                    checkpoint_id=input.checkpoint_id,
                    max_history_items=input.max_history_items,
                )
            )
            await self._event_repository.append(
                input.run_id,
                "agent.context_compacted",
                {
                    "checkpoint_id": result.checkpoint.id,
                    "compacted_messages": result.compacted_messages,
                    "retained_items": result.retained_messages,
                    "max_history_items": input.max_history_items,
                },
            )
            return CompactContextResult(
                compacted=result.compacted_messages > 0,
                retained_items=result.retained_messages,
                checkpoint_id=result.checkpoint.id,
            )
        session = RiftXDatabaseSession(input.run_id, self._session_factory)
        removed, retained = await session.trim_history(input.max_history_items)
        await self._event_repository.append(
            input.run_id,
            "agent.context_compacted",
            {
                "removed_items": removed,
                "retained_items": retained,
                "max_history_items": input.max_history_items,
            },
        )
        return CompactContextResult(compacted=removed > 0, retained_items=retained)

    @activity.defn(name="switch_model_activity")
    async def switch_model_activity(self, input: SwitchModelInput) -> SwitchModelResult:
        await self._require_run(input.run_id)
        if self._compaction_manager is None:
            raise ApplicationError(
                "model switching is unavailable on this Worker",
                non_retryable=True,
            )
        result = await self._compaction_manager.switch_model(
            SwitchModelCommand(
                run_id=input.run_id,
                session_id=input.session_id,
                checkpoint_id=input.checkpoint_id,
                model_profile=input.model_profile,
                max_history_items=input.max_history_items,
            )
        )
        await self._event_repository.append(
            input.run_id,
            "agent.model_switched",
            {
                "checkpoint_id": result.checkpoint.id,
                "previous_model_profile": result.previous_model_profile,
                "model_profile": result.model_profile,
                "context_compilation_id": result.compiled_context.compilation_id,
            },
        )
        return SwitchModelResult(
            checkpoint_id=result.checkpoint.id,
            previous_model_profile=result.previous_model_profile,
            model_profile=result.model_profile,
            context_compilation_id=result.compiled_context.compilation_id,
        )

    @activity.defn(name="generate_report_activity")
    async def generate_report_activity(
        self,
        input: GenerateReportInput,
    ) -> GenerateReportResult:
        await self._require_run(input.run_id)
        reports = await self._report_service.generate(
            input.run_id,
            GenerateReports(reuse_existing=True),
        )
        markdown = next(
            (item for item in reports if item.format is ReportFormat.MARKDOWN),
            reports[0] if reports else None,
        )
        return GenerateReportResult(report_id=markdown.id if markdown else None)

    @activity.defn(name="cleanup_report_failure_activity")
    async def cleanup_report_failure_activity(
        self,
        input: CleanupReportFailureInput,
    ) -> CleanupRunResult:
        """Persist an exhausted report attempt and release deferred cleanup."""

        run = await self._require_run(input.run_id)
        await self._event_repository.append(
            run.id,
            REPORT_GENERATION_FAILED_EVENT_TYPE,
            report_failure_event_payload(),
            event_id=report_failure_event_id(run.id),
        )
        return await self.cleanup_run_activity(
            CleanupRunInput(run_id=run.id, final_status=RunStatus.COMPLETED.value)
        )

    @activity.defn(name="cleanup_run_activity")
    async def cleanup_run_activity(self, input: CleanupRunInput) -> CleanupRunResult:
        run = await self._require_run(input.run_id)
        try:
            target = RunStatus(input.final_status)
        except ValueError as exc:
            raise ApplicationError(
                f"invalid cleanup final status {input.final_status!r}",
                non_retryable=True,
            ) from exc
        if target not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise ApplicationError(
                f"cleanup target {target.value!r} is not terminal",
                non_retryable=True,
            )
        if run.status in {RunStatus.PAUSING, RunStatus.PAUSED, RunStatus.CANCELLING}:
            if target is RunStatus.FAILED and run.status in {
                RunStatus.PAUSING,
                RunStatus.PAUSED,
            }:
                await self._record_paused_failure_intent(run.id)
            return CleanupRunResult(cleaned=False)

        # Cancellation remains owned by the Control Plane. It establishes the
        # CANCELLING fence and only commits CANCELLED after its safety gate has
        # obtained affirmative stop evidence from every effect controller.
        if target is RunStatus.CANCELLED:
            if run.status is RunStatus.CANCELLED:
                await self._event_repository.append(
                    run.id,
                    "run.cleaned_up",
                    cleanup_event_payload(RunStatus.CANCELLED),
                    event_id=cleanup_event_id(run.id, RunStatus.CANCELLED),
                )
                return CleanupRunResult(cleaned=True)
            if run.status is not RunStatus.CANCELLING and run.can_transition_to(
                RunStatus.CANCELLING
            ):
                await self._update_cleanup_status(run.id, RunStatus.CANCELLING)
            return CleanupRunResult(cleaned=False)

        # Completion must serialize against durable user messages before the
        # fence closes admission. A pending instruction keeps the Run open and
        # no physical effect is stopped on behalf of a completion that lost.
        if target is RunStatus.COMPLETED and input.completion_fence:
            try:
                (
                    run,
                    pending_user_message_ids,
                ) = await self._run_repository.fence_completion_if_no_pending_user_messages(
                    run.id,
                    consumed_user_message_ids=input.consumed_user_message_ids,
                    defer_cleanup_event=input.defer_cleanup_event,
                )
            except (InvalidStateTransitionError, RepositoryConflictError):
                current = await self._require_run(run.id)
                if current.status in {
                    RunStatus.PAUSING,
                    RunStatus.CANCELLING,
                    RunStatus.COMPLETING,
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }:
                    return CleanupRunResult(cleaned=False)
                raise
            if pending_user_message_ids:
                return CleanupRunResult(
                    cleaned=False,
                    pending_user_message_ids=list(pending_user_message_ids),
                )
        elif run.status is not target:
            updated = await self._fence_cleanup_finalization(
                run.id,
                target,
                defer_cleanup_event=input.defer_cleanup_event,
            )
            if updated is None:
                return CleanupRunResult(cleaned=False)
            run = updated

        # If another terminal state or a safety fence won, do not relabel it.
        run = await self._require_run(run.id)
        if run.status in {RunStatus.PAUSING, RunStatus.PAUSED, RunStatus.CANCELLING}:
            return CleanupRunResult(cleaned=False)
        if run.status not in {target, RunStatus.COMPLETING}:
            return CleanupRunResult(cleaned=False)
        if target is RunStatus.COMPLETED:
            await self._record_closure(run.id)

        # COMPLETING is the cross-process admission fence for all three effect
        # families. Only trusted physical-stop acknowledgements can release it
        # into COMPLETED/FAILED. Any controller error is retryable and leaves
        # the Run visibly non-terminal without a misleading run.cleaned_up.
        stop_result = await self._safety_stopper.stop_run(run.id, drain=True)
        if not stop_result.succeeded:
            raise ApplicationError(
                "cleanup could not confirm every Run effect stopped: "
                f"{stop_resources_payload(stop_result)!r}",
                type="cleanup_stop_unconfirmed",
            )

        run = await self._require_run(run.id)
        if run.status in {RunStatus.PAUSING, RunStatus.PAUSED, RunStatus.CANCELLING}:
            return CleanupRunResult(cleaned=False)
        updated = await self._commit_cleanup_finalization(
            run.id,
            target,
            defer_cleanup_event=input.defer_cleanup_event,
        )
        if updated is None:
            return CleanupRunResult(cleaned=False)
        run = updated
        if run.status is not target:
            return CleanupRunResult(cleaned=False)
        if not input.defer_cleanup_event:
            await self._event_repository.append(
                run.id,
                "run.cleanup_stop_confirmed",
                {
                    "status": run.status.value,
                    "stop_resources": stop_resources_payload(stop_result),
                    "owner": "workflow_activity",
                },
            )
        return CleanupRunResult(cleaned=run.status is target)

    def registered(self, *, include_runtime_cycle_compat: bool = True) -> list[object]:
        activities: list[object] = [
            self.prepare_conversation_activity,
            self.prepare_run_activity,
            self.agent_cycle_activity,
            self.compact_context_activity,
            self.switch_model_activity,
            self.generate_report_activity,
            self.cleanup_report_failure_activity,
            self.cleanup_run_activity,
        ]
        if include_runtime_cycle_compat:
            activities.append(self.run_agent_cycle_activity)
        return activities

    async def _require_run(self, run_id: str) -> Run:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise ApplicationError(f"run {run_id!r} was not found", non_retryable=True)
        if run.kind not in {RunKind.GENERAL, RunKind.PENTEST}:
            raise ApplicationError(
                "Interactive RiftX Activities cannot operate on a Code Audit Run",
                type="run_kind_operation_unsupported",
                non_retryable=True,
            )
        return run

    async def _finalize_compat_run(self, run_id: str, target: RunStatus) -> bool:
        """Safely terminalize legacy non-deferred Activity executions.

        Workflow histories created before deferred completion cannot change
        their already-recorded Activity command order during replay. Activity
        implementation changes are replay-safe, so legacy retries establish
        the shared COMPLETING fence and require physical-stop evidence here.
        """

        run = await self._require_run(run_id)
        if run.status in {RunStatus.PAUSING, RunStatus.PAUSED, RunStatus.CANCELLING}:
            return False
        if run.status is not target:
            updated = await self._fence_cleanup_finalization(
                run.id,
                target,
                defer_cleanup_event=False,
            )
            if updated is None:
                return False
            run = updated
        if run.status not in {target, RunStatus.COMPLETING}:
            return False
        if target is RunStatus.COMPLETED:
            await self._record_closure(run.id)

        stop_result = await self._safety_stopper.stop_run(run.id, drain=True)
        if not stop_result.succeeded:
            raise ApplicationError(
                "legacy completion could not confirm every Run effect stopped: "
                f"{stop_resources_payload(stop_result)!r}",
                type="cleanup_stop_unconfirmed",
            )

        run = await self._require_run(run.id)
        if run.status in {RunStatus.PAUSING, RunStatus.PAUSED, RunStatus.CANCELLING}:
            return False
        if run.status is not target and run.can_transition_to(target):
            updated = await self._update_cleanup_status(run.id, target)
            if updated is None:
                return False
            run = updated
        return run.status is target

    async def _record_closure(self, run_id: str) -> None:
        report = await self._closure_verifier.verify(run_id)
        await self._event_repository.append(
            run_id,
            CLOSURE_EVALUATED_EVENT_TYPE,
            closure_event_payload(report),
            event_id=closure_event_id(report),
        )

    async def _fence_cleanup_finalization(
        self,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool,
    ) -> Run | None:
        try:
            return await self._run_repository.fence_finalization(
                run_id,
                target,
                defer_cleanup_event=defer_cleanup_event,
            )
        except (InvalidStateTransitionError, RepositoryConflictError):
            current = await self._require_run(run_id)
            if target is RunStatus.FAILED and current.status in {
                RunStatus.PAUSING,
                RunStatus.PAUSED,
            }:
                await self._record_paused_failure_intent(run_id)
            if current.status in {
                RunStatus.PAUSING,
                RunStatus.PAUSED,
                RunStatus.CANCELLING,
                RunStatus.COMPLETING,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                return None
            raise

    async def _record_paused_failure_intent(self, run_id: str) -> None:
        try:
            await self._run_repository.record_finalization_intent(
                run_id,
                RunStatus.FAILED,
            )
        except RepositoryConflictError:
            current = await self._require_run(run_id)
            if current.status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
                return
            raise

    async def _update_cleanup_status(self, run_id: str, target: RunStatus) -> Run | None:
        try:
            return await self._run_repository.update_status(run_id, target)
        except InvalidStateTransitionError:
            # The Control Plane can commit a pause/cancel fence after the
            # Activity's last read. Domain transitions reject overwriting that
            # fence; convert the expected lost race into a clean retry result
            # instead of making Temporal repeatedly fail the cleanup Activity.
            current = await self._require_run(run_id)
            if current.status in {RunStatus.PAUSING, RunStatus.CANCELLING}:
                return None
            raise

    async def _commit_cleanup_finalization(
        self,
        run_id: str,
        target: RunStatus,
        *,
        defer_cleanup_event: bool,
    ) -> Run | None:
        try:
            return await self._run_repository.commit_finalization(
                run_id,
                target,
                defer_cleanup_event=defer_cleanup_event,
            )
        except (InvalidStateTransitionError, RepositoryConflictError):
            current = await self._require_run(run_id)
            if current.status in {
                RunStatus.PAUSING,
                RunStatus.PAUSED,
                RunStatus.CANCELLING,
                RunStatus.CANCELLED,
            } or (
                current.status in {RunStatus.COMPLETED, RunStatus.FAILED}
                and current.status is not target
            ):
                return None
            # Do not hide an invalid intent or deterministic event collision.
            # Those failures must remain visible so Temporal retries them.
            raise

    async def _move_to_running(self, run: Run) -> Run:
        if run.status in {RunStatus.PAUSING, RunStatus.PAUSED}:
            # The conversation-first Workflow uses an unbounded, backed-off
            # retry policy for preparation.  Local resume returns the Run to
            # WAITING_USER, after which a retry can safely continue without a
            # Temporal resume signal.
            raise ApplicationError(f"run {run.id!r} is paused before preparation")
        if run.status in {RunStatus.CREATED, RunStatus.WAITING_USER}:
            run = await self._run_repository.update_status(run.id, RunStatus.PREPARING)
        if run.status is RunStatus.PREPARING:
            run = await self._run_repository.update_status(run.id, RunStatus.RUNNING)
        if run.status is not RunStatus.RUNNING:
            raise ApplicationError(
                f"run {run.id!r} cannot be prepared from status {run.status.value!r}",
                non_retryable=True,
            )
        return run


async def _await_with_heartbeats[ResultT](
    awaitable: Awaitable[ResultT],
    *,
    heartbeat_detail: str,
    interval_seconds: float = 20.0,
) -> ResultT:
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval_seconds)
            if done:
                return await task
            _heartbeat(heartbeat_detail)
    finally:
        if not task.done():
            task.cancel()


def _heartbeat(detail: str) -> None:
    try:
        activity.heartbeat(detail)
    except RuntimeError:
        # Direct Activity unit tests do not install a Temporal activity context.
        pass
