"""Approval request recording and durable decision application."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    ServiceUnavailableError,
)
from riftx.application.ports import ApprovalRepository, RunEventRepository, RunRepository
from riftx.domain import Approval, ApprovalStatus, Run, ToolCall
from riftx.tools import ToolRegistry


class ApprovalWorkflowClient(Protocol):
    async def approve(self, run_id: str, call_id: str) -> None: ...

    async def reject(self, run_id: str, call_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ApprovalInterruption:
    call_id: str
    tool_name: str
    arguments: str | None = None


@dataclass(frozen=True, slots=True)
class DecideApproval:
    decided_by: str = "local-user"
    reason: str | None = None
    approve_for_run: bool = False


class ApprovalRequestRecorder:
    """Persist SDK interruptions as stable ToolCall and Approval records."""

    def __init__(
        self,
        *,
        approval_repository: ApprovalRepository,
        event_repository: RunEventRepository,
        tool_registry: ToolRegistry,
    ) -> None:
        self._approval_repository = approval_repository
        self._event_repository = event_repository
        self._tool_registry = tool_registry

    async def record(
        self,
        run: Run,
        *,
        agent_step_id: str,
        checkpoint_id: str | None,
        interruptions: list[ApprovalInterruption],
    ) -> list[Approval]:
        requests: list[Approval] = []
        for interruption in interruptions:
            arguments = _decode_arguments(interruption.arguments)
            tool_id, display_name, command, env_diff = self._snapshot_tool(
                interruption.tool_name,
                arguments,
            )
            tool_call = ToolCall(
                sdk_call_id=interruption.call_id,
                run_id=run.id,
                agent_step_id=agent_step_id,
                tool_id=tool_id,
                skill_id=interruption.tool_name,
                arguments=arguments,
            )
            approval = Approval(
                run_id=run.id,
                tool_call_id=tool_call.id,
                tool_name=display_name,
                command=command,
                cwd=run.workspace_path,
                target_summary=_target_summary(run),
                env_diff=env_diff,
                reason=(
                    str(arguments.get("reason") or "").strip()
                    or f"Agent requested {display_name!r} during step {agent_step_id}."
                ),
            )
            persisted, created = await self._approval_repository.create_request(
                tool_call,
                approval,
            )
            requests.append(persisted)
            if created:
                await self._event_repository.append(
                    run.id,
                    "tool.approval_required",
                    {
                        "approval_id": persisted.id,
                        "tool_call_id": persisted.tool_call_id,
                        "sdk_call_id": interruption.call_id,
                        "tool_name": persisted.tool_name,
                        "command": persisted.command,
                        "cwd": persisted.cwd,
                        "target_summary": persisted.target_summary,
                        "env_diff": persisted.env_diff,
                        "reason": persisted.reason,
                        "checkpoint_id": checkpoint_id,
                        "agent_step_id": agent_step_id,
                    },
                )
        return requests

    def _snapshot_tool(
        self,
        function_name: str,
        arguments: dict[str, object],
    ) -> tuple[str, str, list[str], dict[str, str | None]]:
        if function_name.endswith("run_registered_tool"):
            tool_id = str(arguments.get("tool_id") or "run_registered_tool")
            definition = self._tool_registry.snapshot.definitions.get(tool_id)
            if definition is None:
                return tool_id, tool_id, [], {}
            extra_args = arguments.get("args")
            argv = [str(item) for item in extra_args] if isinstance(extra_args, list) else []
            return (
                tool_id,
                tool_id,
                [*definition.command, *argv],
                dict(definition.environment),
            )
        if function_name.endswith("run_shell"):
            script = str(arguments.get("script") or "")
            shell = _default_shell(self._tool_registry)
            return "run_shell", "run_shell", [shell, "-lc", script], {}
        return function_name, function_name, [], {}


class ApprovalApplicationService:
    def __init__(
        self,
        *,
        approval_repository: ApprovalRepository,
        run_repository: RunRepository,
        event_repository: RunEventRepository,
        workflow_client: ApprovalWorkflowClient,
    ) -> None:
        self._approval_repository = approval_repository
        self._run_repository = run_repository
        self._event_repository = event_repository
        self._workflow_client = workflow_client

    async def list(
        self,
        run_id: str,
        *,
        status: ApprovalStatus | None = None,
    ) -> list[Approval]:
        if await self._run_repository.get(run_id) is None:
            raise EntityNotFoundError("Run", run_id)
        return list(await self._approval_repository.list(run_id, status=status))

    async def approve(self, approval_id: str, command: DecideApproval) -> Approval:
        return await self._decide(approval_id, ApprovalStatus.APPROVED, command)

    async def reject(self, approval_id: str, command: DecideApproval) -> Approval:
        if command.approve_for_run:
            raise ApplicationConflictError(
                "invalid_approval_scope",
                "A rejected approval cannot grant a Run-wide tool exception",
            )
        return await self._decide(approval_id, ApprovalStatus.REJECTED, command)

    async def _decide(
        self,
        approval_id: str,
        target: ApprovalStatus,
        command: DecideApproval,
    ) -> Approval:
        approval = await self._approval_repository.get(approval_id)
        if approval is None:
            raise EntityNotFoundError("Approval", approval_id)
        tool_call = await self._approval_repository.get_tool_call(approval.tool_call_id)
        if tool_call is None:
            raise EntityNotFoundError("ToolCall", approval.tool_call_id)

        approval, changed = await self._approval_repository.decide(
            approval_id,
            target,
            decided_by=command.decided_by,
            reason=command.reason,
        )
        if target is ApprovalStatus.APPROVED and command.approve_for_run:
            await self._approval_repository.grant_for_run(
                approval.run_id,
                tool_call.tool_id,
                created_by=command.decided_by,
            )
        if changed:
            await self._event_repository.append(
                approval.run_id,
                "tool.approved" if target is ApprovalStatus.APPROVED else "tool.rejected",
                {
                    "approval_id": approval.id,
                    "tool_call_id": approval.tool_call_id,
                    "sdk_call_id": tool_call.sdk_call_id,
                    "tool_name": approval.tool_name,
                    "decided_by": approval.decided_by,
                    "reason": approval.reason,
                    "approve_for_run": command.approve_for_run,
                },
            )

        try:
            if target is ApprovalStatus.APPROVED:
                await self._workflow_client.approve(approval.run_id, tool_call.sdk_call_id)
            else:
                await self._workflow_client.reject(approval.run_id, tool_call.sdk_call_id)
        except Exception as exc:
            raise ServiceUnavailableError(
                "temporal_unavailable",
                "Approval was saved but the durable workflow could not be signaled",
                details={"approval_id": approval.id, "run_id": approval.run_id},
            ) from exc
        return approval


def _decode_arguments(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _default_shell(registry: ToolRegistry) -> str:
    shells = registry.config.shells.default
    if sys.platform == "darwin":
        return shells.macos
    if sys.platform == "win32":
        return shells.windows
    return shells.linux


def _target_summary(run: Run) -> str:
    entries = [f"{item.kind.value}:{item.value}" for item in run.entry_points]
    scope = [
        *[f"cidr:{item}" for item in run.scope.cidrs],
        *[f"ip:{item}" for item in run.scope.ips],
        *[f"domain:{item}" for item in run.scope.domains],
        *[f"url:{item}" for item in run.scope.url_prefixes],
        *[f"exclude:{item}" for item in run.scope.exclusions],
    ]
    values = [*entries, *scope]
    return ", ".join(values) if values else run.objective.description
