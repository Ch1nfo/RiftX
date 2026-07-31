"""Deterministic outer lifecycle for one durable RiftX Runtime session."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.exceptions import CancelledError as TemporalCancelledError

from .models import (
    CleanupReportFailureInput,
    CleanupRunInput,
    CleanupRunResult,
    CompactContextInput,
    CompactContextResult,
    GenerateReportInput,
    GenerateReportResult,
    PrepareConversationInput,
    PrepareConversationResult,
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

# A transiently unreachable Runner is not proof that its effects stopped.
# Keep finalization durable until the owner reconnects and acknowledges the
# stop; leaving the Run at COMPLETING is safer than exhausting three attempts
# and stranding an unmonitored process behind a failed Workflow.
_CLEANUP_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=0,
)

# A signal-with-start can be accepted immediately before the user pauses the
# locally persisted Run.  Pre-start pause/resume intentionally does not need a
# Temporal connection, so preparation activities wait (with server-side
# backoff) for the local status fence to reopen instead of failing and closing
# the one durable Workflow ID.  Explicit non-retryable activity errors still
# terminate immediately.
_INITIAL_PREPARATION_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=0,
)

_WAITING_PHASES = {
    RuntimeYieldReason.TOOL_RUNNING: WorkflowPhase.AGENT_CYCLE,
    RuntimeYieldReason.TERMINAL_OPEN: WorkflowPhase.AGENT_CYCLE,
    RuntimeYieldReason.SUBAGENT_RUNNING: WorkflowPhase.AGENT_CYCLE,
    RuntimeYieldReason.APPROVAL_REQUIRED: WorkflowPhase.WAITING_APPROVAL,
    RuntimeYieldReason.USER_INPUT_REQUIRED: WorkflowPhase.WAITING_INPUT,
}

_CONTROL_SIGNALS_PATCH = "riftx-workflow-control-v3"
_FAILED_CYCLE_CLEANUP_PATCH = "riftx-failed-cycle-cleanup-v1"
_QUEUED_USER_INPUT_PATCH = "riftx-user-input-queue-v1"
_DEFERRED_RUN_COMPLETION_PATCH = "riftx-deferred-run-completion-v1"
_ATOMIC_COMPLETION_FENCE_PATCH = "riftx-atomic-completion-fence-v1"
_POST_REPORT_CLEANUP_PATCH = "riftx-post-report-cleanup-v1"
_DURABLE_CLEANUP_RETRY_PATCH = "riftx-durable-cleanup-retry-v1"
_FAILED_CLEANUP_CONTROL_PATCH = "riftx-failed-cleanup-control-v1"
_MATCHED_SIGNAL_QUEUES_PATCH = "riftx-matched-signal-queues-v1"
_REPORT_FAILURE_CLEANUP_PATCH = "riftx-report-failure-cleanup-v1"


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
        self._use_user_input_queue = False
        self._defer_run_completion = False
        self._use_atomic_completion_fence = False
        self._post_report_cleanup = False
        self._hold_failed_cleanup_for_control = False
        self._use_matched_signal_queues = False
        self._control_sequence = 0
        self._latest_user_message_id: str | None = None
        self._accepted_user_message_ids: list[str] = []
        self._pending_user_message_ids: list[str] = []
        self._consumed_user_message_ids: list[str] = []
        self._completed_execution_id: str | None = None
        self._approval_id: str | None = None
        self._pending_completed_execution_ids: list[str] = []
        self._pending_approval_ids: list[str] = []
        self._compact_history_items: int | None = None
        self._pending_model_profile: str | None = None
        self._report_id: str | None = None
        self._active_cycle_activity: workflow.ActivityHandle[RunAgentCycleActivityResult] | None = (
            None
        )
        self._active_cycle_interruption: str | None = None

    @workflow.run
    async def run(self, input: RunWorkflowInput) -> RunWorkflowResult:
        self._run_id = input.run_id
        self._session_id = input.session_id or f"{input.run_id}:primary"
        self._use_user_input_queue = workflow.patched(_QUEUED_USER_INPUT_PATCH)
        self._defer_run_completion = workflow.patched(_DEFERRED_RUN_COMPLETION_PATCH)
        self._use_atomic_completion_fence = workflow.patched(_ATOMIC_COMPLETION_FENCE_PATCH)
        self._post_report_cleanup = workflow.patched(_POST_REPORT_CLEANUP_PATCH)
        self._hold_failed_cleanup_for_control = workflow.patched(_FAILED_CLEANUP_CONTROL_PATCH)
        self._use_matched_signal_queues = self._matched_signal_queues_enabled()
        if input.await_initial_instruction:
            conversation = await workflow.execute_activity(
                "prepare_conversation_activity",
                PrepareConversationInput(
                    run_id=self._run_id,
                    session_id=self._session_id,
                ),
                result_type=PrepareConversationResult,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_INITIAL_PREPARATION_RETRY_POLICY,
            )
            if conversation.cancelled:
                self._cancel_requested = True
            self._phase = WorkflowPhase.WAITING_INPUT
            self._yield_reason = RuntimeYieldReason.USER_INPUT_REQUIRED
            await workflow.wait_condition(
                lambda: self._has_pending_user_input() or self._cancel_requested
            )
            if self._cancel_requested:
                self._phase = WorkflowPhase.CANCELLED
                self._finished = True
                await self._cleanup("cancelled")
                return self._result()
            if self._paused:
                self._phase = WorkflowPhase.PAUSED
                await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
                if self._cancel_requested:
                    self._phase = WorkflowPhase.CANCELLED
                    self._finished = True
                    await self._cleanup("cancelled")
                    return self._result()
        preparation = await workflow.execute_activity(
            "prepare_run_activity",
            PrepareRunInput(run_id=self._run_id),
            result_type=PrepareRunResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=(
                _INITIAL_PREPARATION_RETRY_POLICY
                if input.await_initial_instruction
                else _ACTIVITY_RETRY_POLICY
            ),
        )
        if not preparation.prepared:
            self._cancel_requested = True
            self._phase = WorkflowPhase.CANCELLED
            self._finished = True
            await self._cleanup("cancelled")
            return self._result()

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
                _WAITING_PHASES.get(self._yield_reason) if self._yield_reason is not None else None
            )
            if waiting_phase is not None and not self._can_resume_from_yield():
                self._phase = waiting_phase
                await workflow.wait_condition(self._can_resume_from_yield)
                continue

            self._phase = WorkflowPhase.AGENT_CYCLE
            self._cycle_id = str(workflow.uuid4())
            latest_user_message_id = self._take_latest_user_message_id()
            result = await self._run_agent_cycle_activity(
                RunAgentCycleActivityInput(
                    run_id=self._run_id,
                    session_id=self._session_id,
                    cycle_id=self._cycle_id,
                    latest_user_message_id=latest_user_message_id,
                    completed_execution_id=self._take_completed_execution_id(),
                    approval_id=self._take_approval_id(),
                    defer_run_completion=self._defer_run_completion,
                )
            )
            if result is None:
                self._restore_user_message_id(latest_user_message_id)
                continue
            self._mark_user_message_consumed(latest_user_message_id)
            self._accept_result(result)

            if result.yield_reason is RuntimeYieldReason.RUN_COMPLETED:
                if self._use_atomic_completion_fence:
                    completion = await self._cleanup(
                        "completed",
                        completion_fence=True,
                        consumed_user_message_ids=self._consumed_user_message_ids,
                        defer_cleanup_event=self._post_report_cleanup,
                    )
                    if completion.pending_user_message_ids:
                        self._reconcile_pending_user_input(completion.pending_user_message_ids)
                    # Signal handlers run while the fence Activity is in flight.
                    # Check the in-memory queue synchronously after its result so
                    # an event delivered in that Workflow activation is never
                    # skipped merely because the pre-Activity queue was empty.
                    if not completion.cleaned or self._has_pending_user_input():
                        continue
                    self._phase = WorkflowPhase.COMPLETED
                    self._finished = True
                    break
                if self._has_pending_user_input():
                    # A second instruction may have arrived while the current
                    # model cycle was in flight. Preserve FIFO delivery and do
                    # not close the durable Workflow until every already
                    # accepted message event has had its own cycle.
                    continue
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
            # Runtime Cycles deliberately defer the terminal Run transition so
            # a message accepted while the previous Cycle was in flight can run
            # next. Once the durable FIFO is empty, cleanup owns the final state
            # fence; reports require that terminal state.
            if self._defer_run_completion and not self._use_atomic_completion_fence:
                await self._cleanup("completed")
            try:
                report = await workflow.execute_activity(
                    "generate_report_activity",
                    GenerateReportInput(run_id=self._run_id),
                    result_type=GenerateReportResult,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_ACTIVITY_RETRY_POLICY,
                )
            except ActivityError:
                # Evaluate this patch only on the failed-report branch. Thus
                # successful histories retain their exact command ordering,
                # while old failed histories without the marker still replay
                # to their original Workflow failure.
                if not self._report_failure_cleanup_enabled():
                    raise
                await self._cleanup_report_failure()
                # The Run remains COMPLETED because its primary work and stop
                # gate succeeded, but the durable Workflow must still expose
                # the failed report rather than silently claiming full success.
                raise
            else:
                self._report_id = report.report_id
                if self._post_report_cleanup and self._use_atomic_completion_fence:
                    # The atomic fence already made the Run terminal, which keeps
                    # late messages out while reports are composed. Emit the
                    # cleanup boundary only after those durable outputs exist.
                    await self._cleanup("completed")
                elif not self._defer_run_completion:
                    # Preserve the command order of pre-patch Workflow histories.
                    await self._cleanup("completed")
            self._phase = WorkflowPhase.COMPLETED
        elif self._phase is WorkflowPhase.FAILED:
            if self._hold_failed_cleanup_for_control:
                await self._settle_failed_cleanup()
            else:
                await self._cleanup("failed")
        return self._result()

    async def _run_agent_cycle_activity(
        self,
        input: RunAgentCycleActivityInput,
    ) -> RunAgentCycleActivityResult | None:
        handle = workflow.start_activity(
            "run_agent_cycle_activity",
            input,
            result_type=RunAgentCycleActivityResult,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=1),
            retry_policy=_ACTIVITY_RETRY_POLICY,
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        self._active_cycle_activity = handle
        try:
            return await handle
        except asyncio.CancelledError:
            if self._active_cycle_interruption is not None:
                return None
            raise
        except ActivityError as exc:
            if self._active_cycle_interruption is not None and isinstance(
                exc.cause,
                TemporalCancelledError,
            ):
                return None
            if await self._cleanup_failed_cycle():
                raise
            return None
        except Exception:
            if await self._cleanup_failed_cycle():
                raise
            return None
        finally:
            self._active_cycle_activity = None
            self._active_cycle_interruption = None

    async def _cleanup_failed_cycle(self) -> bool:
        if not workflow.patched(_FAILED_CYCLE_CLEANUP_PATCH):
            return True
        self._phase = WorkflowPhase.FAILED
        self._finished = True
        if not self._hold_failed_cleanup_for_control:
            await self._cleanup("failed")
            return True
        return await self._settle_failed_cleanup()

    async def _settle_failed_cleanup(self) -> bool:
        """Keep a failed Workflow alive while a safety control owns the Run."""

        self._phase = WorkflowPhase.FAILED
        self._finished = False
        while True:
            control_sequence = self._control_sequence
            result = await self._cleanup("failed")
            if result.cleaned:
                self._phase = WorkflowPhase.FAILED
                self._finished = True
                return True
            if self._cancel_requested:
                self._phase = WorkflowPhase.CANCELLED
                self._finished = True
                while not (await self._cleanup("cancelled")).cleaned:
                    await workflow.sleep(1)
                return False
            if self._paused:
                self._phase = WorkflowPhase.PAUSED
                await workflow.wait_condition(lambda: self._cancel_requested or not self._paused)
                continue
            if self._control_sequence != control_sequence:
                # Pause and resume may both arrive while the cleanup Activity is
                # in flight. The monotonic sequence preserves that wake-up even
                # though the final boolean state is unpaused again.
                continue
            await workflow.wait_condition(
                lambda expected_sequence=control_sequence: (
                    self._control_sequence != expected_sequence
                )
            )

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
            if not self._use_matched_signal_queues:
                return (
                    self._completed_execution_id is not None
                    and self._completed_execution_id == self._waiting_object_id
                )
            return self._has_matching_pending_id(self._pending_completed_execution_ids)
        if self._yield_reason is RuntimeYieldReason.APPROVAL_REQUIRED:
            if not self._use_matched_signal_queues:
                return self._approval_id is not None and (
                    self._waiting_object_id is None or self._approval_id == self._waiting_object_id
                )
            return self._has_matching_pending_id(self._pending_approval_ids)
        if self._yield_reason is RuntimeYieldReason.USER_INPUT_REQUIRED:
            return self._has_pending_user_input()
        return True

    def _has_pending_user_input(self) -> bool:
        if self._use_user_input_queue:
            return bool(self._pending_user_message_ids)
        return self._latest_user_message_id is not None

    def _take_latest_user_message_id(self) -> str | None:
        if self._use_user_input_queue:
            if not self._pending_user_message_ids:
                return None
            return self._pending_user_message_ids.pop(0)
        value = self._latest_user_message_id
        self._latest_user_message_id = None
        return value

    def _take_completed_execution_id(self) -> str | None:
        if not self._use_matched_signal_queues:
            value = self._completed_execution_id
            self._completed_execution_id = None
            return value
        if self._yield_reason not in {
            RuntimeYieldReason.TOOL_RUNNING,
            RuntimeYieldReason.TERMINAL_OPEN,
            RuntimeYieldReason.SUBAGENT_RUNNING,
        }:
            return None
        return self._take_matching_pending_id(self._pending_completed_execution_ids)

    def _take_approval_id(self) -> str | None:
        if not self._use_matched_signal_queues:
            value = self._approval_id
            self._approval_id = None
            return value
        if self._yield_reason is not RuntimeYieldReason.APPROVAL_REQUIRED:
            return None
        return self._take_matching_pending_id(self._pending_approval_ids)

    def _has_matching_pending_id(self, pending_ids: list[str]) -> bool:
        waiting_object_id = self._waiting_object_id
        return waiting_object_id is not None and waiting_object_id in pending_ids

    def _take_matching_pending_id(self, pending_ids: list[str]) -> str | None:
        waiting_object_id = self._waiting_object_id
        if waiting_object_id is None:
            return None
        try:
            index = pending_ids.index(waiting_object_id)
        except ValueError:
            return None
        return pending_ids.pop(index)

    @staticmethod
    def _enqueue_pending_id(pending_ids: list[str], value: str) -> None:
        if value and value not in pending_ids:
            pending_ids.append(value)

    def _matched_signal_queues_enabled(self) -> bool:
        return workflow.patched(_MATCHED_SIGNAL_QUEUES_PATCH)

    def _reconcile_pending_user_input(self, message_ids: list[str]) -> None:
        """Use durable event sequence as FIFO while preserving direct signals."""

        ordered: list[str] = []
        for message_id in [*message_ids, *self._pending_user_message_ids]:
            if not message_id or message_id in self._consumed_user_message_ids:
                continue
            if message_id not in ordered:
                ordered.append(message_id)
            if message_id not in self._accepted_user_message_ids:
                self._accepted_user_message_ids.append(message_id)
        self._pending_user_message_ids = ordered

    def _mark_user_message_consumed(self, message_id: str | None) -> None:
        if (
            self._use_user_input_queue
            and message_id
            and message_id not in self._consumed_user_message_ids
        ):
            self._consumed_user_message_ids.append(message_id)

    def _restore_user_message_id(self, message_id: str | None) -> None:
        if (
            self._use_user_input_queue
            and message_id
            and message_id not in self._pending_user_message_ids
            and message_id not in self._consumed_user_message_ids
        ):
            self._pending_user_message_ids.insert(0, message_id)

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

    async def _cleanup(
        self,
        final_status: str,
        *,
        completion_fence: bool = False,
        consumed_user_message_ids: list[str] | None = None,
        defer_cleanup_event: bool = False,
    ) -> CleanupRunResult:
        resume_phase = self._phase
        self._phase = WorkflowPhase.CLEANUP
        result = await workflow.execute_activity(
            "cleanup_run_activity",
            CleanupRunInput(
                run_id=self._run_id,
                final_status=final_status,
                completion_fence=completion_fence,
                consumed_user_message_ids=list(consumed_user_message_ids or ()),
                defer_cleanup_event=defer_cleanup_event,
            ),
            result_type=CleanupRunResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=(
                _CLEANUP_RETRY_POLICY
                if workflow.patched(_DURABLE_CLEANUP_RETRY_PATCH)
                else _ACTIVITY_RETRY_POLICY
            ),
        )
        self._phase = resume_phase
        return result

    async def _cleanup_report_failure(self) -> CleanupRunResult:
        resume_phase = self._phase
        self._phase = WorkflowPhase.CLEANUP
        result = await workflow.execute_activity(
            "cleanup_report_failure_activity",
            CleanupReportFailureInput(run_id=self._run_id),
            result_type=CleanupRunResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_CLEANUP_RETRY_POLICY,
        )
        self._phase = resume_phase
        return result

    @staticmethod
    def _report_failure_cleanup_enabled() -> bool:
        return workflow.patched(_REPORT_FAILURE_CLEANUP_PATCH)

    @workflow.signal
    def pause(self) -> None:
        self._control_sequence += 1
        self._paused = True
        if not workflow.patched(_CONTROL_SIGNALS_PATCH):
            return
        self._release_waiting_execution()
        self._interrupt_active_cycle("pause")

    @workflow.signal
    def resume(self) -> None:
        self._control_sequence += 1
        self._paused = False

    @workflow.signal
    def execution_completed(self, execution_id: str) -> None:
        if not self._use_matched_signal_queues:
            if execution_id:
                self._completed_execution_id = execution_id
            return
        self._enqueue_pending_id(self._pending_completed_execution_ids, execution_id)

    @workflow.signal
    def approve(self, approval_id: str) -> None:
        if not self._use_matched_signal_queues:
            if approval_id:
                self._approval_id = approval_id
            return
        self._enqueue_pending_id(self._pending_approval_ids, approval_id)

    @workflow.signal
    def reject(self, approval_id: str) -> None:
        if not self._use_matched_signal_queues:
            if approval_id:
                self._approval_id = approval_id
            return
        self._enqueue_pending_id(self._pending_approval_ids, approval_id)

    @workflow.signal
    def user_input(self, message_id: str) -> None:
        if not message_id:
            return
        # Keep the legacy single-value field populated for replay of Workflows
        # started before the queue patch. New Workflows durably remember every
        # accepted event ID, so an ambiguous Signal-With-Start retry cannot
        # enqueue or execute the same persisted instruction twice.
        self._latest_user_message_id = message_id
        if message_id in self._accepted_user_message_ids:
            return
        self._accepted_user_message_ids.append(message_id)
        self._pending_user_message_ids.append(message_id)

    @workflow.signal
    def append_user_message(self, message_id: str) -> None:
        """Compatibility signal name; payload is a persisted Message ID only."""

        self.user_input(message_id)

    @workflow.signal
    def cancel_current_execution(self) -> None:
        if not workflow.patched(_CONTROL_SIGNALS_PATCH):
            return
        self._release_waiting_execution()

    def _release_waiting_execution(self) -> None:
        if (
            self._yield_reason
            in {
                RuntimeYieldReason.TOOL_RUNNING,
                RuntimeYieldReason.TERMINAL_OPEN,
            }
            and self._waiting_object_id is not None
        ):
            # ExecutionService performs the actual process cancellation. Treat the
            # same durable Execution ID as completed so the Workflow can consume
            # its persisted terminal state instead of remaining blocked forever.
            if self._use_matched_signal_queues:
                self._enqueue_pending_id(
                    self._pending_completed_execution_ids,
                    self._waiting_object_id,
                )
            else:
                self._completed_execution_id = self._waiting_object_id

    @workflow.signal
    def cancel(self) -> None:
        self._control_sequence += 1
        self._cancel_requested = True
        self._paused = False
        if not workflow.patched(_CONTROL_SIGNALS_PATCH):
            return
        self._interrupt_active_cycle("cancel")

    def _interrupt_active_cycle(self, reason: str) -> None:
        handle = self._active_cycle_activity
        if handle is None or handle.done():
            return
        self._active_cycle_interruption = reason
        handle.cancel()

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
