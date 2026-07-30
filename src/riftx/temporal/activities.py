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
from riftx.application.ports import (
    ExecutionRepository,
    RunEventRepository,
    RunRepository,
)
from riftx.application.services import (
    ApprovalInterruption,
    ApprovalRequestRecorder,
    GenerateReports,
    ReportApplicationService,
)
from riftx.domain import ReportFormat, Run, RunStatus
from riftx.runner import ExecutionRunner
from riftx.tools import ToolRegistry

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
    RunAgentCycleActivityInput,
    RunAgentCycleActivityResult,
    RuntimeYieldReason,
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
        execution_repository: ExecutionRepository,
        tool_registry: ToolRegistry,
        supervisor: ExecutionRunner,
        agent_cycle: AgentCycleRunner,
        approval_recorder: ApprovalRequestRecorder,
        report_service: ReportApplicationService,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._run_repository = run_repository
        self._event_repository = event_repository
        self._execution_repository = execution_repository
        self._tool_registry = tool_registry
        self._supervisor = supervisor
        self._agent_cycle = agent_cycle
        self._approval_recorder = approval_recorder
        self._report_service = report_service
        self._session_factory = session_factory

    @activity.defn(name="prepare_run_activity")
    async def prepare_run_activity(self, input: PrepareRunInput) -> PrepareRunResult:
        run = await self._require_run(input.run_id)
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
        if run.status is RunStatus.COMPLETED:
            return AgentCycleActivityResult(status=AgentCycleActivityStatus.COMPLETED)
        if run.status in {RunStatus.WAITING_APPROVAL, RunStatus.PAUSED}:
            run = await self._run_repository.update_status(run.id, RunStatus.RUNNING)
        elif run.status is not RunStatus.RUNNING:
            run = await self._move_to_running(run)

        if input.cancel_current_execution:
            await self._cancel_run_executions(run.id)

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
            await self._run_repository.update_status(run.id, RunStatus.COMPLETED)
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

    @activity.defn(name="cleanup_run_activity")
    async def cleanup_run_activity(self, input: CleanupRunInput) -> CleanupRunResult:
        run = await self._require_run(input.run_id)
        await self._cancel_run_executions(run.id)
        try:
            target = RunStatus(input.final_status)
        except ValueError as exc:
            raise ApplicationError(
                f"invalid cleanup final status {input.final_status!r}",
                non_retryable=True,
            ) from exc
        if run.status is not target and run.can_transition_to(target):
            run = await self._run_repository.update_status(run.id, target)
        await self._event_repository.append(
            run.id,
            "run.cleaned_up",
            {"status": run.status.value},
        )
        return CleanupRunResult()

    def registered(self, *, include_runtime_cycle_compat: bool = True) -> list[object]:
        activities = [
            self.prepare_run_activity,
            self.agent_cycle_activity,
            self.compact_context_activity,
            self.generate_report_activity,
            self.cleanup_run_activity,
        ]
        if include_runtime_cycle_compat:
            activities.append(self.run_agent_cycle_activity)
        return activities

    async def _require_run(self, run_id: str) -> Run:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise ApplicationError(f"run {run_id!r} was not found", non_retryable=True)
        return run

    async def _move_to_running(self, run: Run) -> Run:
        if run.status is RunStatus.CREATED:
            run = await self._run_repository.update_status(run.id, RunStatus.PREPARING)
        if run.status is RunStatus.PREPARING:
            run = await self._run_repository.update_status(run.id, RunStatus.RUNNING)
        if run.status is not RunStatus.RUNNING:
            raise ApplicationError(
                f"run {run.id!r} cannot be prepared from status {run.status.value!r}",
                non_retryable=True,
            )
        return run

    async def _cancel_run_executions(self, run_id: str) -> None:
        active = await self._execution_repository.list_active()
        cancelled: list[str] = []
        for execution in active:
            if execution.run_id != run_id:
                continue
            await self._supervisor.cancel(execution.id)
            cancelled.append(execution.id)
        if cancelled:
            await self._event_repository.append(
                run_id,
                "execution.cancel_requested",
                {"execution_ids": cancelled},
            )


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
