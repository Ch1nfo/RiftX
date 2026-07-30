"""Deterministic outer lifecycle for a durable RiftX run."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .models import (
    AgentCycleActivityInput,
    AgentCycleActivityResult,
    AgentCycleActivityStatus,
    CleanupRunInput,
    CleanupRunResult,
    CompactContextInput,
    CompactContextResult,
    GenerateReportInput,
    GenerateReportResult,
    PendingApproval,
    PrepareRunInput,
    PrepareRunResult,
    RunWorkflowInput,
    RunWorkflowResult,
    RunWorkflowStatus,
    WorkflowPhase,
)

_ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


@workflow.defn(name="RiftXRunWorkflow")
class RiftXRunWorkflow:
    def __init__(self) -> None:
        self._run_id = ""
        self._phase = WorkflowPhase.PREPARING
        self._paused = False
        self._finished = False
        self._checkpoint_id: str | None = None
        self._pending_approvals: list[PendingApproval] = []
        self._approval_decisions: dict[str, bool] = {}
        self._user_messages: list[str] = []
        self._active_execution_id: str | None = None
        self._cancel_current_execution_requested = False
        self._cancel_requested = False
        self._compact_history_items: int | None = None

    @workflow.run
    async def run(self, input: RunWorkflowInput) -> RunWorkflowResult:
        self._run_id = input.run_id
        await workflow.execute_activity(
            "prepare_run_activity",
            PrepareRunInput(run_id=input.run_id),
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
            if self._checkpoint_id is not None:
                await self._wait_for_approval_decisions()
                if self._cancel_requested:
                    break
                if self._compact_history_items is not None:
                    continue
                if self._paused:
                    continue
            elif self._phase is WorkflowPhase.WAITING_INPUT and not self._user_messages:
                await workflow.wait_condition(
                    lambda: (
                        bool(self._user_messages)
                        or self._paused
                        or self._cancel_requested
                        or self._compact_history_items is not None
                    )
                )
                if self._cancel_requested:
                    break
                if self._compact_history_items is not None:
                    continue
                if self._paused:
                    continue

            self._phase = WorkflowPhase.AGENT_CYCLE
            agent_step_id = str(workflow.uuid4())
            checkpoint_id = self._checkpoint_id
            decisions = dict(self._approval_decisions)
            messages = list(self._user_messages)
            cancel_current_execution = self._cancel_current_execution_requested
            self._approval_decisions.clear()
            self._user_messages.clear()
            self._cancel_current_execution_requested = False

            result = await workflow.execute_activity(
                "agent_cycle_activity",
                AgentCycleActivityInput(
                    run_id=self._run_id,
                    agent_step_id=agent_step_id,
                    checkpoint_id=checkpoint_id,
                    approval_decisions=decisions,
                    user_messages=messages,
                    cancel_current_execution=cancel_current_execution,
                ),
                result_type=AgentCycleActivityResult,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=1),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            self._active_execution_id = result.active_execution_id

            if result.status is AgentCycleActivityStatus.WAITING_APPROVAL:
                self._checkpoint_id = result.checkpoint_id
                self._pending_approvals = result.pending_approvals
                self._phase = WorkflowPhase.WAITING_APPROVAL
            elif result.status is AgentCycleActivityStatus.NEEDS_INPUT:
                self._checkpoint_id = None
                self._pending_approvals.clear()
                self._phase = WorkflowPhase.WAITING_INPUT
            elif result.status is AgentCycleActivityStatus.COMPLETED:
                self._checkpoint_id = None
                self._pending_approvals.clear()
                self._finished = True
            else:
                self._checkpoint_id = None
                self._pending_approvals.clear()

        if self._cancel_requested:
            self._phase = WorkflowPhase.CLEANUP
            await workflow.execute_activity(
                "cleanup_run_activity",
                CleanupRunInput(run_id=self._run_id, final_status="cancelled"),
                result_type=CleanupRunResult,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            self._phase = WorkflowPhase.CANCELLED
            return RunWorkflowResult(run_id=self._run_id, phase=self._phase)

        self._phase = WorkflowPhase.REPORTING
        report = await workflow.execute_activity(
            "generate_report_activity",
            GenerateReportInput(run_id=self._run_id),
            result_type=GenerateReportResult,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._phase = WorkflowPhase.CLEANUP
        await workflow.execute_activity(
            "cleanup_run_activity",
            CleanupRunInput(run_id=self._run_id, final_status="completed"),
            result_type=CleanupRunResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._phase = WorkflowPhase.COMPLETED
        return RunWorkflowResult(
            run_id=self._run_id,
            phase=self._phase,
            report_id=report.report_id,
        )

    async def _wait_until_runnable(self) -> None:
        if not self._paused:
            return
        self._phase = WorkflowPhase.PAUSED
        await workflow.wait_condition(
            lambda: (
                not self._paused
                or self._cancel_requested
                or self._compact_history_items is not None
            )
        )

    async def _compact_context(self) -> None:
        max_history_items = self._compact_history_items
        if max_history_items is None:
            return
        resume_phase = self._phase
        self._compact_history_items = None
        self._phase = WorkflowPhase.COMPACTING
        await workflow.execute_activity(
            "compact_context_activity",
            CompactContextInput(
                run_id=self._run_id,
                max_history_items=max_history_items,
            ),
            result_type=CompactContextResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._phase = resume_phase

    async def _wait_for_approval_decisions(self) -> None:
        self._phase = WorkflowPhase.WAITING_APPROVAL
        await workflow.wait_condition(
            lambda: (
                self._paused
                or self._cancel_requested
                or self._compact_history_items is not None
                or all(item.call_id in self._approval_decisions for item in self._pending_approvals)
            )
        )

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.signal
    def approve(self, call_id: str) -> None:
        if any(item.call_id == call_id for item in self._pending_approvals):
            self._approval_decisions[call_id] = True

    @workflow.signal
    def reject(self, call_id: str) -> None:
        if any(item.call_id == call_id for item in self._pending_approvals):
            self._approval_decisions[call_id] = False

    @workflow.signal
    def cancel_current_execution(self) -> None:
        self._cancel_current_execution_requested = True

    @workflow.signal
    def cancel(self) -> None:
        self._cancel_requested = True
        self._cancel_current_execution_requested = True
        self._paused = False

    @workflow.signal
    def compact(self, max_history_items: int = 100) -> None:
        self._compact_history_items = max(1, min(max_history_items, 10_000))

    @workflow.signal
    def append_user_message(self, message: str) -> None:
        normalized = message.strip()
        if normalized:
            self._user_messages.append(normalized)

    @workflow.query
    def get_status(self) -> RunWorkflowStatus:
        return RunWorkflowStatus(
            run_id=self._run_id,
            phase=self._phase,
            paused=self._paused,
            finished=self._finished,
            checkpoint_id=self._checkpoint_id,
            pending_approvals=list(self._pending_approvals),
            active_execution_id=self._active_execution_id,
            queued_user_messages=len(self._user_messages),
            cancel_current_execution_requested=self._cancel_current_execution_requested,
            cancel_requested=self._cancel_requested,
            compact_requested=self._compact_history_items is not None,
        )

    @workflow.query
    def get_current_phase(self) -> WorkflowPhase:
        return self._phase

    @workflow.query
    def get_pending_approval(self) -> list[PendingApproval]:
        return list(self._pending_approvals)

    @workflow.query
    def get_active_execution(self) -> str | None:
        return self._active_execution_id
