"""Application service for durable Run creation and control."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    ServiceUnavailableError,
)
from riftx.application.ports import EngagementRepository, RunEventRepository, RunRepository
from riftx.domain import (
    ApprovalMode,
    Engagement,
    EntryPoint,
    Objective,
    Run,
    RunStatus,
    Scope,
    SuccessCriterion,
)


class RunWorkflowClient(Protocol):
    """Small Temporal boundary consumed by the control plane."""

    async def start_run(self, run_id: str) -> object: ...

    async def pause(self, run_id: str) -> None: ...

    async def resume(self, run_id: str) -> None: ...

    async def approve(self, run_id: str, call_id: str) -> None: ...

    async def reject(self, run_id: str, call_id: str) -> None: ...

    async def cancel_current_execution(self, run_id: str) -> None: ...

    async def cancel(self, run_id: str) -> None: ...

    async def append_user_message(self, run_id: str, message: str) -> None: ...

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
    success_criteria: list[SuccessCriterion] = field(default_factory=list)
    entry_points: list[EntryPoint] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    workspace_path: str | None = None
    engagement_id: str | None = None
    engagement: CreateEngagement | None = None


class RunApplicationService:
    def __init__(
        self,
        *,
        engagement_repository: EngagementRepository,
        run_repository: RunRepository,
        event_repository: RunEventRepository,
        workflow_client: RunWorkflowClient,
        workspace_root: Path,
    ) -> None:
        self._engagement_repository = engagement_repository
        self._run_repository = run_repository
        self._event_repository = event_repository
        self._workflow_client = workflow_client
        self._workspace_root = workspace_root

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
        await self._invoke_workflow(run, "pause", self._workflow_client.pause)
        await self._event_repository.append(run.id, "run.pause_requested")
        return run

    async def resume(self, run_id: str) -> Run:
        run = await self._require_controllable_run(run_id, action="resume")
        await self._invoke_workflow(run, "resume", self._workflow_client.resume)
        await self._event_repository.append(run.id, "run.resume_requested")
        return run

    async def cancel_current_execution(self, run_id: str) -> Run:
        run = await self._require_controllable_run(
            run_id,
            action="cancel the current execution for",
        )
        await self._invoke_workflow(
            run,
            "cancel the current execution",
            self._workflow_client.cancel_current_execution,
        )
        await self._event_repository.append(run.id, "execution.cancel_requested")
        return run

    async def cancel(self, run_id: str) -> Run:
        run = await self._require_controllable_run(run_id, action="cancel")
        await self._invoke_workflow(run, "cancel", self._workflow_client.cancel)
        await self._event_repository.append(run.id, "run.cancel_requested")
        return run

    async def append_user_message(self, run_id: str, message: str) -> Run:
        run = await self._require_controllable_run(run_id, action="send a message to")
        normalized = message.strip()
        if not normalized:
            raise ApplicationConflictError(
                "empty_message",
                "A run message must not be empty",
            )
        await self._invoke_workflow(
            run,
            "send a message",
            lambda target: self._workflow_client.append_user_message(target, normalized),
        )
        await self._event_repository.append(
            run.id,
            "user.message_queued",
            {"message": normalized},
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
        except Exception as exc:
            raise ServiceUnavailableError(
                "temporal_unavailable",
                f"Could not {action} because Temporal is unavailable",
                details={"run_id": run.id},
            ) from exc
