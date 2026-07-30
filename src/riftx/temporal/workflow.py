"""Deterministic outer lifecycle for one durable RiftX Runtime session."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .models import (
    CleanupRunInput,
    CleanupRunResult,
    CompactContextInput,
    CompactContextResult,
    GenerateReportInput,
    GenerateReportResult,
    PrepareRunInput,
    PrepareRunResult,
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

_ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

_WAITING_PHASES = {
    RuntimeYieldReason.TOOL_RUNNING: WorkflowPhase.AGENT_CYCLE,
    RuntimeYieldReason.TERMINAL_OPEN: WorkflowPhase.AGENT_CYCLE,
    RuntimeYieldReason.SUBAGENT_RUNNING: WorkflowPhase.AGENT_CYCLE,
    RuntimeYieldReason.APPROVAL_REQUIRED: WorkflowPhase.WAITING_APPROVAL,
    RuntimeYieldReason.USER_INPUT_REQUIRED: WorkflowPhase.WAITING_INPUT,
}


@workflow.defn(name="RiftXRunWorkflow")
class RiftXRunWorkflow:
    """Keep only durable identifiers while Runtime state remains in the database."""

    def __init__(self) -> None:
        self._run_id = ""
        self._session_id = ""
        self._cycle_id: str | None = None
        self._yield_reason: RuntimeYieldReason | None = None
        self._waiting_object_id: str | None = None
        self._checkpoint_id: str | None = None
        self._phase = WorkflowPhase.PREPARING
        self._paused = False
        self._finished = False
        self._cancel_requested = False
        self._latest_user_message_id: str | None = None
        self._completed_execution_id: str | None = None
        self._approval_id: str | None = None
        self._compact_history_items: int | None = None
        self._pending_model_profile: str | None = None
        self._report_id: str | None = None

    @workflow.run
    async def run(self, input: RunWorkflowInput) -> RunWorkflowResult:
        self._run_id = input.run_id
        self._session_id = input.session_id or f"{input.run_id}:primary"
        await workflow.execute_activity(
            "prepare_run_activity",
            PrepareRunInput(run_id=self._run_id),
            result_type=PrepareRunResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )

        while not self._finished and not self._cancel_requested:
            await self._wait_until_runnable()
            if self._cancel_requested:
                break
            if self._compact_history_items is not None:
                await self._compact_context()
                continue
            if self._pending_model_profile is not None:
                await self._switch_model()
                continue
            waiting_phase = (
                _WAITING_PHASES.get(self._yield_reason)
                if self._yield_reason is not None
                else None
            )
            if waiting_phase is not None and not self._can_resume_from_yield():
                self._phase = waiting_phase
                await workflow.wait_condition(self._can_resume_from_yield)
                continue

            self._phase = WorkflowPhase.AGENT_CYCLE
            self._cycle_id = str(workflow.uuid4())
            result = await workflow.execute_activity(
                "run_agent_cycle_activity",
                RunAgentCycleActivityInput(
                    run_id=self._run_id,
                    session_id=self._session_id,
                    cycle_id=self._cycle_id,
                    latest_user_message_id=self._take_latest_user_message_id(),
                    completed_execution_id=self._take_completed_execution_id(),
                    approval_id=self._take_approval_id(),
                ),
                result_type=RunAgentCycleActivityResult,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=1),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            self._accept_result(result)

            if result.yield_reason is RuntimeYieldReason.RUN_COMPLETED:
                self._phase = WorkflowPhase.COMPLETED
                self._finished = True
                break
            if result.yield_reason is RuntimeYieldReason.RUN_CANCELLED:
                self._cancel_requested = True
                break
            if result.yield_reason is RuntimeYieldReason.FATAL_FAILURE:
                self._phase = WorkflowPhase.FAILED
                self._finished = True
                break
            if result.yield_reason is RuntimeYieldReason.RUN_PAUSED:
                self._paused = True

            waiting_phase = _WAITING_PHASES.get(result.yield_reason)
            if waiting_phase is not None:
                self._phase = waiting_phase
                await workflow.wait_condition(self._can_resume_from_yield)

        if self._cancel_requested:
            self._phase = WorkflowPhase.CANCELLED
            self._finished = True
            await self._cleanup("cancelled")
        elif self._phase is WorkflowPhase.COMPLETED:
            self._phase = WorkflowPhase.REPORTING
            report = await workflow.execute_activity(
                "generate_report_activity",
                GenerateReportInput(run_id=self._run_id),
                result_type=GenerateReportResult,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            self._report_id = report.report_id
            await self._cleanup("completed")
            self._phase = WorkflowPhase.COMPLETED
        elif self._phase is WorkflowPhase.FAILED:
            await self._cleanup("failed")
        return self._result()

    def _accept_result(self, result: RunAgentCycleActivityResult) -> None:
        if result.run_id != self._run_id or result.session_id != self._session_id:
            raise ValueError("Runtime Cycle result does not belong to this Workflow")
        if result.cycle_id != self._cycle_id:
            raise ValueError("Runtime Cycle result has an unexpected cycle ID")
        self._yield_reason = result.yield_reason
        self._waiting_object_id = result.waiting_object_id
        self._checkpoint_id = result.checkpoint_id

    async def _wait_until_runnable(self) -> None:
        if not self._paused:
            return
        self._phase = WorkflowPhase.PAUSED
        await workflow.wait_condition(
            lambda: (
                not self._paused
                or self._cancel_requested
                or self._compact_history_items is not None
                or self._pending_model_profile is not None
            )
        )

    def _can_resume_from_yield(self) -> bool:
        if (
            self._cancel_requested
            or self._compact_history_items is not None
            or self._pending_model_profile is not None
        ):
            return True
        if self._yield_reason in {
            RuntimeYieldReason.TOOL_RUNNING,
            RuntimeYieldReason.TERMINAL_OPEN,
            RuntimeYieldReason.SUBAGENT_RUNNING,
        }:
            return (
                self._completed_execution_id is not None
                and self._completed_execution_id == self._waiting_object_id
            )
        if self._yield_reason is RuntimeYieldReason.APPROVAL_REQUIRED:
            return self._approval_id is not None and (
                self._waiting_object_id is None or self._approval_id == self._waiting_object_id
            )
        if self._yield_reason is RuntimeYieldReason.USER_INPUT_REQUIRED:
            return self._latest_user_message_id is not None
        return True

    def _take_latest_user_message_id(self) -> str | None:
        value = self._latest_user_message_id
        self._latest_user_message_id = None
        return value

    def _take_completed_execution_id(self) -> str | None:
        value = self._completed_execution_id
        self._completed_execution_id = None
        return value

    def _take_approval_id(self) -> str | None:
        value = self._approval_id
        self._approval_id = None
        return value

    async def _compact_context(self) -> None:
        max_history_items = self._compact_history_items
        if max_history_items is None:
            return
        self._compact_history_items = None
        self._phase = WorkflowPhase.COMPACTING
        result = await workflow.execute_activity(
            "compact_context_activity",
            CompactContextInput(
                run_id=self._run_id,
                max_history_items=max_history_items,
                session_id=self._session_id,
                checkpoint_id=str(workflow.uuid4()),
            ),
            result_type=CompactContextResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._checkpoint_id = result.checkpoint_id or self._checkpoint_id

    async def _switch_model(self) -> None:
        model_profile = self._pending_model_profile
        if model_profile is None:
            return
        self._pending_model_profile = None
        self._phase = WorkflowPhase.COMPACTING
        result = await workflow.execute_activity(
            "switch_model_activity",
            SwitchModelInput(
                run_id=self._run_id,
                session_id=self._session_id,
                checkpoint_id=str(workflow.uuid4()),
                model_profile=model_profile,
            ),
            result_type=SwitchModelResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._checkpoint_id = result.checkpoint_id

    async def _cleanup(self, final_status: str) -> None:
        resume_phase = self._phase
        self._phase = WorkflowPhase.CLEANUP
        await workflow.execute_activity(
            "cleanup_run_activity",
            CleanupRunInput(run_id=self._run_id, final_status=final_status),
            result_type=CleanupRunResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._phase = resume_phase

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.signal
    def execution_completed(self, execution_id: str) -> None:
        if execution_id:
            self._completed_execution_id = execution_id

    @workflow.signal
    def approve(self, approval_id: str) -> None:
        if approval_id:
            self._approval_id = approval_id

    @workflow.signal
    def reject(self, approval_id: str) -> None:
        if approval_id:
            self._approval_id = approval_id

    @workflow.signal
    def user_input(self, message_id: str) -> None:
        if message_id:
            self._latest_user_message_id = message_id

    @workflow.signal
    def append_user_message(self, message_id: str) -> None:
        """Compatibility signal name; payload is a persisted Message ID only."""

        self.user_input(message_id)

    @workflow.signal
    def cancel_current_execution(self) -> None:
        # Execution cancellation is persisted by ExecutionService. The completion
        # signal resumes this Workflow with the same stable execution identity.
        return

    @workflow.signal
    def cancel(self) -> None:
        self._cancel_requested = True
        self._paused = False

    @workflow.signal
    def compact(self, max_history_items: int = 100) -> None:
        self._compact_history_items = max(1, min(max_history_items, 10_000))

    @workflow.signal
    def switch_model(self, model_profile: str) -> None:
        normalized = model_profile.strip()
        if normalized:
            self._pending_model_profile = normalized

    @workflow.query
    def get_status(self) -> RunWorkflowStatus:
        phase = WorkflowPhase.PAUSED if self._paused and not self._finished else self._phase
        return RunWorkflowStatus(
            run_id=self._run_id,
            session_id=self._session_id,
            phase=phase,
            paused=self._paused,
            finished=self._finished,
            cycle_id=self._cycle_id,
            yield_reason=self._yield_reason,
            waiting_object_id=self._waiting_object_id,
            checkpoint_id=self._checkpoint_id,
            active_execution_id=(
                self._waiting_object_id
                if self._yield_reason is RuntimeYieldReason.TOOL_RUNNING
                else None
            ),
            cancel_requested=self._cancel_requested,
        )

    @workflow.query
    def get_current_phase(self) -> WorkflowPhase:
        return self.get_status().phase

    @workflow.query
    def get_active_execution(self) -> str | None:
        return self.get_status().active_execution_id

    def _result(self) -> RunWorkflowResult:
        return RunWorkflowResult(
            run_id=self._run_id,
            session_id=self._session_id,
            phase=self._phase,
            cycle_id=self._cycle_id,
            yield_reason=self._yield_reason,
            waiting_object_id=self._waiting_object_id,
            checkpoint_id=self._checkpoint_id,
            report_id=self._report_id,
        )
