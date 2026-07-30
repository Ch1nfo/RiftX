"""Typer command-line interface for the RiftX control plane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
import typer
import uvicorn
from rich.console import Console

from riftx.api import APISettings, create_app
from riftx.config import RiftXConfig, RiftXConfigError, load_riftx_config
from riftx.domain import ApprovalMode, EntryPointKind, RunStatus, TerminalOwner

from .client import APIClient, RiftXAPIError
from .interactive import run_interactive
from .render import (
    render_approvals,
    render_artifact,
    render_artifacts,
    render_error,
    render_event,
    render_node,
    render_nodes,
    render_report,
    render_reports,
    render_run,
    render_runs,
    render_terminal,
    render_tools,
)
from .terminal import attach_terminal

console = Console()
app = typer.Typer(
    name="riftx",
    help="Host-native durable agent execution platform.",
    no_args_is_help=False,
    invoke_without_command=True,
    rich_markup_mode="rich",
)
run_app = typer.Typer(help="Create, inspect, and control Runs.")
nodes_app = typer.Typer(help="Register and inspect execution nodes.")
tools_app = typer.Typer(help="Inspect the node-local Tool Registry.")
terminal_app = typer.Typer(help="Create and control interactive terminal sessions.")
artifact_app = typer.Typer(help="Register and inspect immutable Run artifacts.")
report_app = typer.Typer(help="Generate and inspect structured Run reports.")
app.add_typer(run_app, name="run")
app.add_typer(nodes_app, name="node")
app.add_typer(tools_app, name="tools")
app.add_typer(terminal_app, name="terminal")
app.add_typer(artifact_app, name="artifact")
app.add_typer(report_app, name="report")


@dataclass(frozen=True, slots=True)
class CLIState:
    api_url: str
    config: RiftXConfig
    config_path: Path | None


@app.callback()
def main(
    context: typer.Context,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            envvar="RIFTX_API_URL",
            help="RiftX Control Plane base URL.",
        ),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            envvar="RIFTX_CONFIG",
            help="Explicit RiftX YAML configuration file.",
        ),
    ] = None,
) -> None:
    """Run a command, or enter interactive mode when no command is given."""

    try:
        config = load_riftx_config(explicit_path=config_path)
    except RiftXConfigError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    resolved_api_url = api_url or f"http://{config.server.host}:{config.server.port}"
    context.obj = CLIState(
        api_url=resolved_api_url,
        config=config,
        config_path=config_path,
    )
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
    context: typer.Context,
    host: Annotated[str | None, typer.Option(help="Listen address override.")] = None,
    port: Annotated[
        int | None,
        typer.Option(min=1, max=65535, help="Listen port override."),
    ] = None,
    reload: Annotated[bool, typer.Option(help="Enable development auto-reload.")] = False,
) -> None:
    """Start the shared FastAPI Control Plane."""

    state = _state(context)
    server = state.config.server.model_copy(
        update={
            **({"host": host} if host is not None else {}),
            **({"port": port} if port is not None else {}),
        }
    )
    config = state.config.model_copy(update={"server": server})
    settings = APISettings.from_config(config)
    uvicorn.run(
        create_app(settings=settings),
        host=server.host,
        port=server.port,
        reload=reload,
        log_level="info",
    )


@app.command("approvals")
def list_approvals(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
) -> None:
    """List durable approval requests for a Run."""

    _run_with_client(
        context,
        lambda client: render_approvals(
            console,
            client.list_approvals(run_id).get("items", []),
        ),
    )


@app.command("approve")
def approve(
    context: typer.Context,
    approval_id: Annotated[str, typer.Argument(help="Approval ID.")],
    for_run: Annotated[
        bool,
        typer.Option("--for-run", help="Approve this Tool for the rest of the Run."),
    ] = False,
) -> None:
    """Approve a paused Tool call once or for the rest of its Run."""

    _run_with_client(
        context,
        lambda client: client.approve(approval_id, approve_for_run=for_run),
    )
    console.print("[green]Approval saved and workflow signaled.[/green]")


@app.command("reject")
def reject(
    context: typer.Context,
    approval_id: Annotated[str, typer.Argument(help="Approval ID.")],
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Reason returned to the durable Agent."),
    ] = None,
) -> None:
    """Reject a paused Tool call."""

    _run_with_client(
        context,
        lambda client: client.reject(approval_id, reason=reason),
    )
    console.print("[yellow]Approval rejected and workflow signaled.[/yellow]")


@artifact_app.command("register")
def register_artifact(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    source_path: Annotated[
        str,
        typer.Argument(help="Path visible to the Control Plane host."),
    ],
    name: Annotated[str | None, typer.Option(help="Artifact display/file name.")] = None,
    mime_type: Annotated[str | None, typer.Option("--mime-type")] = None,
    description: Annotated[str, typer.Option(help="Evidence description.")] = "",
    execution_id: Annotated[str | None, typer.Option("--execution-id")] = None,
) -> None:
    """Snapshot a Run-owned file into immutable artifact storage."""

    _run_with_client(
        context,
        lambda client: render_artifact(
            console,
            client.register_artifact(
                run_id,
                source_path,
                name=name,
                mime_type=mime_type,
                description=description,
                execution_id=execution_id,
            ),
        ),
    )


@artifact_app.command("list")
def list_artifacts(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    execution_id: Annotated[str | None, typer.Option("--execution-id")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List immutable artifacts registered for a Run."""

    _run_with_client(
        context,
        lambda client: render_artifacts(
            console,
            client.list_artifacts(
                run_id,
                execution_id=execution_id,
                limit=limit,
                offset=offset,
            ).get("items", []),
        ),
    )


@artifact_app.command("show")
def show_artifact(
    context: typer.Context,
    artifact_id: Annotated[str, typer.Argument(help="Artifact ID.")],
) -> None:
    """Show immutable Artifact metadata and content URL."""

    _run_with_client(
        context,
        lambda client: render_artifact(console, client.get_artifact(artifact_id)),
    )


@report_app.command("generate")
def generate_reports(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    formats: Annotated[
        list[str] | None,
        typer.Option("--format", help="Output format; repeat for multiple formats."),
    ] = None,
) -> None:
    """Generate immutable Markdown, HTML, and/or JSON reports."""

    _run_with_client(
        context,
        lambda client: render_reports(
            console,
            client.generate_reports(run_id, formats=formats).get("items", []),
        ),
    )


@report_app.command("list")
def list_reports(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    report_format: Annotated[str | None, typer.Option("--format")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List generated reports for a Run."""

    _run_with_client(
        context,
        lambda client: render_reports(
            console,
            client.list_reports(
                run_id,
                format=report_format,
                limit=limit,
                offset=offset,
            ).get("items", []),
        ),
    )


@report_app.command("show")
def show_report(
    context: typer.Context,
    report_id: Annotated[str, typer.Argument(help="Report ID.")],
) -> None:
    """Show generated Report metadata and its immutable content URL."""

    _run_with_client(
        context,
        lambda client: render_report(console, client.get_report(report_id)),
    )


@terminal_app.command("create")
def create_terminal(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    command: Annotated[
        list[str] | None,
        typer.Argument(
            help=(
                "Command and arguments (use -- before command options); omit for the default shell."
            )
        ),
    ] = None,
    cwd: Annotated[str | None, typer.Option("--cwd", help="Terminal working directory.")] = None,
    cols: Annotated[int, typer.Option(min=1, max=1000)] = 120,
    rows: Annotated[int, typer.Option(min=1, max=1000)] = 40,
    owner: Annotated[
        TerminalOwner,
        typer.Option(case_sensitive=False, help="Initial terminal owner."),
    ] = TerminalOwner.AGENT,
) -> None:
    """Start a host-native terminal through the Control Plane."""

    _run_with_client(
        context,
        lambda client: render_terminal(
            console,
            client.create_terminal(
                run_id,
                argv=command,
                cwd=cwd,
                cols=cols,
                rows=rows,
                owner=owner.value,
            ),
        ),
    )


@terminal_app.command("show")
def show_terminal(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Terminal session ID.")],
) -> None:
    """Show durable terminal metadata."""

    _run_with_client(
        context,
        lambda client: render_terminal(console, client.get_terminal(session_id)),
    )


@terminal_app.command("close")
def close_terminal(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Terminal session ID.")],
) -> None:
    """Close a terminal session and its native process group."""

    _run_with_client(
        context,
        lambda client: render_terminal(console, client.close_terminal(session_id)),
    )


@app.command("attach")
def attach(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Terminal session ID.")],
    read_only: Annotated[
        bool,
        typer.Option("--read-only", help="Observe output without taking terminal ownership."),
    ] = False,
    cursor: Annotated[int, typer.Option(min=0, help="Transcript byte cursor.")] = 0,
) -> None:
    """Attach the local TTY; press Ctrl+] to release and detach."""

    state = _state(context)
    try:
        with APIClient(state.api_url) as client:
            console.print(f"[dim]Attaching to {session_id}; press Ctrl+] to detach.[/dim]")
            attach_terminal(
                client,
                session_id,
                console,
                take_over=not read_only,
                cursor=cursor,
            )
    except (RiftXAPIError, httpx.HTTPError, OSError, ValueError) as exc:
        render_error(console, exc)
        raise typer.Exit(1) from exc


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


@nodes_app.command("list")
def list_nodes(
    context: typer.Context,
    status: Annotated[str | None, typer.Option("--status")] = None,
) -> None:
    """List registered local and remote execution nodes."""

    _run_with_client(
        context,
        lambda client: render_nodes(
            console,
            client.list_nodes(status=status).get("items", []),
        ),
    )


@nodes_app.command("show")
def show_node(
    context: typer.Context,
    node_id: Annotated[str, typer.Argument(help="Node ID.")],
) -> None:
    """Show durable metadata for one execution node."""

    _run_with_client(context, lambda client: render_node(console, client.get_node(node_id)))


@nodes_app.command("disconnect")
def disconnect_node(
    context: typer.Context,
    node_id: Annotated[str, typer.Argument(help="Node ID.")],
) -> None:
    """Mark an execution node offline."""

    _run_with_client(
        context,
        lambda client: render_node(console, client.disconnect_node(node_id)),
    )


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
