"""Dynamic function tools exposed to the primary Agent."""

from __future__ import annotations

import json
from pathlib import Path

from agents import FunctionTool, RunContextWrapper, function_tool
from agents.tool_context import ToolContext

from riftx.domain import (
    ApprovalLevel,
    Finding,
    FindingEvidence,
    FindingSeverity,
    requires_approval,
)
from riftx.skills import RegisteredToolArguments, ShellArguments, SkillContext
from riftx.tools import ExecutionPolicy, ToolUnavailableError

from .context import RiftXAgentContext
from .services import AgentRuntimeServices


def build_agent_tools(services: AgentRuntimeServices) -> list[FunctionTool]:
    """Build only the base tools allowed by the current node execution policy."""

    async def _registered_tool_needs_approval(
        wrapper: RunContextWrapper[RiftXAgentContext],
        arguments: dict[str, object],
        _call_id: str,
    ) -> bool:
        context = wrapper.context
        tool_id = str(arguments.get("tool_id") or "")
        snapshot = next(
            (item for item in context.available_tools if item.id == tool_id),
            None,
        )
        if snapshot is None:
            return False
        level = snapshot.approval_level
        granted = (
            await services.approval_repository.is_granted(context.run_id, tool_id)
            if services.approval_repository is not None and tool_id
            else False
        )
        return requires_approval(
            context.approval_mode,
            level,
            granted_for_run=granted,
        )

    async def _shell_needs_approval(
        wrapper: RunContextWrapper[RiftXAgentContext],
        _arguments: dict[str, object],
        _call_id: str,
    ) -> bool:
        context = wrapper.context
        granted = (
            await services.approval_repository.is_granted(context.run_id, "run_shell")
            if services.approval_repository is not None
            else False
        )
        return requires_approval(
            context.approval_mode,
            ApprovalLevel.SENSITIVE,
            granted_for_run=granted,
        )

    async def _tool_failed(
        context: RiftXAgentContext,
        tool_name: str,
        error: Exception,
    ) -> None:
        await services.event_repository.append(
            context.run_id,
            "agent.tool_failed",
            {
                "agent_step_id": context.agent_step_id,
                "tool": tool_name,
                "error_type": type(error).__name__,
                "message": str(error)[:1000],
            },
        )

    @function_tool
    async def list_available_tools(ctx: ToolContext[RiftXAgentContext]) -> str:
        """List the tools that are actually available on the selected execution node."""

        return json.dumps(
            [item.model_dump(mode="json") for item in ctx.context.available_tools],
            ensure_ascii=False,
        )

    @function_tool(needs_approval=_registered_tool_needs_approval)
    async def run_registered_tool(
        ctx: ToolContext[RiftXAgentContext],
        tool_id: str,
        args: list[str],
        timeout_seconds: float | None = None,
        reason: str = "",
    ) -> str:
        """Run one available registered tool with an argv list, never a shell string."""

        context = ctx.context
        visible_tool_ids = {item.id for item in context.available_tools}
        await services.event_repository.append(
            context.run_id,
            "agent.tool_started",
            {
                "agent_step_id": context.agent_step_id,
                "tool": "run_registered_tool",
                "registered_tool_id": tool_id,
                "tool_call_id": ctx.tool_call_id,
            },
        )
        try:
            if tool_id not in visible_tool_ids:
                raise ToolUnavailableError(
                    f"tool {tool_id!r} is not available to this Agent on node {context.node_id!r}"
                )
            skill = services.skill_registry.get("run_registered_tool")
            result = await skill.execute(
                _skill_context(context, services),
                RegisteredToolArguments(
                    tool_id=tool_id,
                    args=args,
                    timeout_seconds=timeout_seconds,
                    execution_key=_execution_key(context, ctx.tool_call_id, tool_id),
                ),
            )
        except Exception as exc:
            await _tool_failed(context, "run_registered_tool", exc)
            raise
        await services.event_repository.append(
            context.run_id,
            "agent.tool_completed",
            {
                "agent_step_id": context.agent_step_id,
                "tool": "run_registered_tool",
                "registered_tool_id": tool_id,
                "tool_call_id": ctx.tool_call_id,
                "execution_id": result.execution_id,
                "status": result.status.value,
                "exit_code": result.exit_code,
            },
        )
        return result.model_dump_json()

    @function_tool
    async def create_finding(
        ctx: ToolContext[RiftXAgentContext],
        title: str,
        severity: FindingSeverity,
        affected_assets: list[str],
        description: str,
        evidence: list[FindingEvidence],
        reproduction_steps: list[str],
        impact: str,
        recommendation: str,
    ) -> str:
        """Create a structured finding supported by execution or artifact evidence."""

        finding = Finding(
            run_id=ctx.context.run_id,
            title=title,
            severity=severity,
            affected_assets=affected_assets,
            description=description,
            evidence=evidence,
            reproduction_steps=reproduction_steps,
            impact=impact,
            recommendation=recommendation,
        )
        await services.finding_repository.create(finding)
        await services.event_repository.append(
            ctx.context.run_id,
            "finding.created",
            {
                "finding_id": finding.id,
                "title": finding.title,
                "severity": finding.severity.value,
                "agent_step_id": ctx.context.agent_step_id,
            },
        )
        return finding.model_dump_json()

    @function_tool
    async def update_plan(ctx: ToolContext[RiftXAgentContext], plan_summary: str) -> str:
        """Publish a concise plan summary without hidden chain-of-thought."""

        ctx.context.plan_summary = plan_summary
        await services.event_repository.append(
            ctx.context.run_id,
            "agent.plan_updated",
            {
                "agent_step_id": ctx.context.agent_step_id,
                "plan_summary": plan_summary,
            },
        )
        return "Plan updated."

    @function_tool
    async def complete_run(ctx: ToolContext[RiftXAgentContext], run_summary: str) -> str:
        """Request completion after the objective and required success criteria are satisfied."""

        ctx.context.completion_requested = True
        ctx.context.run_summary = run_summary
        await services.event_repository.append(
            ctx.context.run_id,
            "agent.completion_requested",
            {
                "agent_step_id": ctx.context.agent_step_id,
                "run_summary": run_summary,
            },
        )
        return "Run completion requested."

    tools = [
        list_available_tools,
        run_registered_tool,
        create_finding,
        update_plan,
        complete_run,
    ]

    if services.tool_registry.config.execution_policy is ExecutionPolicy.OPEN:

        @function_tool(needs_approval=_shell_needs_approval)
        async def run_shell(
            ctx: ToolContext[RiftXAgentContext],
            script: str,
            timeout_seconds: float | None = None,
            reason: str = "",
        ) -> str:
            """Run an explicit shell script only when the node execution policy permits it."""

            context = ctx.context
            await services.event_repository.append(
                context.run_id,
                "agent.tool_started",
                {
                    "agent_step_id": context.agent_step_id,
                    "tool": "run_shell",
                    "tool_call_id": ctx.tool_call_id,
                },
            )
            try:
                skill = services.skill_registry.get("run_shell")
                result = await skill.execute(
                    _skill_context(context, services),
                    ShellArguments(
                        script=script,
                        timeout_seconds=timeout_seconds,
                        execution_key=_execution_key(context, ctx.tool_call_id, "shell"),
                    ),
                )
            except Exception as exc:
                await _tool_failed(context, "run_shell", exc)
                raise
            await services.event_repository.append(
                context.run_id,
                "agent.tool_completed",
                {
                    "agent_step_id": context.agent_step_id,
                    "tool": "run_shell",
                    "tool_call_id": ctx.tool_call_id,
                    "execution_id": result.execution_id,
                    "status": result.status.value,
                    "exit_code": result.exit_code,
                },
            )
            return result.model_dump_json()

        tools.insert(2, run_shell)

    return tools


def _skill_context(
    context: RiftXAgentContext,
    services: AgentRuntimeServices,
) -> SkillContext:
    return SkillContext(
        run_id=context.run_id,
        node_id=context.node_id,
        agent_step_id=context.agent_step_id,
        cwd=Path(context.workspace),
        supervisor=services.supervisor,
        tool_registry=services.tool_registry,
        node_environment=services.node_environment,
        run_environment=services.run_environment,
    )


def _execution_key(
    context: RiftXAgentContext,
    tool_call_id: str,
    discriminator: str,
) -> str:
    return f"{context.run_id}:{context.agent_step_id}:{tool_call_id}:{discriminator}"
