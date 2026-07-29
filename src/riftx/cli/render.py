"""Rich renderers for control-plane data."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def render_runs(console: Console, runs: Iterable[dict[str, Any]]) -> None:
    table = Table(title="RiftX Runs", expand=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Objective")
    table.add_column("Node")
    table.add_column("Created")
    count = 0
    for run in runs:
        count += 1
        objective = run.get("objective", {})
        description = (
            objective.get("description", "") if isinstance(objective, dict) else str(objective)
        )
        table.add_row(
            str(run.get("id", "")),
            _status_text(str(run.get("status", "unknown"))),
            str(description),
            str(run.get("node_id", "")),
            str(run.get("created_at", "")),
        )
    if count:
        console.print(table)
    else:
        console.print("[dim]No runs found.[/dim]")


def render_run(console: Console, run: dict[str, Any]) -> None:
    objective = run.get("objective", {})
    description = (
        objective.get("description", "") if isinstance(objective, dict) else str(objective)
    )
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold")
    body.add_column()
    body.add_row("ID", str(run.get("id", "")))
    body.add_row("Status", _status_text(str(run.get("status", "unknown"))))
    body.add_row("Objective", str(description))
    body.add_row("Node", str(run.get("node_id", "")))
    body.add_row("Approval", str(run.get("approval_mode", "")))
    body.add_row("Workspace", str(run.get("workspace_path", "")))
    body.add_row("Workflow", str(run.get("temporal_workflow_id", "")))
    console.print(Panel(body, title="RiftX Run", border_style="cyan"))


def render_tools(console: Console, payload: dict[str, Any]) -> None:
    table = Table(
        title=(
            f"Tools on {payload.get('node_id', 'unknown')} "
            f"(generation {payload.get('generation', '?')})"
        ),
        expand=True,
    )
    table.add_column("Tool", style="cyan")
    table.add_column("Availability")
    table.add_column("Version")
    table.add_column("Executor")
    table.add_column("Capabilities")
    for item in payload.get("tools", []):
        definition = item.get("definition", {})
        state = item.get("state", {})
        table.add_row(
            str(definition.get("id", "")),
            _availability_text(str(state.get("availability", "unknown"))),
            str(state.get("version") or "—"),
            str(definition.get("executor", "")),
            ", ".join(definition.get("capabilities", [])),
        )
    console.print(table)


def render_terminal(console: Console, terminal: dict[str, Any]) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold", no_wrap=True)
    body.add_column(overflow="fold")
    body.add_row("Session", str(terminal.get("id", "")))
    body.add_row("Run", str(terminal.get("run_id", "")))
    body.add_row("Status", _status_text(str(terminal.get("status", "unknown"))))
    body.add_row("Owner", str(terminal.get("owner", "")))
    body.add_row("Command", " ".join(str(item) for item in terminal.get("argv", [])))
    body.add_row("Working dir", str(terminal.get("cwd", "")))
    body.add_row("Size", f"{terminal.get('cols', '?')} × {terminal.get('rows', '?')}")
    body.add_row("PID", str(terminal.get("pid") or "—"))
    if terminal.get("exit_code") is not None:
        body.add_row("Exit code", str(terminal["exit_code"]))
    console.print(Panel(body, title="Terminal", border_style="cyan"))


def render_approvals(console: Console, approvals: Iterable[dict[str, Any]]) -> None:
    items = list(approvals)
    if not items:
        console.print("[dim]No approvals found.[/dim]")
        return
    for approval in items:
        body = Table.grid(padding=(0, 2))
        body.add_column(style="bold", no_wrap=True)
        body.add_column(overflow="fold")
        command = approval.get("command", [])
        env_diff = approval.get("env_diff", {})
        body.add_row("ID", str(approval.get("id", "")))
        body.add_row("Status", _status_text(str(approval.get("status", "unknown"))))
        body.add_row("Tool", str(approval.get("tool_name", "")))
        body.add_row("Command", " ".join(str(item) for item in command))
        body.add_row("Working dir", str(approval.get("cwd", "")))
        body.add_row("Target", str(approval.get("target_summary", "")))
        body.add_row("Environment", JSON.from_data(env_diff) if env_diff else "—")
        body.add_row("Reason", str(approval.get("reason", "")))
        if approval.get("decided_by"):
            body.add_row("Decided by", str(approval["decided_by"]))
        console.print(Panel(body, title="Approval", border_style="yellow"))


def render_artifacts(console: Console, artifacts: Iterable[dict[str, Any]]) -> None:
    items = list(artifacts)
    if not items:
        console.print("[dim]No artifacts found.[/dim]")
        return
    table = Table(title="Run Artifacts", expand=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("MIME")
    table.add_column("Size", justify="right")
    table.add_column("SHA-256", overflow="ellipsis")
    table.add_column("Execution")
    for artifact in items:
        table.add_row(
            str(artifact.get("id", "")),
            str(artifact.get("name", "")),
            str(artifact.get("mime_type", "")),
            str(artifact.get("size", 0)),
            str(artifact.get("sha256", "")),
            str(artifact.get("execution_id") or "—"),
        )
    console.print(table)


def render_artifact(console: Console, artifact: dict[str, Any]) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold", no_wrap=True)
    body.add_column(overflow="fold")
    body.add_row("ID", str(artifact.get("id", "")))
    body.add_row("Run", str(artifact.get("run_id", "")))
    body.add_row("Name", str(artifact.get("name", "")))
    body.add_row("MIME", str(artifact.get("mime_type", "")))
    body.add_row("Size", str(artifact.get("size", 0)))
    body.add_row("SHA-256", str(artifact.get("sha256", "")))
    body.add_row("Execution", str(artifact.get("execution_id") or "—"))
    body.add_row("Description", str(artifact.get("description", "")) or "—")
    body.add_row("Content", str(artifact.get("content_url", "")))
    console.print(Panel(body, title="Artifact", border_style="cyan"))


def render_reports(console: Console, reports: Iterable[dict[str, Any]]) -> None:
    items = list(reports)
    if not items:
        console.print("[dim]No reports found.[/dim]")
        return
    table = Table(title="Run Reports", expand=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Format")
    table.add_column("Artifact")
    table.add_column("Findings", justify="right")
    table.add_column("Created")
    for report in items:
        table.add_row(
            str(report.get("id", "")),
            str(report.get("format", "")),
            str(report.get("artifact_id", "")),
            str(len(report.get("finding_ids", []))),
            str(report.get("created_at", "")),
        )
    console.print(table)


def render_report(console: Console, report: dict[str, Any]) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold", no_wrap=True)
    body.add_column(overflow="fold")
    body.add_row("ID", str(report.get("id", "")))
    body.add_row("Run", str(report.get("run_id", "")))
    body.add_row("Format", str(report.get("format", "")))
    body.add_row("Artifact", str(report.get("artifact_id", "")))
    body.add_row("Findings", ", ".join(str(item) for item in report.get("finding_ids", [])) or "—")
    body.add_row("Content", str(report.get("content_url", "")))
    body.add_row("Created", str(report.get("created_at", "")))
    console.print(Panel(body, title="Report", border_style="green"))


def render_event(console: Console, event: object) -> None:
    if not isinstance(event, dict):
        console.print(event)
        return
    sequence = event.get("sequence", "?")
    event_type = event.get("event_type", "event")
    payload = event.get("payload", {})
    console.print(f"[dim]#{sequence}[/dim] [bold cyan]{event_type}[/bold cyan]")
    if payload:
        console.print(JSON.from_data(payload), soft_wrap=True)


def render_error(console: Console, error: Exception) -> None:
    from .client import RiftXAPIError

    if isinstance(error, RiftXAPIError):
        console.print(
            Panel(
                f"[bold]{error.message}[/bold]\n[dim]code={error.code} "
                f"status={error.status_code}[/dim]",
                title="RiftX API error",
                border_style="red",
            )
        )
    else:
        console.print(Panel(str(error), title="Error", border_style="red"))


def _status_text(status: str) -> Text:
    colors = {
        "running": "green",
        "completed": "bright_green",
        "waiting_approval": "yellow",
        "paused": "yellow",
        "failed": "red",
        "cancelled": "red",
        "created": "blue",
        "preparing": "blue",
    }
    return Text(status, style=colors.get(status, "white"))


def _availability_text(availability: str) -> Text:
    colors = {
        "available": "green",
        "unavailable": "red",
        "misconfigured": "yellow",
        "disabled": "dim",
    }
    return Text(availability, style=colors.get(availability, "white"))
