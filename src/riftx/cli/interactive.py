"""Claude-Code-like interactive client backed exclusively by the RiftX API."""

from __future__ import annotations

import shlex
import webbrowser
from dataclasses import dataclass

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.panel import Panel

from .client import APIClient, RiftXAPIError
from .i18n import get_language, tr
from .render import (
    render_approvals,
    render_context,
    render_error,
    render_node,
    render_nodes,
    render_run,
    render_runs,
    render_terminal,
    render_tools,
)
from .terminal import attach_terminal, control_terminal

_COMMANDS = [
    "/new",
    "/resume",
    "/runs",
    "/status",
    "/tools",
    "/node",
    "/model",
    "/mode",
    "/plan",
    "/pause",
    "/continue",
    "/cancel",
    "/compact",
    "/context",
    "/web",
    "/watch",
    "/approvals",
    "/terminal",
    "/attach",
    "/takeover",
    "/release",
    "/approve",
    "/reject",
    "/help",
    "/exit",
]


@dataclass(slots=True)
class InteractiveState:
    active_run_id: str | None = None
    active_terminal_id: str | None = None
    node_id: str = "local"
    model_profile: str | None = None
    approval_mode: str = "balanced"


def _help_text() -> str:
    if get_language() == "zh":
        return (
            "[bold]/new OBJECTIVE[/bold] 创建任务\n"
            "[bold]/resume RUN_ID[/bold] 选择已有任务\n"
            "[bold]/runs[/bold] 列出任务\n"
            "[bold]/status[/bold] 显示当前任务\n"
            "[bold]/tools [NODE][/bold] 列出工具\n"
            "[bold]/node [NODE][/bold] 列出节点或为新任务选择节点\n"
            "[bold]/model [PROFILE][/bold] 查看或选择新任务模型\n"
            "[bold]/mode [auto|balanced|manual][/bold] 查看或选择审批模式\n"
            "[bold]/plan[/bold] 显示最新 Agent 计划\n"
            "[bold]/context[/bold] 检查最新模型上下文清单\n"
            "[bold]/pause[/bold]、[bold]/continue[/bold]、[bold]/cancel[/bold] 控制当前任务\n"
            "[bold]/watch[/bold] 流式显示当前任务事件\n"
            "[bold]/approvals[/bold] 列出审批请求\n"
            "[bold]/terminal [COMMAND ...][/bold] 为当前任务启动终端\n"
            "[bold]/attach [SESSION_ID][/bold] 接管并连接终端（Ctrl+] 分离）\n"
            "[bold]/release [SESSION_ID][/bold] 将终端所有权归还 Agent\n"
            "[bold]/approve APPROVAL_ID [--for-run][/bold] 批准工具调用\n"
            "[bold]/reject APPROVAL_ID [REASON][/bold] 拒绝工具调用\n"
            "[bold]/compact [MAX_ITEMS][/bold] 压缩持久化 Agent 历史\n"
            "[bold]/web[/bold] 在 WebUI 中打开当前任务\n"
            "[bold]/exit[/bold] 退出交互模式"
        )
    return (
        "[bold]/new OBJECTIVE[/bold] create a run\n"
        "[bold]/resume RUN_ID[/bold] select an existing run\n"
        "[bold]/runs[/bold] list runs\n"
        "[bold]/status[/bold] show the active run\n"
        "[bold]/tools [NODE][/bold] list tools\n"
        "[bold]/node [NODE][/bold] list nodes or select the node for new runs\n"
        "[bold]/model [PROFILE][/bold] show or select the model for new runs\n"
        "[bold]/mode [auto|balanced|manual][/bold] show or select approval mode\n"
        "[bold]/plan[/bold] show the latest Agent plan\n"
        "[bold]/context[/bold] inspect the latest model Context Manifest\n"
        "[bold]/pause[/bold], [bold]/continue[/bold], [bold]/cancel[/bold] control the active run\n"
        "[bold]/watch[/bold] stream active run events\n"
        "[bold]/approvals[/bold] list approval requests\n"
        "[bold]/terminal [COMMAND ...][/bold] start a terminal for the active run\n"
        "[bold]/attach [SESSION_ID][/bold] take over and attach (Ctrl+] detaches)\n"
        "[bold]/release [SESSION_ID][/bold] return terminal ownership to the Agent\n"
        "[bold]/approve APPROVAL_ID [--for-run][/bold] approve a tool call\n"
        "[bold]/reject APPROVAL_ID [REASON][/bold] reject a tool call\n"
        "[bold]/compact [MAX_ITEMS][/bold] compact persisted Agent history\n"
        "[bold]/web[/bold] open the active Run in the WebUI\n"
        "[bold]/exit[/bold] close interactive mode"
    )


def run_interactive(client: APIClient, console: Console) -> None:
    state = InteractiveState()
    session: PromptSession[str] = PromptSession(
        history=InMemoryHistory(),
        completer=WordCompleter(_COMMANDS, sentence=True),
        complete_while_typing=False,
    )
    console.print(
        Panel(
            tr("Type an objective to create a Run, or use /help.").replace(
                "/help", "[bold]/help[/bold]"
            ),
            title=tr("RiftX Interactive"),
            border_style="cyan",
        )
    )

    while True:
        try:
            with patch_stdout():
                text = session.prompt(_prompt(state)).strip()
        except EOFError:
            console.print(f"[dim]{tr('Session closed.')}[/dim]")
            return
        except KeyboardInterrupt:
            console.print(f"[dim]{tr('Use /exit to leave RiftX.')}[/dim]")
            continue
        if not text:
            continue
        try:
            if text.startswith("/"):
                if _handle_command(text, state, client, console):
                    return
            else:
                _handle_message(text, state, client, console)
        except (RiftXAPIError, httpx.HTTPError, OSError, ValueError) as exc:
            render_error(console, exc)


def _handle_command(
    text: str,
    state: InteractiveState,
    client: APIClient,
    console: Console,
) -> bool:
    parts = shlex.split(text)
    command = parts[0].lower()
    args = parts[1:]
    if command == "/exit":
        return True
    if command == "/help":
        console.print(_help_text())
        return False
    if command == "/new":
        objective = " ".join(args).strip()
        if not objective:
            raise ValueError("Usage: /new OBJECTIVE")
        created = client.create_run(_new_run_payload(objective, state))
        state.active_run_id = str(created["id"])
        render_run(console, created)
        return False
    if command == "/resume":
        if not args:
            raise ValueError("Usage: /resume RUN_ID")
        selected = client.get_run(args[0])
        state.active_run_id = str(selected["id"])
        state.node_id = str(selected.get("node_id") or state.node_id)
        state.model_profile = selected.get("model_profile") or None
        state.approval_mode = str(selected.get("approval_mode") or state.approval_mode)
        render_run(console, selected)
        return False
    if command == "/runs":
        render_runs(console, client.list_runs().get("items", []))
        return False
    if command == "/status":
        render_run(console, client.get_run(_require_active(state)))
        return False
    if command == "/tools":
        render_tools(console, client.list_tools(args[0] if args else state.node_id))
        return False
    if command == "/node":
        if not args:
            render_nodes(console, client.list_nodes().get("items", []))
            return False
        selected = client.get_node(args[0])
        state.node_id = str(selected["id"])
        render_node(console, selected)
        console.print(f"[green]{tr('New runs will use node {node}.', node=state.node_id)}[/green]")
        return False
    if command == "/model":
        if not args:
            console.print(
                tr(
                    "Model for new runs: {model}",
                    model=f"[cyan]{tr(state.model_profile or 'default')}[/cyan]",
                )
            )
            return False
        state.model_profile = args[0]
        console.print(
            f"[green]{tr('New runs will use model profile {model}.', model=args[0])}[/green]"
        )
        return False
    if command == "/mode":
        if not args:
            console.print(
                tr("Approval mode for new runs: {mode}", mode=f"[cyan]{state.approval_mode}[/cyan]")
            )
            return False
        mode = args[0].lower()
        if mode not in {"auto", "balanced", "manual"}:
            raise ValueError("Usage: /mode [auto|balanced|manual]")
        state.approval_mode = mode
        console.print(f"[green]{tr('New runs will use {mode} approval mode.', mode=mode)}[/green]")
        return False
    if command == "/plan":
        events = client.list_events(_require_active(state), limit=1000).get("items", [])
        plan = next(
            (
                event.get("payload", {}).get("plan_summary")
                for event in reversed(events)
                if event.get("event_type") == "agent.plan_updated"
            ),
            None,
        )
        console.print(
            Panel(
                str(plan or tr("The Agent has not published a plan yet.")),
                title=tr("Latest plan"),
                border_style="cyan",
            )
        )
        return False
    if command == "/context":
        if args:
            render_context(console, client.get_session_context(args[0]))
        else:
            render_context(console, client.get_run_context(_require_active(state)))
        return False
    if command == "/pause":
        client.pause_run(_require_active(state))
        console.print(f"[yellow]{tr('Pause requested.')}[/yellow]")
        return False
    if command == "/continue":
        client.resume_run(_require_active(state))
        console.print(f"[green]{tr('Resume requested.')}[/green]")
        return False
    if command == "/cancel":
        client.cancel_run(_require_active(state))
        console.print(f"[yellow]{tr('Run cancellation requested.')}[/yellow]")
        return False
    if command == "/compact":
        max_history_items = int(args[0]) if args else 100
        if max_history_items < 1 or max_history_items > 10_000:
            raise ValueError("Usage: /compact [MAX_ITEMS between 1 and 10000]")
        client.compact_run(
            _require_active(state),
            max_history_items=max_history_items,
        )
        console.print(f"[green]{tr('Context compaction requested.')}[/green]")
        return False
    if command == "/web":
        path = f"/runs/{state.active_run_id}" if state.active_run_id else "/"
        url = f"{client.base_url}{path}"
        console.print(url)
        webbrowser.open(url)
        return False
    if command == "/watch":
        _watch(client, _require_active(state), console)
        return False
    if command == "/approvals":
        render_approvals(
            console,
            client.list_approvals(_require_active(state)).get("items", []),
        )
        return False
    if command == "/terminal":
        terminal = client.create_terminal(_require_active(state), argv=args or None)
        state.active_terminal_id = str(terminal["id"])
        render_terminal(console, terminal)
        return False
    if command in {"/attach", "/takeover"}:
        session_id = args[0] if args else _require_active_terminal(state)
        state.active_terminal_id = session_id
        attach_terminal(client, session_id, console)
        return False
    if command == "/release":
        session_id = args[0] if args else _require_active_terminal(state)
        render_terminal(console, control_terminal(client, session_id, "release"))
        return False
    if command == "/approve":
        if not args:
            raise ValueError("Usage: /approve APPROVAL_ID [--for-run]")
        client.approve(args[0], approve_for_run="--for-run" in args[1:])
        console.print(f"[green]{tr('Approval saved and workflow signaled.')}[/green]")
        return False
    if command == "/reject":
        if not args:
            raise ValueError("Usage: /reject APPROVAL_ID [REASON]")
        client.reject(args[0], reason=" ".join(args[1:]).strip() or None)
        console.print(f"[yellow]{tr('Approval rejected and workflow signaled.')}[/yellow]")
        return False
    raise ValueError(f"Unknown command {command!r}; use /help")


def _handle_message(
    text: str,
    state: InteractiveState,
    client: APIClient,
    console: Console,
) -> None:
    if state.active_run_id is None:
        created = client.create_run(_new_run_payload(text, state))
        state.active_run_id = str(created["id"])
        render_run(console, created)
        return
    client.append_message(state.active_run_id, text)
    console.print(f"[green]{tr('Message queued.')}[/green]")


def _watch(client: APIClient, run_id: str, console: Console) -> None:
    console.print(
        f"[dim]{tr('Streaming events for {run_id}; press Ctrl+C to stop.', run_id=run_id)}[/dim]"
    )
    try:
        for event in client.stream_events(run_id):
            if isinstance(event.data, dict):
                event_type = event.data.get("event_type", event.event or "event")
                sequence = event.data.get("sequence", event.id or "?")
                console.print(f"[dim]#{sequence}[/dim] [cyan]{event_type}[/cyan]")
                payload = event.data.get("payload")
                if payload:
                    console.print(payload)
    except KeyboardInterrupt:
        console.print(f"[dim]{tr('Stopped watching.')}[/dim]")


def _new_run_payload(objective: str, state: InteractiveState) -> dict[str, object]:
    payload: dict[str, object] = {
        "objective": objective,
        "node_id": state.node_id,
        "approval_mode": state.approval_mode,
    }
    if state.model_profile is not None:
        payload["model_profile"] = state.model_profile
    return payload


def _require_active(state: InteractiveState) -> str:
    if state.active_run_id is None:
        raise ValueError(tr("No active run; use /new OBJECTIVE or /resume RUN_ID"))
    return state.active_run_id


def _require_active_terminal(state: InteractiveState) -> str:
    if state.active_terminal_id is None:
        raise ValueError(tr("No active terminal; use /terminal or /attach SESSION_ID"))
    return state.active_terminal_id


def _prompt(state: InteractiveState) -> str:
    if state.active_run_id is None:
        return "RiftX > "
    return f"RiftX [{state.active_run_id[:8]}] > "
