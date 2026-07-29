"""Claude-Code-like interactive client backed exclusively by the RiftX API."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.panel import Panel

from .client import APIClient, RiftXAPIError
from .render import (
    render_approvals,
    render_error,
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
    "/pause",
    "/continue",
    "/cancel",
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


def run_interactive(client: APIClient, console: Console) -> None:
    state = InteractiveState()
    session: PromptSession[str] = PromptSession(
        history=InMemoryHistory(),
        completer=WordCompleter(_COMMANDS, sentence=True),
        complete_while_typing=False,
    )
    console.print(
        Panel(
            "Type an objective to create a Run, or use [bold]/help[/bold].",
            title="RiftX Interactive",
            border_style="cyan",
        )
    )

    while True:
        try:
            with patch_stdout():
                text = session.prompt(_prompt(state)).strip()
        except EOFError:
            console.print("[dim]Session closed.[/dim]")
            return
        except KeyboardInterrupt:
            console.print("[dim]Use /exit to leave RiftX.[/dim]")
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
        console.print(
            "[bold]/new OBJECTIVE[/bold] create a run\n"
            "[bold]/resume RUN_ID[/bold] select an existing run\n"
            "[bold]/runs[/bold] list runs\n"
            "[bold]/status[/bold] show the active run\n"
            "[bold]/tools [NODE][/bold] list tools\n"
            "[bold]/pause[/bold], [bold]/continue[/bold], "
            "[bold]/cancel[/bold] control the active run\n"
            "[bold]/watch[/bold] stream active run events\n"
            "[bold]/approvals[/bold] list approval requests\n"
            "[bold]/terminal [COMMAND ...][/bold] start a terminal for the active run\n"
            "[bold]/attach [SESSION_ID][/bold] take over and attach (Ctrl+] detaches)\n"
            "[bold]/release [SESSION_ID][/bold] return terminal ownership to the Agent\n"
            "[bold]/approve APPROVAL_ID [--for-run][/bold] approve a tool call\n"
            "[bold]/reject APPROVAL_ID [REASON][/bold] reject a tool call\n"
            "[bold]/exit[/bold] close interactive mode"
        )
        return False
    if command == "/new":
        objective = " ".join(args).strip()
        if not objective:
            raise ValueError("Usage: /new OBJECTIVE")
        created = client.create_run({"objective": objective})
        state.active_run_id = str(created["id"])
        render_run(console, created)
        return False
    if command == "/resume":
        if not args:
            raise ValueError("Usage: /resume RUN_ID")
        selected = client.get_run(args[0])
        state.active_run_id = str(selected["id"])
        render_run(console, selected)
        return False
    if command == "/runs":
        render_runs(console, client.list_runs().get("items", []))
        return False
    if command == "/status":
        render_run(console, client.get_run(_require_active(state)))
        return False
    if command == "/tools":
        render_tools(console, client.list_tools(args[0] if args else "local"))
        return False
    if command == "/pause":
        client.pause_run(_require_active(state))
        console.print("[yellow]Pause requested.[/yellow]")
        return False
    if command == "/continue":
        client.resume_run(_require_active(state))
        console.print("[green]Resume requested.[/green]")
        return False
    if command == "/cancel":
        client.cancel_current_execution(_require_active(state))
        console.print("[yellow]Current execution cancellation requested.[/yellow]")
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
        console.print("[green]Approval saved and workflow signaled.[/green]")
        return False
    if command == "/reject":
        if not args:
            raise ValueError("Usage: /reject APPROVAL_ID [REASON]")
        client.reject(args[0], reason=" ".join(args[1:]).strip() or None)
        console.print("[yellow]Approval rejected and workflow signaled.[/yellow]")
        return False
    raise ValueError(f"Unknown command {command!r}; use /help")


def _handle_message(
    text: str,
    state: InteractiveState,
    client: APIClient,
    console: Console,
) -> None:
    if state.active_run_id is None:
        created = client.create_run({"objective": text})
        state.active_run_id = str(created["id"])
        render_run(console, created)
        return
    client.append_message(state.active_run_id, text)
    console.print("[green]Message queued.[/green]")


def _watch(client: APIClient, run_id: str, console: Console) -> None:
    console.print(f"[dim]Streaming events for {run_id}; press Ctrl+C to stop.[/dim]")
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
        console.print("[dim]Stopped watching.[/dim]")


def _require_active(state: InteractiveState) -> str:
    if state.active_run_id is None:
        raise ValueError("No active run; use /new OBJECTIVE or /resume RUN_ID")
    return state.active_run_id


def _require_active_terminal(state: InteractiveState) -> str:
    if state.active_terminal_id is None:
        raise ValueError("No active terminal; use /terminal or /attach SESSION_ID")
    return state.active_terminal_id


def _prompt(state: InteractiveState) -> str:
    if state.active_run_id is None:
        return "RiftX > "
    return f"RiftX [{state.active_run_id[:8]}] > "
