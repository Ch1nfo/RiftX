"""Dynamic function tools exposed to the primary Agent."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agents import FunctionTool, RunContextWrapper, function_tool
from agents.tool_context import ToolContext

from riftx.application.services import (
    CreateFinding,
    CreateTerminal,
    RegisterArtifact,
    TerminalView,
)
from riftx.domain import (
    ApprovalLevel,
    FindingEvidence,
    FindingSeverity,
    Scope,
    TerminalOwner,
    requires_approval,
)
from riftx.skills import (
    PortScanArguments,
    RegisteredToolArguments,
    ShellArguments,
    SkillContext,
    SkillResult,
)
from riftx.tools import ExecutionPolicy, ToolUnavailableError

from .context import RiftXAgentContext
from .services import AgentRuntimeServices
from .tool_policy import validate_agent_tool_inventory


def build_agent_tools(services: AgentRuntimeServices) -> list[FunctionTool]:
    """Build base tools plus structured skills supported by the selected node."""

    async def _approval_required(
        context: RiftXAgentContext,
        tool_id: str,
        level: ApprovalLevel,
    ) -> bool:
        granted = (
            await services.approval_repository.is_granted(context.run_id, tool_id)
            if services.approval_repository is not None and tool_id
            else False
        )
        return requires_approval(context.approval_mode, level, granted_for_run=granted)

    async def _registered_tool_needs_approval(
        wrapper: RunContextWrapper[RiftXAgentContext],
        arguments: dict[str, object],
        _call_id: str,
    ) -> bool:
        tool_id = str(arguments.get("tool_id") or "")
        snapshot = next(
            (item for item in wrapper.context.available_tools if item.id == tool_id),
            None,
        )
        if snapshot is None:
            return False
        return await _approval_required(wrapper.context, tool_id, snapshot.approval_level)

    async def _shell_needs_approval(
        wrapper: RunContextWrapper[RiftXAgentContext],
        _arguments: dict[str, object],
        _call_id: str,
    ) -> bool:
        return await _approval_required(
            wrapper.context,
            "run_shell",
            ApprovalLevel.SENSITIVE,
        )

    async def _terminal_needs_approval(
        wrapper: RunContextWrapper[RiftXAgentContext],
        arguments: dict[str, object],
        _call_id: str,
    ) -> bool:
        tool_id = str(arguments.get("tool_id") or "")
        snapshot = next(
            (item for item in wrapper.context.available_tools if item.id == tool_id),
            None,
        )
        level = snapshot.approval_level if snapshot is not None else ApprovalLevel.SENSITIVE
        return await _approval_required(wrapper.context, tool_id or "open_terminal", level)

    async def _port_scan_needs_approval(
        wrapper: RunContextWrapper[RiftXAgentContext],
        arguments: dict[str, object],
        _call_id: str,
    ) -> bool:
        requested = str(arguments.get("tool_id") or "") or None
        skill = services.skill_registry.get("port_scan")
        try:
            selected = services.skill_registry.select_tool(
                services.tool_registry,
                required_capabilities=skill.required_capabilities,
                preferred_tools=skill.preferred_tools,
                requested_tool_id=requested,
            )
        except ToolUnavailableError:
            return False
        return await _approval_required(
            wrapper.context,
            selected.id,
            selected.approval_level,
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
        """List tools actually available on the selected execution node."""

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
        """Run one available non-interactive registered tool with an argv list."""

        context = ctx.context
        visible_tool_ids = {item.id for item in context.available_tools}
        await _tool_started(context, services, "run_registered_tool", ctx.tool_call_id, tool_id)
        try:
            if tool_id not in visible_tool_ids:
                raise ToolUnavailableError(
                    f"tool {tool_id!r} is not available to this Agent on node {context.node_id!r}"
                )
            result = await services.skill_registry.get("run_registered_tool").execute(
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
        await _tool_completed(
            context,
            services,
            "run_registered_tool",
            ctx.tool_call_id,
            result,
            tool_id,
        )
        return result.model_dump_json()

    @function_tool(needs_approval=_terminal_needs_approval)
    async def open_terminal(
        ctx: ToolContext[RiftXAgentContext],
        tool_id: str | None = None,
        args: list[str] | None = None,
        argv: list[str] | None = None,
        cwd: str | None = None,
        cols: int = 120,
        rows: int = 40,
        reason: str = "",
    ) -> str:
        """Open an interactive registered PTY tool, or explicit argv in open policy."""

        context = ctx.context
        command = _terminal_argv(
            context,
            services,
            tool_id=tool_id,
            args=args or [],
            argv=argv or [],
        )
        tool_state = services.tool_registry.snapshot.states.get(tool_id) if tool_id else None
        view = await services.terminal_service.create(
            context.run_id,
            CreateTerminal(
                argv=command,
                tool_id=tool_id,
                tool_version=tool_state.version if tool_state is not None else None,
                cwd=cwd,
                cols=cols,
                rows=rows,
                owner=TerminalOwner.AGENT,
            ),
        )
        return json.dumps(_terminal_view(view), ensure_ascii=False)

    @function_tool
    async def read_terminal(
        ctx: ToolContext[RiftXAgentContext],
        session_id: str,
        cursor: int = 0,
        max_bytes: int = 65536,
    ) -> str:
        """Read bounded terminal output from an exact transcript cursor."""

        await _require_run_terminal(ctx.context, services, session_id)
        output = await services.terminal_service.read(
            session_id,
            cursor=cursor,
            max_bytes=min(max(max_bytes, 1), 1024 * 1024),
        )
        return json.dumps(
            {
                "session_id": session_id,
                "data": output.data.decode("utf-8", errors="replace"),
                "cursor": output.cursor,
                "next_cursor": output.next_cursor,
                "eof": output.eof,
            },
            ensure_ascii=False,
        )

    @function_tool
    async def send_terminal_input(
        ctx: ToolContext[RiftXAgentContext],
        session_id: str,
        data: str,
    ) -> str:
        """Send UTF-8 input as the Agent to an Agent-owned terminal."""

        await _require_run_terminal(ctx.context, services, session_id)
        await services.terminal_service.write(
            session_id,
            data.encode("utf-8"),
            actor=TerminalOwner.AGENT,
        )
        return "Terminal input sent."

    @function_tool
    async def close_terminal(
        ctx: ToolContext[RiftXAgentContext],
        session_id: str,
    ) -> str:
        """Close an interactive terminal session and return its final state."""

        await _require_run_terminal(ctx.context, services, session_id)
        return json.dumps(
            _terminal_view(await services.terminal_service.close(session_id)),
            ensure_ascii=False,
        )

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

        finding = await services.finding_service.create_finding(
            ctx.context.run_id,
            CreateFinding(
                title=title,
                severity=severity,
                affected_assets=affected_assets,
                description=description,
                evidence=evidence,
                reproduction_steps=reproduction_steps,
                impact=impact,
                recommendation=recommendation,
                agent_step_id=ctx.context.agent_step_id,
            ),
        )
        return finding.model_dump_json()

    @function_tool
    async def add_artifact(
        ctx: ToolContext[RiftXAgentContext],
        source_path: str,
        name: str | None = None,
        mime_type: str | None = None,
        description: str = "",
        execution_id: str | None = None,
    ) -> str:
        """Snapshot a workspace or execution file as an immutable Run artifact."""

        artifact = await services.artifact_service.register(
            ctx.context.run_id,
            RegisterArtifact(
                source_path=source_path,
                name=name,
                mime_type=mime_type,
                description=description,
                execution_id=execution_id,
            ),
        )
        return artifact.model_dump_json()

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
        """Request completion after objective and required criteria are satisfied."""

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
        open_terminal,
        read_terminal,
        send_terminal_input,
        close_terminal,
        create_finding,
        add_artifact,
        update_plan,
        complete_run,
    ]

    if any("port_scan" in item.capabilities for item in services.tool_registry.available_tools()):

        @function_tool(needs_approval=_port_scan_needs_approval)
        async def port_scan(
            ctx: ToolContext[RiftXAgentContext],
            target: str,
            ports: str | None = None,
            service_detection: bool = False,
            tool_id: str | None = None,
            timeout_seconds: float | None = None,
            reason: str = "",
        ) -> str:
            """Scan an explicitly in-scope target and return machine-parsed results."""

            context = ctx.context
            try:
                result = await services.skill_registry.get("port_scan").execute(
                    _skill_context(context, services),
                    PortScanArguments(
                        target=target,
                        ports=ports,
                        service_detection=service_detection,
                        tool_id=tool_id,
                        timeout_seconds=timeout_seconds,
                        execution_key=_execution_key(context, ctx.tool_call_id, "port_scan"),
                    ),
                )
            except Exception as exc:
                await _tool_failed(context, "port_scan", exc)
                raise
            return result.model_dump_json()

        tools.insert(2, port_scan)

    if services.tool_registry.config.execution_policy is ExecutionPolicy.OPEN:

        @function_tool(needs_approval=_shell_needs_approval)
        async def run_shell(
            ctx: ToolContext[RiftXAgentContext],
            script: str,
            timeout_seconds: float | None = None,
            reason: str = "",
        ) -> str:
            """Run an explicit shell script only when node policy permits it."""

            context = ctx.context
            await _tool_started(context, services, "run_shell", ctx.tool_call_id)
            try:
                result = await services.skill_registry.get("run_shell").execute(
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
            await _tool_completed(
                context,
                services,
                "run_shell",
                ctx.tool_call_id,
                result,
            )
            return result.model_dump_json()

        tools.insert(2, run_shell)

    validate_agent_tool_inventory(tools)
    return tools


async def _require_run_terminal(
    context: RiftXAgentContext,
    services: AgentRuntimeServices,
    session_id: str,
) -> None:
    view = await services.terminal_service.get(session_id)
    if view.terminal.run_id != context.run_id:
        # Do not reveal whether a terminal from another Run exists. Agent tools
        # are always scoped to their immutable Run context.
        raise ToolUnavailableError("terminal session is not available to this Run")


async def _tool_started(
    context: RiftXAgentContext,
    services: AgentRuntimeServices,
    tool_name: str,
    tool_call_id: str,
    registered_tool_id: str | None = None,
) -> None:
    payload = {
        "agent_step_id": context.agent_step_id,
        "tool": tool_name,
        "tool_call_id": tool_call_id,
    }
    if registered_tool_id:
        payload["registered_tool_id"] = registered_tool_id
    await services.event_repository.append(context.run_id, "agent.tool_started", payload)


async def _tool_completed(
    context: RiftXAgentContext,
    services: AgentRuntimeServices,
    tool_name: str,
    tool_call_id: str,
    result: SkillResult,
    registered_tool_id: str | None = None,
) -> None:
    payload = {
        "agent_step_id": context.agent_step_id,
        "tool": tool_name,
        "tool_call_id": tool_call_id,
        "execution_id": result.execution_id,
        "status": result.status.value,
        "exit_code": result.exit_code,
    }
    if registered_tool_id:
        payload["registered_tool_id"] = registered_tool_id
    await services.event_repository.append(context.run_id, "agent.tool_completed", payload)


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
        scope=_scope_from_items(context.scope),
        node_environment=services.node_environment,
        run_environment=services.run_environment,
    )


def _scope_from_items(items: list[str]) -> Scope:
    values: dict[str, list[str]] = {
        "cidrs": [],
        "ips": [],
        "domains": [],
        "url_prefixes": [],
        "asset_tags": [],
        "exclusions": [],
    }
    mapping = {
        "cidr": "cidrs",
        "ip": "ips",
        "domain": "domains",
        "url": "url_prefixes",
        "tag": "asset_tags",
        "exclude": "exclusions",
    }
    for item in items:
        prefix, separator, value = item.partition(":")
        field = mapping.get(prefix)
        if separator and field and value:
            values[field].append(value)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    for item in items:
        prefix, separator, value = item.partition(":")
        if not separator or not value:
            continue
        if prefix == "starts_at":
            starts_at = datetime.fromisoformat(value)
        elif prefix == "ends_at":
            ends_at = datetime.fromisoformat(value)
    return Scope(**values, starts_at=starts_at, ends_at=ends_at)


def _terminal_argv(
    context: RiftXAgentContext,
    services: AgentRuntimeServices,
    *,
    tool_id: str | None,
    args: list[str],
    argv: list[str],
) -> list[str]:
    if tool_id:
        visible = {item.id for item in context.available_tools}
        if tool_id not in visible:
            raise ToolUnavailableError(f"tool {tool_id!r} is not available to this Agent")
        definition = services.tool_registry.get_available(tool_id)
        if definition.executor.value != "pty":
            raise ToolUnavailableError(f"tool {tool_id!r} is not configured for PTY execution")
        if argv:
            raise ValueError("argv cannot be combined with a registered terminal tool")
        return services.tool_registry.build_argv(tool_id, args)
    if services.tool_registry.config.execution_policy is ExecutionPolicy.REGISTERED_ONLY:
        raise ToolUnavailableError("explicit terminal argv is disabled by execution policy")
    if args:
        raise ValueError("args require tool_id")
    if not argv or any(not item for item in argv):
        raise ValueError("open_terminal requires tool_id or non-empty argv")
    return argv


def _terminal_view(view: TerminalView) -> dict[str, object]:
    terminal = view.terminal
    execution = view.execution
    return {
        "id": terminal.id,
        "run_id": terminal.run_id,
        "execution_id": terminal.execution_id,
        "status": terminal.status.value,
        "owner": terminal.owner.value,
        "cols": terminal.cols,
        "rows": terminal.rows,
        "argv": execution.argv,
        "cwd": execution.cwd,
        "pid": execution.pid,
        "exit_code": execution.exit_code,
        "execution_status": execution.status.value,
    }


def _execution_key(
    context: RiftXAgentContext,
    tool_call_id: str,
    discriminator: str,
) -> str:
    return f"{context.run_id}:{context.agent_step_id}:{tool_call_id}:{discriminator}"
