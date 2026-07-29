"""Typer command-line interface for the RiftX control plane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import httpx
import typer
import uvicorn
from rich.console import Console

from riftx.domain import ApprovalMode, EntryPointKind, RunStatus

from .client import APIClient, RiftXAPIError
from .interactive import run_interactive
from .render import render_error, render_event, render_run, render_runs, render_tools

console = Console()
app = typer.Typer(
    name="riftx",
    help="Host-native durable agent execution platform.",
    no_args_is_help=False,
    invoke_without_command=True,
    rich_markup_mode="rich",
)
run_app = typer.Typer(help="Create, inspect, and control Runs.")
tools_app = typer.Typer(help="Inspect the node-local Tool Registry.")
app.add_typer(run_app, name="run")
app.add_typer(tools_app, name="tools")


@dataclass(frozen=True, slots=True)
class CLIState:
    api_url: str


@app.callback()
def main(
    context: typer.Context,
    api_url: Annotated[
        str,
        typer.Option(
            "--api-url",
            envvar="RIFTX_API_URL",
            help="RiftX Control Plane base URL.",
        ),
    ] = "http://127.0.0.1:8787",
) -> None:
    """Run a command, or enter interactive mode when no command is given."""

    context.obj = CLIState(api_url=api_url)
    if context.invoked_subcommand is None:
        with APIClient(api_url) as client:
            run_interactive(client, console)


@app.command()
def interactive(context: typer.Context) -> None:
    """Enter the interactive RiftX session explicitly."""

    state = _state(context)
    with APIClient(state.api_url) as client:
        run_interactive(client, console)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Listen address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Listen port.")] = 8787,
    reload: Annotated[bool, typer.Option(help="Enable development auto-reload.")] = False,
) -> None:
    """Start the shared FastAPI Control Plane."""

    uvicorn.run(
        "riftx.api.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@run_app.command("create")
def create_run(
    context: typer.Context,
    objective: Annotated[str, typer.Argument(help="Run objective.")],
    engagement_name: Annotated[
        str | None,
        typer.Option("--engagement", help="Create a named Engagement for this Run."),
    ] = None,
    node_id: Annotated[str | None, typer.Option("--node", help="Execution node ID.")] = None,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Workspace path visible to the worker."),
    ] = None,
    approval_mode: Annotated[
        ApprovalMode,
        typer.Option("--mode", case_sensitive=False, help="Approval mode."),
    ] = ApprovalMode.BALANCED,
    success: Annotated[
        list[str] | None,
        typer.Option("--success", help="Repeatable success criterion."),
    ] = None,
    entry: Annotated[
        list[str] | None,
        typer.Option("--entry", help="Repeatable entry point as KIND=VALUE."),
    ] = None,
) -> None:
    """Create a durable Run and start its Temporal workflow."""

    payload: dict[str, object] = {
        "objective": objective,
        "approval_mode": approval_mode.value,
        "success_criteria": [{"description": item, "required": True} for item in (success or [])],
        "entry_points": [_parse_entry_point(item) for item in (entry or [])],
    }
    if engagement_name:
        payload["engagement"] = {"name": engagement_name}
    if node_id:
        payload["node_id"] = node_id
    if workspace:
        payload["workspace_path"] = workspace
    _run_with_client(context, lambda client: render_run(console, client.create_run(payload)))


@run_app.command("list")
def list_runs(
    context: typer.Context,
    run_status: Annotated[
        RunStatus | None,
        typer.Option("--status", case_sensitive=False, help="Filter by Run status."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List persisted Runs."""

    def operation(client: APIClient) -> None:
        payload = client.list_runs(
            status=run_status.value if run_status else None,
            limit=limit,
            offset=offset,
        )
        render_runs(console, payload.get("items", []))

    _run_with_client(context, operation)


@run_app.command("show")
def show_run(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
) -> None:
    """Show one persisted Run."""

    _run_with_client(context, lambda client: render_run(console, client.get_run(run_id)))


@run_app.command("pause")
def pause_run(context: typer.Context, run_id: str) -> None:
    """Request that a Run pause at a durable workflow boundary."""

    _run_with_client(context, lambda client: client.pause_run(run_id))
    console.print("[yellow]Pause requested.[/yellow]")


@run_app.command("resume")
def resume_run(context: typer.Context, run_id: str) -> None:
    """Resume a paused Run."""

    _run_with_client(context, lambda client: client.resume_run(run_id))
    console.print("[green]Resume requested.[/green]")


@run_app.command("cancel-current")
def cancel_current(context: typer.Context, run_id: str) -> None:
    """Cancel only the Run's current active execution."""

    _run_with_client(context, lambda client: client.cancel_current_execution(run_id))
    console.print("[yellow]Current execution cancellation requested.[/yellow]")


@run_app.command("message")
def send_message(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    message: Annotated[str, typer.Argument(help="Message to append to the Agent session.")],
) -> None:
    """Queue a user message through the durable workflow."""

    _run_with_client(context, lambda client: client.append_message(run_id, message))
    console.print("[green]Message queued.[/green]")


@run_app.command("watch")
def watch_run(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    after: Annotated[
        str | None,
        typer.Option("--after", help="Resume after this SSE event ID."),
    ] = None,
) -> None:
    """Stream the Run timeline over SSE until interrupted."""

    state = _state(context)
    try:
        with APIClient(state.api_url) as client:
            for event in client.stream_events(run_id, last_event_id=after):
                render_event(console, event.data)
    except KeyboardInterrupt:
        console.print("[dim]Stopped watching.[/dim]")
    except (RiftXAPIError, httpx.HTTPError) as exc:
        render_error(console, exc)
        raise typer.Exit(1) from exc


@tools_app.command("list")
def list_tools(
    context: typer.Context,
    node_id: Annotated[str, typer.Option("--node")] = "local",
) -> None:
    """List configured tools and their probed availability."""

    _run_with_client(
        context,
        lambda client: render_tools(console, client.list_tools(node_id)),
    )


@tools_app.command("reload")
def reload_tools(
    context: typer.Context,
    node_id: Annotated[str, typer.Option("--node")] = "local",
) -> None:
    """Reload tools.yaml and re-run availability probes."""

    _run_with_client(
        context,
        lambda client: render_tools(console, client.refresh_tools(node_id)),
    )


@tools_app.command("doctor")
def doctor_tools(
    context: typer.Context,
    node_id: Annotated[str, typer.Option("--node")] = "local",
) -> None:
    """Refresh the registry and fail when enabled tools are unavailable."""

    unhealthy = False

    def operation(client: APIClient) -> None:
        nonlocal unhealthy
        payload = client.refresh_tools(node_id)
        render_tools(console, payload)
        unhealthy = any(
            item.get("definition", {}).get("enabled", False)
            and item.get("state", {}).get("availability") != "available"
            for item in payload.get("tools", [])
        )

    _run_with_client(context, operation)
    if unhealthy:
        raise typer.Exit(1)


def _run_with_client(
    context: typer.Context,
    operation: Callable[[APIClient], object],
) -> None:
    state = _state(context)
    try:
        with APIClient(state.api_url) as client:
            operation(client)
    except (RiftXAPIError, httpx.HTTPError) as exc:
        render_error(console, exc)
        raise typer.Exit(1) from exc


def _state(context: typer.Context) -> CLIState:
    state = context.find_root().obj
    if not isinstance(state, CLIState):
        raise RuntimeError("CLI state was not initialized")
    return state


def _parse_entry_point(value: str) -> dict[str, str]:
    kind, separator, entry_value = value.partition("=")
    if not separator or not entry_value.strip():
        raise typer.BadParameter("entry points must use KIND=VALUE")
    try:
        parsed_kind = EntryPointKind(kind.strip().lower())
    except ValueError as exc:
        choices = ", ".join(item.value for item in EntryPointKind)
        raise typer.BadParameter(f"entry point kind must be one of: {choices}") from exc
    return {"kind": parsed_kind.value, "value": entry_value.strip()}


if __name__ == "__main__":
    app()
