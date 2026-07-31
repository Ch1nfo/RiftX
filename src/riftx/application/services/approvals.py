"""Approval request recording and durable decision application."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    ApplicationServiceError,
    EntityNotFoundError,
    RepositoryConflictError,
    ServiceUnavailableError,
)
from riftx.application.ports import ApprovalRepository, RunEventRepository, RunRepository
from riftx.domain import Approval, ApprovalStatus, Run, RunStatus, ToolCall
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    ApprovalDecision,
    RuntimeApprovalRequest,
    ToolCallIntent,
)
from riftx.tools import ToolRegistry

_APPROVAL_BLOCKED_RUN_STATUSES = frozenset(
    {
        RunStatus.PAUSING,
        RunStatus.CANCELLING,
        RunStatus.COMPLETING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)


class ApprovalWorkflowClient(Protocol):
    async def approve(self, run_id: str, approval_id: str) -> None: ...

    async def reject(self, run_id: str, approval_id: str) -> None: ...


class RuntimeApprovalRepository(Protocol):
    async def create(self, request: RuntimeApprovalRequest) -> RuntimeApprovalRequest: ...

    async def get(self, approval_id: str) -> RuntimeApprovalRequest | None: ...

    async def save(self, request: RuntimeApprovalRequest) -> RuntimeApprovalRequest: ...


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


class RuntimeApprovalRequestRecorder:
    """Bridge a durable ToolCallIntent into the control-plane approval API."""

    def __init__(
        self,
        *,
        approval_repository: ApprovalRepository,
        runtime_repository: RuntimeApprovalRepository,
        event_repository: RunEventRepository,
    ) -> None:
        self._approvals = approval_repository
        self._runtime = runtime_repository
        self._events = event_repository

    async def record(
        self,
        run: Run,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        step: AgentStep,
        intent: ToolCallIntent,
        context_compilation_id: str | None,
        working_memory_version: int | None,
    ) -> RuntimeApprovalRequest:
        call_id = intent.engine_call_id or intent.id
        tool_id = intent.tool_id or intent.skill_id or "unknown"
        tool_call = ToolCall(
            sdk_call_id=call_id,
            run_id=run.id,
            agent_step_id=step.id,
            tool_id=tool_id,
            skill_id=intent.skill_id,
            arguments=intent.arguments,
        )
        command, cwd, env = _intent_execution_summary(intent)
        approval = Approval(
            run_id=run.id,
            tool_call_id=tool_call.id,
            tool_name=tool_id,
            command=command,
            cwd=cwd or run.workspace_path,
            target_summary=intent.target_summary or _target_summary(run),
            env_diff=env,
            reason=intent.reason or f"Agent requested {tool_id!r} during step {step.id}.",
        )
        approval, created = await self._approvals.create_request(tool_call, approval)
        request = await self._runtime.create(
            RuntimeApprovalRequest(
                id=approval.id,
                run_id=run.id,
                session_id=session.id,
                cycle_id=cycle.id,
                tool_call_intent_id=intent.id,
                context_compilation_id=context_compilation_id,
                working_memory_version=working_memory_version,
            )
        )
        if created:
            await self._events.append(
                run.id,
                "tool.approval_required",
                {
                    "approval_id": request.id,
                    "tool_call_id": approval.tool_call_id,
                    "tool_call_intent_id": intent.id,
                    "sdk_call_id": call_id,
                    "tool_name": approval.tool_name,
                    "command": approval.command,
                    "cwd": approval.cwd,
                    "target_summary": approval.target_summary,
                    "env_diff": approval.env_diff,
                    "reason": approval.reason,
                    "context_compilation_id": context_compilation_id,
                    "working_memory_version": working_memory_version,
                    "agent_step_id": step.id,
                },
            )
        return request


class ApprovalApplicationService:
    def __init__(
        self,
        *,
        approval_repository: ApprovalRepository,
        run_repository: RunRepository,
        event_repository: RunEventRepository,
        workflow_client: ApprovalWorkflowClient,
        runtime_approval_repository: RuntimeApprovalRepository | None = None,
    ) -> None:
        self._approval_repository = approval_repository
        self._run_repository = run_repository
        self._event_repository = event_repository
        self._workflow_client = workflow_client
        self._runtime_approvals = runtime_approval_repository

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
        run = await self._run_repository.get(approval.run_id)
        if run is None:
            raise EntityNotFoundError("Run", approval.run_id)
        self._raise_if_approval_not_actionable(approval, run)
        tool_call = await self._approval_repository.get_tool_call(approval.tool_call_id)
        if tool_call is None:
            raise EntityNotFoundError("ToolCall", approval.tool_call_id)

        try:
            approval, changed = await self._approval_repository.decide(
                approval_id,
                target,
                decided_by=command.decided_by,
                reason=command.reason,
                blocked_run_statuses=_APPROVAL_BLOCKED_RUN_STATUSES,
            )
        except RepositoryConflictError:
            # The durable Run fence and Approval decision serialize in one
            # repository transaction. Re-read after losing that race so the
            # public API reports the lifecycle state instead of a generic
            # persistence conflict.
            current = await self._run_repository.get(approval.run_id)
            if current is not None and current.status in _APPROVAL_BLOCKED_RUN_STATUSES:
                self._raise_if_approval_not_actionable(approval, current)
            raise
        # A same-direction retry is normally only a durable signal retry.  It
        # can also recover the narrow crash window after the public Approval
        # committed but before its RuntimeApprovalRequest did.  In that case
        # reconstruct strictly from persisted state, never from the retry
        # payload; an unprovable Run-wide grant therefore becomes APPROVE_ONCE.
        if self._runtime_approvals is not None:
            runtime_request = await self._runtime_approvals.get(approval_id)
            if runtime_request is not None and runtime_request.status is ApprovalStatus.PENDING:
                decision, decided_by, feedback = (
                    (
                        _runtime_decision(target, command),
                        command.decided_by,
                        command.reason,
                    )
                    if changed
                    else _persisted_runtime_decision(approval)
                )
                runtime_request.decide(
                    decision,
                    decided_by=decided_by,
                    feedback=feedback,
                )
                await self._runtime_approvals.save(runtime_request)
        if changed and target is ApprovalStatus.APPROVED and command.approve_for_run:
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

        # A safety fence can win immediately after the atomic decision. The
        # saved decision remains retryable after PAUSED is reached, but it must
        # not release the Workflow while physical stop acknowledgement is still
        # pending (or while finalization owns the Run).
        current = await self._run_repository.get(approval.run_id)
        if current is None:
            raise EntityNotFoundError("Run", approval.run_id)
        self._raise_if_approval_not_actionable(approval, current, approval_saved=True)

        try:
            if target is ApprovalStatus.APPROVED:
                await self._workflow_client.approve(approval.run_id, approval.id)
            else:
                await self._workflow_client.reject(approval.run_id, approval.id)
        except ApplicationServiceError as exc:
            raise type(exc)(
                exc.code,
                f"Approval was saved, but the durable workflow could not be signaled: "
                f"{exc.message}",
                details={
                    **exc.details,
                    "approval_id": approval.id,
                    "run_id": approval.run_id,
                    "approval_saved": True,
                },
            ) from exc
        except Exception as exc:
            raise ServiceUnavailableError(
                "temporal_unavailable",
                "Approval was saved but the durable workflow could not be signaled",
                details={
                    "approval_id": approval.id,
                    "run_id": approval.run_id,
                    "approval_saved": True,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            ) from exc
        return approval

    @staticmethod
    def _raise_if_approval_not_actionable(
        approval: Approval,
        run: Run,
        *,
        approval_saved: bool = False,
    ) -> None:
        if run.status not in _APPROVAL_BLOCKED_RUN_STATUSES:
            return
        details: dict[str, object] = {
            "approval_id": approval.id,
            "run_id": run.id,
            "run_status": run.status.value,
        }
        if approval_saved:
            details["approval_saved"] = True
        raise ApplicationConflictError(
            "approval_not_actionable",
            f"Cannot decide Approval {approval.id!r} after Run {run.id!r} "
            f"became {run.status.value}",
            details=details,
        )


def _runtime_decision(target: ApprovalStatus, command: DecideApproval) -> ApprovalDecision:
    if target is ApprovalStatus.APPROVED:
        return (
            ApprovalDecision.APPROVE_TOOL_FOR_RUN
            if command.approve_for_run
            else ApprovalDecision.APPROVE_ONCE
        )
    return (
        ApprovalDecision.REJECT_WITH_FEEDBACK
        if command.reason and command.reason.strip()
        else ApprovalDecision.REJECT
    )


def _persisted_runtime_decision(
    approval: Approval,
) -> tuple[ApprovalDecision, str, str | None]:
    """Recover a split Approval write without trusting a retry payload."""

    decided_by = approval.decided_by or "unknown-operator"
    if approval.status is ApprovalStatus.APPROVED:
        return ApprovalDecision.APPROVE_ONCE, decided_by, None
    if approval.status is ApprovalStatus.REJECTED:
        feedback = approval.reason.strip() or None
        return (
            ApprovalDecision.REJECT_WITH_FEEDBACK if feedback else ApprovalDecision.REJECT,
            decided_by,
            feedback,
        )
    raise ValueError(f"Approval {approval.id!r} has no persisted decision to recover")


def _intent_execution_summary(
    intent: ToolCallIntent,
) -> tuple[list[str], str, dict[str, str | None]]:
    spec = intent.execution_spec or {}
    argv = spec.get("argv")
    command = [str(value) for value in argv] if isinstance(argv, list) else []
    command_text = spec.get("command_text")
    shell_path = spec.get("shell_path")
    if not command and isinstance(command_text, str) and command_text:
        command = [str(shell_path or "shell"), "-lc", command_text]
    cwd = str(spec.get("cwd") or "")
    raw_env = spec.get("env")
    env = (
        {str(key): None if value is None else str(value) for key, value in raw_env.items()}
        if isinstance(raw_env, dict)
        else {}
    )
    return command, cwd, env


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
