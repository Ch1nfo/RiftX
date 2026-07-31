"""Rich renderers for control-plane data."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

from rich.console import Console, Group
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .i18n import tr

_CONTEXT_CATEGORY_LABELS = {
    "runtime_contract": "Runtime Contract",
    "stable_instructions": "Stable Instructions",
    "run_contract": "Run Contract",
    "working_memory": "Working Memory",
    "conversation": "Conversation",
    "tool_results": "Tool Results",
    "retrieved_memory": "Retrieved Memory",
    "subagent_results": "Subagent Results",
    "tool_schemas": "Tool Schemas",
}

_STOP_RESOURCE_TYPES = (
    ("executions", "Execution"),
    ("browser_sessions", "Browser session"),
    ("target_http_requests", "Target HTTP request"),
)


class _StopResourceRow(NamedTuple):
    resource_type: str
    resource_id: str
    node_id: str | None
    observed_status: str | None
    confirmed_status: str | None
    failure: str | None
    confirmed: bool


def render_nodes(console: Console, nodes: Iterable[dict[str, Any]]) -> None:
    items = list(nodes)
    if not items:
        console.print(f"[dim]{tr('No execution nodes found.')}[/dim]")
        return
    table = Table(title=tr("Execution Nodes"), expand=True)
    table.add_column(tr("ID"), style="cyan", no_wrap=True)
    table.add_column(tr("Name"))
    table.add_column(tr("Status"))
    table.add_column(tr("Platform"))
    table.add_column(tr("Runner"))
    table.add_column(tr("Capabilities"))
    table.add_column(tr("Last seen"))
    for node in items:
        table.add_row(
            str(node.get("id", "")),
            str(node.get("name", "")),
            _status_text(str(node.get("status", "unknown"))),
            f"{node.get('platform', '')}/{node.get('architecture', '')}",
            str(node.get("runner_version", "unknown")),
            ", ".join(str(item) for item in node.get("capabilities", [])) or "—",
            str(node.get("last_seen_at") or "—"),
        )
    console.print(table)


def render_node(console: Console, node: dict[str, Any]) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold", no_wrap=True)
    body.add_column(overflow="fold")
    body.add_row(tr("ID"), str(node.get("id", "")))
    body.add_row(tr("Name"), str(node.get("name", "")))
    body.add_row(tr("Status"), tr(str(node.get("status", "unknown"))))
    body.add_row(
        tr("Platform"),
        f"{node.get('platform', '')}/{node.get('architecture', '')}",
    )
    body.add_row(tr("Runner"), str(node.get("runner_version", "unknown")))
    body.add_row(
        tr("Capabilities"),
        ", ".join(str(item) for item in node.get("capabilities", [])) or "—",
    )
    body.add_row(tr("Labels"), str(node.get("labels", {})))
    body.add_row(tr("Last seen"), str(node.get("last_seen_at") or "—"))
    console.print(Panel(body, title=tr("Execution Node"), border_style="cyan"))


def render_model_profiles(console: Console, payload: dict[str, Any]) -> None:
    profiles = payload.get("profiles", [])
    if not profiles:
        console.print(f"[dim]{tr('No model profiles configured.')}[/dim]")
        return
    table = Table(title=tr("Model Profiles"), expand=True)
    table.add_column(tr("Name"), style="cyan", no_wrap=True)
    table.add_column(tr("Provider"))
    table.add_column(tr("Request mode"))
    table.add_column(tr("Model"))
    table.add_column(tr("Base URL"))
    table.add_column(tr("Credential"))
    table.add_column(tr("Default"))
    for profile in profiles:
        table.add_row(
            str(profile.get("name", "")),
            str(profile.get("provider", "")),
            str(profile.get("request_mode", "")),
            str(profile.get("model", "")),
            str(profile.get("base_url") or "—"),
            tr("configured") if profile.get("api_key_configured") else tr("missing"),
            "*" if profile.get("is_effective_default") else "",
        )
    console.print(table)
    configured_default = payload.get("default_profile")
    effective_default = payload.get("effective_default_profile")
    if configured_default != effective_default:
        console.print(
            f"[dim]{tr('Configured default')}: {configured_default}; "
            f"{tr('effective default')}: {effective_default}[/dim]"
        )


def render_model_profile(console: Console, profile: dict[str, Any]) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold", no_wrap=True)
    body.add_column(overflow="fold")
    body.add_row(tr("Name"), str(profile.get("name", "")))
    body.add_row(tr("Provider"), str(profile.get("provider", "")))
    body.add_row(tr("Request mode"), str(profile.get("request_mode", "")))
    body.add_row(tr("Model"), str(profile.get("model", "")))
    body.add_row(tr("Base URL"), str(profile.get("base_url") or "—"))
    body.add_row(
        tr("Requires API key"),
        tr("yes") if profile.get("requires_api_key") else tr("no"),
    )
    body.add_row(tr("API key environment"), str(profile.get("api_key_env") or "—"))
    body.add_row(tr("Timeout (seconds)"), str(profile.get("timeout_seconds", "—")))
    body.add_row(tr("Max retries"), str(profile.get("max_retries", "—")))
    body.add_row(
        tr("Stored API key"),
        tr("yes") if profile.get("has_stored_api_key") else tr("no"),
    )
    body.add_row(
        tr("Credential status"),
        tr("configured") if profile.get("api_key_configured") else tr("missing"),
    )
    body.add_row(tr("Default"), tr("yes") if profile.get("is_default") else tr("no"))
    console.print(Panel(body, title=tr("Model Profile"), border_style="cyan"))


def render_context(console: Console, compilation: dict[str, Any]) -> None:
    manifest = compilation.get("manifest") or {}
    categories = manifest.get("categories") or {}
    table = Table(title=tr("Context Inspector"), expand=True)
    table.add_column(tr("Category"), style="cyan")
    table.add_column(tr("Items"), justify="right")
    table.add_column(tr("Characters"), justify="right")
    table.add_column(tr("Estimated tokens"), justify="right")
    for key, label in _CONTEXT_CATEGORY_LABELS.items():
        usage = categories.get(key) or {}
        table.add_row(
            tr(label),
            str(usage.get("item_count", 0)),
            str(usage.get("character_count", 0)),
            str(usage.get("estimated_tokens", 0)),
        )
    console.print(table)
    actual_input = compilation.get("actual_input_tokens")
    actual_output = compilation.get("actual_output_tokens")
    console.print(
        (
            f"{tr('Model')}: [cyan]{{}}[/cyan]  {tr('Estimated input')}: [bold]{{}}[/bold]  "
            f"{tr('Actual input/output')}: [bold]{{}}/{{}}[/bold]  "
            f"{tr('Compilation')}: [dim]{{}}[/dim]"
        ).format(
            compilation.get("model_profile", "unknown"),
            compilation.get("estimated_tokens", 0),
            actual_input if actual_input is not None else "—",
            actual_output if actual_output is not None else "—",
            compilation.get("id", ""),
        )
    )


def render_runs(console: Console, runs: Iterable[dict[str, Any]]) -> None:
    table = Table(title=tr("RiftX Runs"), expand=True)
    table.add_column(tr("ID"), style="cyan", no_wrap=True)
    table.add_column(tr("Status"))
    table.add_column(tr("Objective"))
    table.add_column(tr("Node"))
    table.add_column(tr("Created"))
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
        console.print(f"[dim]{tr('No runs found.')}[/dim]")


def render_run(console: Console, run: dict[str, Any]) -> None:
    objective = run.get("objective", {})
    description = (
        objective.get("description", "") if isinstance(objective, dict) else str(objective)
    )
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold")
    body.add_column()
    body.add_row(tr("ID"), str(run.get("id", "")))
    body.add_row(tr("Status"), _status_text(str(run.get("status", "unknown"))))
    body.add_row(tr("Objective"), str(description))
    body.add_row(tr("Node"), str(run.get("node_id", "")))
    body.add_row(tr("Approval"), str(run.get("approval_mode", "")))
    body.add_row(tr("Model"), tr(str(run.get("model_profile") or "default")))
    body.add_row(tr("Workspace"), str(run.get("workspace_path", "")))
    body.add_row(tr("Workflow"), str(run.get("temporal_workflow_id", "")))
    console.print(Panel(body, title=tr("RiftX Run"), border_style="cyan"))


def render_memories(console: Console, memories: Iterable[dict[str, Any]]) -> None:
    items = list(memories)
    if not items:
        console.print(f"[dim]{tr('No long-term memories found.')}[/dim]")
        return
    table = Table(title=tr("Long-Term Memory"), expand=True)
    table.add_column(tr("ID"), style="cyan", no_wrap=True)
    table.add_column(tr("Type"))
    table.add_column(tr("Scope"))
    table.add_column(tr("Status"))
    table.add_column(tr("Pin"))
    table.add_column(tr("Summary"))
    for memory in items:
        table.add_row(
            str(memory.get("id", "")),
            str(memory.get("memory_type", "")),
            f"{memory.get('scope_type', '')}:{memory.get('scope_id', '')}",
            _status_text(str(memory.get("status", "unknown"))),
            tr("yes") if memory.get("pinned") else tr("no"),
            str(memory.get("summary", "")),
        )
    console.print(table)


def render_memory(console: Console, memory: dict[str, Any]) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold", no_wrap=True)
    body.add_column(overflow="fold")
    body.add_row(tr("ID"), str(memory.get("id", "")))
    body.add_row(tr("Type"), str(memory.get("memory_type", "")))
    body.add_row(
        tr("Scope"),
        f"{memory.get('scope_type', '')}:{memory.get('scope_id', '')}",
    )
    body.add_row(tr("Status"), tr(str(memory.get("status", ""))))
    body.add_row(tr("Pinned"), tr("yes") if memory.get("pinned") else tr("no"))
    body.add_row(tr("Title"), str(memory.get("title", "")))
    body.add_row(tr("Summary"), str(memory.get("summary", "")))
    body.add_row(tr("Content"), str(memory.get("content", "")))
    body.add_row(tr("Sources"), "\n".join(memory.get("source_refs", [])) or "—")
    console.print(Panel(body, title=tr("Long-Term Memory"), border_style="cyan"))


def render_executions(console: Console, executions: Iterable[dict[str, Any]]) -> None:
    items = list(executions)
    if not items:
        console.print(f"[dim]{tr('No executions found.')}[/dim]")
        return
    table = Table(title=tr("Executions"), expand=True)
    table.add_column(tr("ID"), style="cyan", no_wrap=True)
    table.add_column(tr("Status"))
    table.add_column(tr("Session"))
    table.add_column(tr("Tool Call"))
    table.add_column(tr("Attempt"))
    table.add_column(tr("Command"))
    for execution in items:
        command = execution.get("command_text") or " ".join(execution.get("argv", []))
        table.add_row(
            str(execution.get("id", "")),
            _status_text(str(execution.get("status", "unknown"))),
            str(execution.get("session_id") or "—"),
            str(execution.get("tool_call_id") or "—"),
            str(execution.get("attempt_group") or "—"),
            str(command),
        )
    console.print(table)


def render_execution(console: Console, execution: dict[str, Any]) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold", no_wrap=True)
    body.add_column(overflow="fold")
    body.add_row(tr("ID"), str(execution.get("id", "")))
    body.add_row(tr("Status"), _status_text(str(execution.get("status", "unknown"))))
    body.add_row(tr("Run"), str(execution.get("run_id", "")))
    body.add_row(tr("Session"), str(execution.get("session_id") or "—"))
    body.add_row(tr("Tool Call"), str(execution.get("tool_call_id") or "—"))
    body.add_row(tr("Attempt"), str(execution.get("attempt_group") or "—"))
    body.add_row(tr("Node"), str(execution.get("node_id", "")))
    body.add_row(tr("PID"), str(execution.get("pid") or "—"))
    exit_code = execution.get("exit_code")
    body.add_row(tr("Exit code"), str(exit_code if exit_code is not None else "—"))
    body.add_row(tr("Execution key"), str(execution.get("execution_key", "")))
    console.print(Panel(body, title=tr("Execution"), border_style="cyan"))


def render_execution_wait(console: Console, result: dict[str, Any]) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold", no_wrap=True)
    body.add_column(overflow="fold")
    body.add_row(tr("Wait status"), _status_text(str(result.get("wait_status", "unknown"))))
    body.add_row(
        tr("Execution status"),
        _status_text(str(result.get("execution_status", "unknown"))),
    )
    body.add_row(tr("Execution"), str(result.get("execution_id", "")))
    next_poll = result.get("next_poll_after_seconds")
    body.add_row(tr("Next poll"), f"{next_poll}s" if next_poll is not None else "—")
    partial_output = result.get("partial_output")
    if partial_output:
        body.add_row(tr("Output"), str(partial_output))
    console.print(Panel(body, title=tr("Execution Wait"), border_style="cyan"))


def render_tools(console: Console, payload: dict[str, Any]) -> None:
    table = Table(
        title=tr(
            "Tools on {node} (generation {generation})",
            node=payload.get("node_id", "unknown"),
            generation=payload.get("generation", "?"),
        ),
        expand=True,
    )
    table.add_column(tr("Tool"), style="cyan")
    table.add_column(tr("Availability"))
    table.add_column(tr("Version"))
    table.add_column(tr("Executor"))
    table.add_column(tr("Capabilities"))
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
    body.add_row(tr("Session"), str(terminal.get("id", "")))
    body.add_row(tr("Run"), str(terminal.get("run_id", "")))
    body.add_row(tr("Status"), _status_text(str(terminal.get("status", "unknown"))))
    body.add_row(tr("Owner"), str(terminal.get("owner", "")))
    body.add_row(tr("Command"), " ".join(str(item) for item in terminal.get("argv", [])))
    body.add_row(tr("Working dir"), str(terminal.get("cwd", "")))
    body.add_row(tr("Size"), f"{terminal.get('cols', '?')} × {terminal.get('rows', '?')}")
    body.add_row(tr("PID"), str(terminal.get("pid") or "—"))
    if terminal.get("exit_code") is not None:
        body.add_row(tr("Exit code"), str(terminal["exit_code"]))
    console.print(Panel(body, title=tr("Terminal"), border_style="cyan"))


def render_approvals(console: Console, approvals: Iterable[dict[str, Any]]) -> None:
    items = list(approvals)
    if not items:
        console.print(f"[dim]{tr('No approvals found.')}[/dim]")
        return
    for approval in items:
        body = Table.grid(padding=(0, 2))
        body.add_column(style="bold", no_wrap=True)
        body.add_column(overflow="fold")
        command = approval.get("command", [])
        env_diff = approval.get("env_diff", {})
        body.add_row(tr("ID"), str(approval.get("id", "")))
        body.add_row(tr("Status"), _status_text(str(approval.get("status", "unknown"))))
        body.add_row(tr("Tool"), str(approval.get("tool_name", "")))
        body.add_row(tr("Command"), " ".join(str(item) for item in command))
        body.add_row(tr("Working dir"), str(approval.get("cwd", "")))
        body.add_row(tr("Target"), str(approval.get("target_summary", "")))
        body.add_row(tr("Environment"), JSON.from_data(env_diff) if env_diff else "—")
        body.add_row(tr("Reason"), str(approval.get("reason", "")))
        if approval.get("decided_by"):
            body.add_row(tr("Decided by"), str(approval["decided_by"]))
        console.print(Panel(body, title=tr("Approval"), border_style="yellow"))


def render_artifacts(console: Console, artifacts: Iterable[dict[str, Any]]) -> None:
    items = list(artifacts)
    if not items:
        console.print(f"[dim]{tr('No artifacts found.')}[/dim]")
        return
    table = Table(title=tr("Run Artifacts"), expand=True)
    table.add_column(tr("ID"), style="cyan", no_wrap=True)
    table.add_column(tr("Name"))
    table.add_column(tr("MIME"))
    table.add_column(tr("Size"), justify="right")
    table.add_column(tr("SHA-256"), overflow="ellipsis")
    table.add_column(tr("Execution"))
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
    body.add_row(tr("ID"), str(artifact.get("id", "")))
    body.add_row(tr("Run"), str(artifact.get("run_id", "")))
    body.add_row(tr("Name"), str(artifact.get("name", "")))
    body.add_row(tr("MIME"), str(artifact.get("mime_type", "")))
    body.add_row(tr("Size"), str(artifact.get("size", 0)))
    body.add_row(tr("SHA-256"), str(artifact.get("sha256", "")))
    body.add_row(tr("Execution"), str(artifact.get("execution_id") or "—"))
    body.add_row(tr("Description"), str(artifact.get("description", "")) or "—")
    body.add_row(tr("Content"), str(artifact.get("content_url", "")))
    console.print(Panel(body, title=tr("Artifact"), border_style="cyan"))


def render_reports(console: Console, reports: Iterable[dict[str, Any]]) -> None:
    items = list(reports)
    if not items:
        console.print(f"[dim]{tr('No reports found.')}[/dim]")
        return
    table = Table(title=tr("Run Reports"), expand=True)
    table.add_column(tr("ID"), style="cyan", no_wrap=True)
    table.add_column(tr("Format"))
    table.add_column(tr("Artifact"))
    table.add_column(tr("Findings"), justify="right")
    table.add_column(tr("Created"))
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
    body.add_row(tr("ID"), str(report.get("id", "")))
    body.add_row(tr("Run"), str(report.get("run_id", "")))
    body.add_row(tr("Format"), str(report.get("format", "")))
    body.add_row(tr("Artifact"), str(report.get("artifact_id", "")))
    body.add_row(
        tr("Findings"), ", ".join(str(item) for item in report.get("finding_ids", [])) or "—"
    )
    body.add_row(tr("Content"), str(report.get("content_url", "")))
    body.add_row(tr("Created"), str(report.get("created_at", "")))
    console.print(Panel(body, title=tr("Report"), border_style="green"))


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
        summary = Group(
            Text(error.message, style="bold"),
            Text(f"code={error.code} status={error.status_code}", style="dim"),
        )
        details = _safety_stop_table(error.code, error.details)
        console.print(
            Panel(
                Group(summary, details) if details is not None else summary,
                title=tr("RiftX API error"),
                border_style="red",
            )
        )
    else:
        console.print(Panel(str(error), title=tr("Error"), border_style="red"))


def _safety_stop_table(code: str, details: object) -> Table | None:
    if code not in {"execution_cancel_failed", "safety_stop_failed"} or not isinstance(
        details, Mapping
    ):
        return None
    rows = _stop_resource_rows(details.get("stop_resources"))
    if not any(row.resource_type == "Execution" for row in rows):
        rows = (
            _resource_rows(
                "Execution",
                {
                    "attempted_ids": details.get("execution_ids"),
                    "node_ids": details.get("execution_nodes"),
                    "observed_statuses": details.get("execution_statuses"),
                    "confirmed_ids": details.get("confirmed_execution_ids"),
                    "confirmed_statuses": details.get("confirmed_statuses"),
                    "failures": details.get("failed_executions"),
                },
            )
            + rows
        )
    if not rows:
        return None

    table = Table(title=tr("Safety stop disposition"), expand=True)
    table.add_column(tr("Resource type"), overflow="fold")
    table.add_column(tr("Resource ID"), style="cyan", overflow="fold")
    table.add_column(tr("Node"), overflow="fold")
    table.add_column(tr("Stop result"), overflow="fold")
    table.add_column(tr("Reason"), overflow="fold")
    for row in rows:
        if row.confirmed_status is not None:
            result = tr("Stopped ({status})", status=row.confirmed_status)
        elif row.confirmed:
            result = tr("Stop confirmed")
        else:
            result = tr("Stop unconfirmed")
            if row.observed_status is not None:
                result += f" ({row.observed_status})"
        table.add_row(
            Text(tr(row.resource_type)),
            Text(row.resource_id),
            Text(row.node_id or tr("Unknown node")),
            Text(result),
            Text(row.failure if not row.confirmed and row.failure is not None else "—"),
        )
    return table


def _stop_resource_rows(value: object) -> list[_StopResourceRow]:
    if not isinstance(value, Mapping):
        return []
    return [
        row
        for resource_key, resource_label in _STOP_RESOURCE_TYPES
        for row in _resource_rows(resource_label, value.get(resource_key))
    ]


def _resource_rows(resource_type: str, value: object) -> list[_StopResourceRow]:
    if not isinstance(value, Mapping):
        return []
    attempted_ids = _string_list(value.get("attempted_ids"))
    node_ids = _string_map(value.get("node_ids"))
    observed_statuses = _string_map(value.get("observed_statuses"))
    confirmed_statuses = _string_map(value.get("confirmed_statuses"))
    confirmed_ids = set(_string_list(value.get("confirmed_ids")))
    failures = _string_map(value.get("failures"))
    succeeded = value.get("succeeded") is True
    rows: list[_StopResourceRow] = []
    for resource_id in attempted_ids:
        confirmed_status = confirmed_statuses.get(resource_id)
        confirmed = succeeded or resource_id in confirmed_ids or confirmed_status is not None
        rows.append(
            _StopResourceRow(
                resource_type=resource_type,
                resource_id=resource_id,
                node_id=node_ids.get(resource_id),
                observed_status=observed_statuses.get(resource_id),
                confirmed_status=confirmed_status,
                failure=None if confirmed else failures.get(resource_id),
                confirmed=confirmed,
            )
        )
    return rows


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str)))


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)
    }


def _status_text(status: str) -> Text:
    colors = {
        "running": "green",
        "completed": "bright_green",
        "waiting_approval": "yellow",
        "paused": "yellow",
        "failed": "red",
        "cancelled": "red",
        "queued": "blue",
        "starting": "blue",
        "created": "blue",
        "hard_timeout": "red",
        "preparing": "blue",
        "online": "green",
        "degraded": "yellow",
        "offline": "red",
        "lost": "bright_red",
    }
    return Text(tr(status), style=colors.get(status, "white"))


def _availability_text(availability: str) -> Text:
    colors = {
        "available": "green",
        "unavailable": "red",
        "misconfigured": "yellow",
        "disabled": "dim",
    }
    return Text(tr(availability), style=colors.get(availability, "white"))


_METRIC_LABELS = {
    "task_completion_rate": "Task Completion Rate",
    "repeated_tool_call_rate": "Repeated Tool Call Rate",
    "invalid_tool_call_rate": "Invalid Tool Call Rate",
    "recovery_success_rate": "Recovery Success Rate",
    "execution_duplication_rate": "Execution Duplication Rate",
    "compaction_fidelity": "Compaction Fidelity",
    "context_token_efficiency": "Context Token Efficiency",
    "subagent_utility": "Subagent Utility",
    "approval_resume_success_rate": "Approval Resume Success Rate",
    "browser_action_failure_rate": "Browser Action Failure Rate",
    "citation_coverage": "Citation Coverage",
}


def render_runtime_metrics(console: Console, snapshot: dict[str, Any]) -> None:
    metrics = snapshot.get("metrics") or {}
    table = Table(title=f"{tr('Runtime Metrics')} · {snapshot.get('run_id', '')}", expand=True)
    table.add_column(tr("Metric"), style="cyan")
    table.add_column(tr("Value"), justify="right")
    table.add_column(tr("Counts"), justify="right")
    table.add_column(tr("Direction"))
    for name, label in _METRIC_LABELS.items():
        metric = metrics.get(name) or {}
        value = metric.get("value")
        table.add_row(
            tr(label),
            f"{float(value) * 100:.2f}%" if value is not None else "—",
            f"{metric.get('numerator', 0)}/{metric.get('denominator', 0)}",
            str(metric.get("direction", "")),
        )
    console.print(table)
    console.print(f"{tr('Generated')}: [dim]{snapshot.get('generated_at', '—')}[/dim]")
