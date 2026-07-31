"""Application service for durable Run creation and control."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    ApplicationServiceError,
    EntityNotFoundError,
    ServiceUnavailableError,
)
from riftx.application.ports import (
    EngagementRepository,
    ExecutionRepository,
    RunEventRepository,
    RunRepository,
)
from riftx.domain import (
    ApprovalMode,
    Engagement,
    EntryPoint,
    Execution,
    ExecutionStatus,
    Objective,
    Run,
    RunStatus,
    Scope,
    SuccessCriterion,
)
from riftx.runner import ExecutionRunner

_ACTIVE_EXECUTION_STATUSES = {
    ExecutionStatus.CREATED,
    ExecutionStatus.QUEUED,
    ExecutionStatus.STARTING,
    ExecutionStatus.RUNNING,
}
_STOP_CANDIDATE_EXECUTION_STATUSES = _ACTIVE_EXECUTION_STATUSES | {
    # LOST means the Control Plane no longer has proof that the process
    # stopped. A durable cancel tombstone must still be delivered and
    # acknowledged before pause/cancel can claim success.
    ExecutionStatus.LOST,
}
_EXECUTION_LIST_PAGE_SIZE = 1000


class RunWorkflowClient(Protocol):
    """Small Temporal boundary consumed by the control plane."""

    async def start_run(self, run_id: str) -> object: ...

    async def pause(self, run_id: str) -> None: ...

    async def resume(self, run_id: str) -> None: ...

    async def approve(self, run_id: str, call_id: str) -> None: ...

    async def reject(self, run_id: str, call_id: str) -> None: ...

    async def cancel_current_execution(self, run_id: str) -> None: ...

    async def cancel(self, run_id: str) -> None: ...

    async def compact(self, run_id: str, max_history_items: int = 100) -> None: ...

    async def switch_model(self, run_id: str, model_profile: str) -> None: ...

    async def append_user_message(self, run_id: str, user_input_id: str) -> None: ...

    def workflow_id(self, run_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class CreateEngagement:
    name: str
    description: str = ""
    authorization_reference: str | None = None


@dataclass(frozen=True, slots=True)
class CreateRun:
    objective: str
    node_id: str
    approval_mode: ApprovalMode = ApprovalMode.BALANCED
    model_profile: str | None = None
    success_criteria: list[SuccessCriterion] = field(default_factory=list)
    entry_points: list[EntryPoint] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    workspace_path: str | None = None
    engagement_id: str | None = None
    engagement: CreateEngagement | None = None


@dataclass(frozen=True, slots=True)
class _ExecutionStopResult:
    attempted_ids: tuple[str, ...]
    confirmed_statuses: dict[str, str]
    failures: dict[str, str]

    @property
    def confirmed_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.confirmed_statuses))

    @property
    def succeeded(self) -> bool:
        return not self.failures


class RunApplicationService:
    def __init__(
        self,
        *,
        engagement_repository: EngagementRepository,
        run_repository: RunRepository,
        event_repository: RunEventRepository,
        workflow_client: RunWorkflowClient,
        execution_repository: ExecutionRepository,
        execution_runner: ExecutionRunner,
        workspace_root: Path,
        execution_cancel_timeout_seconds: float = 5.0,
        execution_cancel_poll_seconds: float = 0.05,
        execution_cancel_max_passes: int = 5,
    ) -> None:
        if execution_cancel_timeout_seconds < 0:
            raise ValueError("execution_cancel_timeout_seconds must not be negative")
        if execution_cancel_poll_seconds <= 0:
            raise ValueError("execution_cancel_poll_seconds must be positive")
        if execution_cancel_max_passes < 1:
            raise ValueError("execution_cancel_max_passes must be positive")
        self._engagement_repository = engagement_repository
        self._run_repository = run_repository
        self._event_repository = event_repository
        self._workflow_client = workflow_client
        self._execution_repository = execution_repository
        self._execution_runner = execution_runner
        self._workspace_root = workspace_root
        self._execution_cancel_timeout_seconds = execution_cancel_timeout_seconds
        self._execution_cancel_poll_seconds = execution_cancel_poll_seconds
        self._execution_cancel_max_passes = execution_cancel_max_passes

    async def create_run(self, command: CreateRun) -> Run:
        engagement = await self._resolve_engagement(command)
        run = Run(
            engagement_id=engagement.id,
            node_id=command.node_id,
            objective=Objective(description=command.objective),
            success_criteria=command.success_criteria,
            entry_points=command.entry_points,
            scope=command.scope,
            approval_mode=command.approval_mode,
            model_profile=command.model_profile,
            workspace_path=command.workspace_path or "",
        )
        if not run.workspace_path:
            run.workspace_path = str(self._workspace_root / run.id)
        workspace = await asyncio.to_thread(lambda: Path(run.workspace_path).expanduser().resolve())
        try:
            await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            raise ApplicationConflictError(
                "workspace_unavailable",
                f"Unable to create Run workspace {str(workspace)!r}: {exc}",
                details={"workspace_path": str(workspace)},
            ) from exc
        run.workspace_path = str(workspace)
        run.temporal_workflow_id = self._workflow_client.workflow_id(run.id)
        await self._run_repository.create(run)

        try:
            await self._workflow_client.start_run(run.id)
        except Exception as exc:
            await self._event_repository.append(
                run.id,
                "workflow.start_failed",
                {"workflow_id": run.temporal_workflow_id, "reason": str(exc)},
            )
            raise ServiceUnavailableError(
                "temporal_unavailable",
                "Temporal is unavailable; the run was saved but its workflow was not started",
                details={"run_id": run.id},
            ) from exc

        await self._event_repository.append(
            run.id,
            "workflow.started",
            {"workflow_id": run.temporal_workflow_id},
        )
        return run

    async def get_run(self, run_id: str) -> Run:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run

    async def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Run]:
        return list(await self._run_repository.list(status=status, limit=limit, offset=offset))

    async def pause(self, run_id: str) -> Run:
        run = await self._require_controllable_run(run_id, action="pause")
        run = await self._transition_if_possible(run, RunStatus.PAUSING)
        stop_result, workflow_synced = await asyncio.gather(
            self._cancel_active_executions(run.id, drain=True),
            self._invoke_workflow_best_effort(run, "pause", self._workflow_client.pause),
        )
        await self._event_repository.append(
            run.id,
            "run.pause_requested",
            self._stop_event_payload(stop_result, workflow_synced=workflow_synced),
        )
        self._raise_if_stop_failed(run, stop_result)
        current = await self.get_run(run.id)
        return await self._transition_if_possible(current, RunStatus.PAUSED)

    async def resume(self, run_id: str) -> Run:
        run = await self._require_controllable_run(run_id, action="resume")
        if run.status is RunStatus.PAUSING:
            run = await self._transition_if_possible(run, RunStatus.PAUSED)
        if run.status is not RunStatus.PAUSED:
            raise ApplicationConflictError(
                "run_not_paused",
                f"Cannot resume run {run.id!r} while it is {run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        run = await self._run_repository.update_status(run.id, RunStatus.RUNNING)
        try:
            await self._invoke_workflow(run, "resume", self._workflow_client.resume)
        except Exception:
            current = await self.get_run(run.id)
            if current.status is RunStatus.RUNNING:
                await self._run_repository.update_status(current.id, RunStatus.PAUSED)
            raise
        await self._event_repository.append(run.id, "run.resume_requested")
        return await self.get_run(run.id)

    async def cancel_current_execution(self, run_id: str) -> Run:
        # Safety controls must remain available for a terminal Run because a
        # crashed Workflow can leave an orphaned host process behind.
        run = await self.get_run(run_id)
        stop_result, workflow_synced = await asyncio.gather(
            self._cancel_active_executions(run.id, drain=False),
            self._invoke_workflow_best_effort(
                run,
                "cancel the current execution",
                self._workflow_client.cancel_current_execution,
            ),
        )
        await self._event_repository.append(
            run.id,
            "execution.cancel_requested",
            self._stop_event_payload(stop_result, workflow_synced=workflow_synced),
        )
        self._raise_if_stop_failed(run, stop_result)
        return await self.get_run(run.id)

    async def cancel(self, run_id: str) -> Run:
        # Full cancellation is also an orphan-process recovery operation, so it
        # intentionally remains callable after the Workflow/Run became terminal.
        run = await self.get_run(run_id)
        if run.status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            run = await self._transition_if_possible(run, RunStatus.CANCELLING)
        stop_result, workflow_synced = await asyncio.gather(
            self._cancel_active_executions(run.id, drain=True),
            self._invoke_workflow_best_effort(run, "cancel", self._workflow_client.cancel),
        )
        await self._event_repository.append(
            run.id,
            "run.cancel_requested",
            self._stop_event_payload(stop_result, workflow_synced=workflow_synced),
        )
        self._raise_if_stop_failed(run, stop_result)
        current = await self.get_run(run.id)
        if current.status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            current = await self._transition_if_possible(current, RunStatus.CANCELLING)
            current = await self._transition_if_possible(current, RunStatus.CANCELLED)
        return current

    async def compact(self, run_id: str, *, max_history_items: int = 100) -> Run:
        run = await self._require_controllable_run(run_id, action="compact context for")
        await self._invoke_workflow(
            run,
            "compact context",
            lambda target: self._workflow_client.compact(target, max_history_items),
        )
        await self._event_repository.append(
            run.id,
            "agent.context_compaction_requested",
            {"max_history_items": max_history_items},
        )
        return run

    async def switch_model(self, run_id: str, model_profile: str) -> Run:
        run = await self._require_controllable_run(run_id, action="switch the model for")
        normalized = model_profile.strip()
        if not normalized:
            raise ApplicationConflictError(
                "empty_model_profile",
                "A model profile must not be empty",
            )
        await self._invoke_workflow(
            run,
            "switch model",
            lambda target: self._workflow_client.switch_model(target, normalized),
        )
        await self._event_repository.append(
            run.id,
            "agent.model_switch_requested",
            {"model_profile": normalized},
        )
        return run

    async def append_user_message(self, run_id: str, message: str) -> Run:
        run = await self._require_controllable_run(run_id, action="send a message to")
        normalized = message.strip()
        if not normalized:
            raise ApplicationConflictError(
                "empty_message",
                "A run message must not be empty",
            )
        event = await self._event_repository.append(
            run.id,
            "user.message_queued",
            {"message": normalized},
        )
        await self._invoke_workflow(
            run,
            "send a message",
            lambda target: self._workflow_client.append_user_message(target, event.id),
        )
        return run

    async def _resolve_engagement(self, command: CreateRun) -> Engagement:
        if command.engagement_id and command.engagement:
            raise ApplicationConflictError(
                "engagement_conflict",
                "Provide either engagement_id or engagement, not both",
            )
        if command.engagement_id:
            engagement = await self._engagement_repository.get(command.engagement_id)
            if engagement is None:
                raise EntityNotFoundError("Engagement", command.engagement_id)
            return engagement

        requested = command.engagement or CreateEngagement(name=f"Run: {command.objective[:80]}")
        engagement = Engagement(
            name=requested.name,
            description=requested.description,
            authorization_reference=requested.authorization_reference,
        )
        return await self._engagement_repository.create(engagement)

    async def _require_controllable_run(self, run_id: str, *, action: str) -> Run:
        run = await self.get_run(run_id)
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            raise ApplicationConflictError(
                "run_not_controllable",
                f"Cannot {action} run {run.id!r} while it is {run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        return run

    async def _invoke_workflow(
        self,
        run: Run,
        action: str,
        operation: Callable[[str], Awaitable[object]],
    ) -> None:
        try:
            await operation(run.id)
        except ApplicationServiceError:
            raise
        except Exception as exc:
            raise ApplicationConflictError(
                "workflow_control_failed",
                f"Could not {action} because the Workflow rejected the control request",
                details={
                    "run_id": run.id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            ) from exc

    async def _invoke_workflow_best_effort(
        self,
        run: Run,
        action: str,
        operation: Callable[[str], Awaitable[object]],
    ) -> bool:
        try:
            await self._invoke_workflow(run, action, operation)
        except Exception as exc:
            payload: dict[str, object] = {
                "action": action,
                "workflow_id": run.temporal_workflow_id,
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }
            if isinstance(exc, ApplicationServiceError):
                payload["error_code"] = exc.code
                payload["details"] = exc.details
            await self._event_repository.append(run.id, "workflow.signal_failed", payload)
            return False
        return True

    async def _cancel_active_executions(
        self,
        run_id: str,
        *,
        drain: bool,
    ) -> _ExecutionStopResult:
        attempted: set[str] = set()
        confirmed: dict[str, str] = {}
        failures: dict[str, str] = {}
        passes = self._execution_cancel_max_passes if drain else 1

        for _ in range(passes):
            candidates = [
                execution
                for execution in await self._list_run_executions(run_id)
                if execution.status in _STOP_CANDIDATE_EXECUTION_STATUSES
                and execution.id not in attempted
            ]
            if not candidates:
                break
            attempted.update(execution.id for execution in candidates)
            results = await asyncio.gather(
                *(self._cancel_and_confirm(execution) for execution in candidates),
                return_exceptions=True,
            )
            for execution, result in zip(candidates, results, strict=True):
                if isinstance(result, BaseException):
                    failures[execution.id] = f"{type(result).__name__}: {result}"
                    continue
                confirmed[execution.id] = result.status.value
                failures.pop(execution.id, None)
            if not drain:
                break
            await asyncio.sleep(0)

        refreshed = await self._list_run_executions(run_id)
        remaining = []
        for execution in refreshed:
            if execution.status in _STOP_CANDIDATE_EXECUTION_STATUSES:
                remaining.append(execution)
            elif execution.id in attempted:
                confirmed[execution.id] = execution.status.value
                failures.pop(execution.id, None)
        for execution in remaining:
            attempted.add(execution.id)
            failures.setdefault(
                execution.id,
                f"stop was not confirmed; execution remains {execution.status.value}",
            )
        return _ExecutionStopResult(
            attempted_ids=tuple(sorted(attempted)),
            confirmed_statuses=dict(sorted(confirmed.items())),
            failures=dict(sorted(failures.items())),
        )

    async def _list_run_executions(self, run_id: str) -> list[Execution]:
        executions: list[Execution] = []
        offset = 0
        while True:
            page = list(
                await self._execution_repository.list(
                    run_id,
                    limit=_EXECUTION_LIST_PAGE_SIZE,
                    offset=offset,
                )
            )
            executions.extend(page)
            if len(page) < _EXECUTION_LIST_PAGE_SIZE:
                return executions
            offset += len(page)

    async def _cancel_and_confirm(self, execution: Execution) -> Execution:
        await self._execution_runner.cancel(execution.id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._execution_cancel_timeout_seconds
        while True:
            current = await self._execution_repository.get(execution.id)
            if current is None:
                raise RuntimeError("execution disappeared before stop could be confirmed")
            if current.status not in _STOP_CANDIDATE_EXECUTION_STATUSES:
                return current
            if loop.time() >= deadline:
                if current.status is ExecutionStatus.LOST:
                    raise TimeoutError(
                        "execution remains lost; cancellation was queued but the Runner "
                        "did not acknowledge that the process stopped"
                    )
                raise TimeoutError(
                    f"execution remains {current.status.value} after cancellation"
                )
            await asyncio.sleep(self._execution_cancel_poll_seconds)

    async def _transition_if_possible(self, run: Run, target: RunStatus) -> Run:
        current = await self.get_run(run.id)
        if current.status is target:
            return current
        if not current.can_transition_to(target):
            return current
        return await self._run_repository.update_status(current.id, target)

    @staticmethod
    def _stop_event_payload(
        result: _ExecutionStopResult,
        *,
        workflow_synced: bool,
    ) -> dict[str, object]:
        return {
            "execution_ids": list(result.attempted_ids),
            "confirmed_execution_ids": list(result.confirmed_ids),
            "confirmed_statuses": result.confirmed_statuses,
            "failed_executions": result.failures,
            "workflow_synced": workflow_synced,
        }

    @staticmethod
    def _raise_if_stop_failed(run: Run, result: _ExecutionStopResult) -> None:
        if result.succeeded:
            return
        raise ServiceUnavailableError(
            "execution_cancel_failed",
            "Could not confirm that every execution requiring a safety stop was stopped",
            details={
                "run_id": run.id,
                "execution_ids": list(result.attempted_ids),
                "confirmed_execution_ids": list(result.confirmed_ids),
                "failed_executions": result.failures,
            },
        )
