"""Typer command-line interface for the RiftX control plane."""

from __future__ import annotations

import asyncio
import logging
import math
import platform
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import httpx
import typer
import uvicorn
from click import Command, Context
from rich.console import Console
from typer.core import TyperGroup

from riftx.api import APISettings, create_app
from riftx.config import RiftXConfig, RiftXConfigError, load_riftx_config
from riftx.doctor import (
    DoctorFixError,
    apply_local_doctor_fixes,
    run_live_doctor,
    run_local_doctor,
)
from riftx.domain import ApprovalMode, EntryPointKind, RunKind, RunStatus, TerminalOwner
from riftx.memory import MemoryScopeType, MemoryType
from riftx.models import (
    MAX_MODEL_TIMEOUT_SECONDS,
    ModelAPI,
    ModelProviderKind,
    validate_provider_base_url,
    validate_remote_api_key_env,
    validate_remote_base_url,
)
from riftx.runner.daemon import RunnerDaemonConfig, run_runner_daemon
from riftx.security import DeploymentProfileError, is_loopback_host
from riftx.temporal.worker_runtime import build_temporal_worker

from .client import APIClient, RiftXAPIError
from .i18n import Language, normalize_language, set_language, tr
from .interactive import run_interactive
from .render import (
    render_approvals,
    render_artifact,
    render_artifacts,
    render_doctor_report,
    render_error,
    render_event,
    render_execution,
    render_execution_wait,
    render_executions,
    render_memories,
    render_memory,
    render_model_profile,
    render_model_profiles,
    render_node,
    render_nodes,
    render_report,
    render_reports,
    render_run,
    render_runs,
    render_runtime_metrics,
    render_terminal,
    render_tools,
)
from .terminal import attach_terminal

console = Console()


class _AuditGroup(TyperGroup):
    """Route an unknown first token to the local folder scan command."""

    def resolve_command(
        self,
        ctx: Context,
        args: list[str],
    ) -> tuple[str | None, Command | None, list[str]]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            command = self.get_command(ctx, "scan")
            return "scan", command, args
        return super().resolve_command(ctx, args)


app = typer.Typer(
    name="riftx",
    help="Host-native durable agent execution platform.",
    no_args_is_help=False,
    invoke_without_command=True,
    rich_markup_mode="rich",
)
run_app = typer.Typer(help="Create, inspect, and control Runs.")
execution_app = typer.Typer(help="Inspect, wait for, and cancel durable Executions.")
nodes_app = typer.Typer(help="Register and inspect execution nodes.")
tools_app = typer.Typer(help="Inspect the node-local Tool Registry.")
terminal_app = typer.Typer(help="Create and control interactive terminal sessions.")
artifact_app = typer.Typer(help="Register and inspect immutable Run artifacts.")
report_app = typer.Typer(help="Generate and inspect structured Run reports.")
memory_app = typer.Typer(help="Create and manage scope-aware long-term Memory.")
model_app = typer.Typer(help="Configure model provider profiles.")
audit_app = typer.Typer(
    cls=_AuditGroup,
    help="Audit a local folder with read-only static analysis.",
)
app.add_typer(run_app, name="run")
app.add_typer(execution_app, name="execution")
app.add_typer(nodes_app, name="node")
app.add_typer(tools_app, name="tools")
app.add_typer(terminal_app, name="terminal")
app.add_typer(artifact_app, name="artifact")
app.add_typer(report_app, name="report")
app.add_typer(memory_app, name="memory")
app.add_typer(model_app, name="model")
app.add_typer(audit_app, name="audit")


@dataclass(frozen=True, slots=True)
class CLIState:
    api_url: str
    config: RiftXConfig
    config_path: Path | None
    language: Language


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
    language: Annotated[
        str | None,
        typer.Option(
            "--language",
            "-L",
            envvar="RIFTX_LANGUAGE",
            help="Language for CLI output (en or zh).",
        ),
    ] = None,
) -> None:
    """Run a command, or enter interactive mode when no command is given."""

    try:
        resolved_language = normalize_language(language)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--language") from exc
    set_language(resolved_language)

    try:
        config = load_riftx_config(explicit_path=config_path)
    except RiftXConfigError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    resolved_api_url = api_url or f"http://{config.server.host}:{config.server.port}"
    context.obj = CLIState(
        api_url=resolved_api_url,
        config=config,
        config_path=config_path,
        language=resolved_language,
    )
    if context.invoked_subcommand is None:
        with APIClient(resolved_api_url) as client:
            run_interactive(client, console)


@app.command()
def interactive(context: typer.Context) -> None:
    """Enter the interactive RiftX session explicitly."""

    state = _state(context)
    with APIClient(state.api_url) as client:
        run_interactive(client, console)


@app.command()
def doctor(
    context: typer.Context,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Apply registered offline-safe local repairs."),
    ] = False,
) -> None:
    """Inspect RiftX readiness and optionally apply bounded local repairs."""

    state = _state(context)
    report = run_local_doctor(state.config)
    with APIClient(state.api_url, timeout_seconds=3) as client:
        if fix:
            persistence_fix = any(
                check.id in {"database_migrations", "pack_integrity"} and check.fixable
                for check in report.checks
            )
            control_plane_reachable = False
            if persistence_fix:
                try:
                    client.health()
                except httpx.TransportError:
                    pass
                except Exception:
                    control_plane_reachable = True
                else:
                    control_plane_reachable = True
            try:
                fixes = apply_local_doctor_fixes(
                    state.config,
                    report,
                    allow_persistence_fix=not control_plane_reachable,
                )
            except DoctorFixError as exc:
                console.print(f"[red]Doctor fix failed:[/red] {exc}")
                render_doctor_report(console, report)
                raise typer.Exit(1) from exc
            for applied in fixes:
                console.print(f"Fixed {applied.check_id}: repaired {applied.path}")
                if applied.backup_path is not None:
                    console.print(f"Backup retained: {applied.backup_path}")
            report = run_local_doctor(state.config)
        report = run_live_doctor(state.config, report, client)
    render_doctor_report(console, report)
    if report.failed:
        raise typer.Exit(1)


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
    try:
        settings = APISettings.from_config(config)
    except DeploymentProfileError as exc:
        raise typer.BadParameter(str(exc), param_hint="--host/--config") from exc
    uvicorn.run(
        create_app(settings=settings),
        host=server.host,
        port=server.port,
        reload=reload,
        log_level="info",
    )


def _is_loopback_listen_host(host: str) -> bool:
    return is_loopback_host(host)


@app.command()
def worker(context: typer.Context) -> None:
    """Start the production Temporal Worker."""

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_temporal_worker(_state(context).config))


@app.command("runner")
def runner_daemon(
    context: typer.Context,
    server_url: Annotated[
        str | None,
        typer.Option("--server-url", help="Control Plane URL override."),
    ] = None,
    node_id: Annotated[
        str | None,
        typer.Option("--node-id", help="Stable Runner node ID override."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Runner display name."),
    ] = None,
    state_path: Annotated[
        Path | None,
        typer.Option("--state-path", help="Runner state directory override."),
    ] = None,
    credential_path: Annotated[
        Path | None,
        typer.Option(
            "--credential-path",
            help="Runner credential file override; keep it outside execution state.",
        ),
    ] = None,
) -> None:
    """Start the outbound Runner daemon using the shared RiftX configuration."""

    config = _state(context).config
    if config.audit.source_roots and (state_path is not None or credential_path is not None):
        raise typer.BadParameter(
            "Runner storage paths are deployment-owned when Audit source roots are configured",
            param_hint="--state-path/--credential-path",
        )
    resolved_node_id = node_id or config.runner.node_id
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        run_runner_daemon(
            RunnerDaemonConfig(
                server_url=server_url or config.runner.endpoint,
                node_id=resolved_node_id,
                name=name or platform.node() or resolved_node_id,
                state_path=(state_path or config.runner.state_path).expanduser(),
                credential_path=(credential_path or config.runner.credential_path).expanduser(),
                registration_token=config.runner.registration_token,
                command_lease_seconds=config.runner.command_lease_seconds,
                require_containment=config.execution.require_containment,
                payload_uid=config.execution.payload_uid,
                payload_gid=config.execution.payload_gid,
                audit=config.audit,
            )
        )
    )


@app.command()
def web(
    context: typer.Context,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the Control Plane UI in a browser."),
    ] = True,
) -> None:
    """Print and optionally open the RiftX WebUI."""

    url = f"{_state(context).api_url.rstrip('/')}/"
    console.print(url)
    if open_browser:
        webbrowser.open(url)


@audit_app.command("scan")
def scan_local_folder(
    context: typer.Context,
    folder: Annotated[Path, typer.Argument(help="Local folder to audit.")],
) -> None:
    """Create and start a read-only static audit for a local folder."""

    try:
        source_path = folder.expanduser().resolve(strict=True)
    except OSError as exc:
        raise typer.BadParameter("folder does not exist", param_hint="folder") from exc
    if not source_path.is_dir():
        raise typer.BadParameter("folder must be a directory", param_hint="folder")

    def operation(client: APIClient) -> None:
        created = client.create_local_audit(str(source_path))
        audit_id = str(created["audit_id"])
        console.print_json(data=client.start_local_audit(audit_id))

    _run_with_client(context, operation)


@audit_app.command("status")
def show_local_audit_status(
    context: typer.Context,
    audit_id: Annotated[str, typer.Argument(help="Local Audit ID.")],
) -> None:
    """Show local Audit status."""

    _run_with_client(
        context,
        lambda client: console.print_json(data=client.get_local_audit(audit_id)),
    )


@audit_app.command("findings")
def show_local_audit_findings(
    context: typer.Context,
    audit_id: Annotated[str, typer.Argument(help="Local Audit ID.")],
) -> None:
    """List local Audit Findings."""

    _run_with_client(
        context,
        lambda client: console.print_json(
            data=client.list_local_audit_findings(audit_id)
        ),
    )


@audit_app.command("report")
def show_local_audit_report(
    context: typer.Context,
    audit_id: Annotated[str, typer.Argument(help="Local Audit ID.")],
    format: Annotated[
        str,
        typer.Option("--format", help="Report format: json or markdown."),
    ] = "json",
) -> None:
    """Print a local Audit report."""

    normalized = format.lower()
    if normalized not in {"json", "markdown"}:
        raise typer.BadParameter(
            "format must be json or markdown",
            param_hint="--format",
        )
    _run_with_client(
        context,
        lambda client: console.print(
            client.get_local_audit_report(audit_id, format=normalized),
            markup=False,
            highlight=False,
            end="",
        ),
    )


@audit_app.command("cancel")
def cancel_local_audit(
    context: typer.Context,
    audit_id: Annotated[str, typer.Argument(help="Local Audit ID.")],
) -> None:
    """Cancel a local Audit."""

    _run_with_client(
        context,
        lambda client: console.print_json(data=client.cancel_local_audit(audit_id)),
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
    console.print(f"[green]{tr('Approval saved and workflow signaled.')}[/green]")


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
    console.print(f"[yellow]{tr('Approval rejected and workflow signaled.')}[/yellow]")


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
            attach_message = tr(
                "Attaching to {session_id}; press Ctrl+] to detach.",
                session_id=session_id,
            )
            console.print(f"[dim]{attach_message}[/dim]")
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


@model_app.command("list")
def list_model_profiles(context: typer.Context) -> None:
    """List configured model profiles without exposing credentials."""

    _run_with_client(
        context,
        lambda client: render_model_profiles(console, client.list_model_profiles()),
    )


@model_app.command("show")
def show_model_profile(
    context: typer.Context,
    profile_name: Annotated[str, typer.Argument(help="Model profile name.")],
) -> None:
    """Show one model profile without exposing its API key."""

    _run_with_client(
        context,
        lambda client: render_model_profile(console, client.get_model_profile(profile_name)),
    )


@model_app.command("configure")
def configure_model_profile(
    context: typer.Context,
    profile_name: Annotated[str, typer.Argument(help="Model profile name.")],
    model_name: Annotated[str, typer.Option("--model", help="Provider model name.")],
    provider: Annotated[
        ModelProviderKind,
        typer.Option("--provider", case_sensitive=False),
    ] = ModelProviderKind.OPENAI_COMPATIBLE,
    request_mode: Annotated[
        ModelAPI,
        typer.Option("--request-mode", case_sensitive=False),
    ] = ModelAPI.CHAT_COMPLETIONS,
    base_url: Annotated[str | None, typer.Option("--base-url")] = None,
    api_key_env: Annotated[
        str | None,
        typer.Option(
            "--api-key-env",
            help="RIFTX_MODEL_* variable checked before local storage.",
        ),
    ] = "RIFTX_MODEL_API_KEY",
    requires_api_key: Annotated[
        bool,
        typer.Option("--requires-api-key/--no-api-key"),
    ] = True,
    timeout_seconds: Annotated[float, typer.Option("--timeout")] = 120,
    max_retries: Annotated[int, typer.Option("--max-retries", min=0, max=10)] = 2,
    api_key_prompt: Annotated[
        bool,
        typer.Option("--api-key-prompt", help="Read and hide an API key interactively."),
    ] = False,
    api_key_stdin: Annotated[
        bool,
        typer.Option("--api-key-stdin", help="Read an API key from standard input."),
    ] = False,
    clear_stored_api_key: Annotated[
        bool,
        typer.Option("--clear-stored-api-key"),
    ] = False,
) -> None:
    """Create or replace a model profile through the Control Plane."""

    try:
        base_url = validate_remote_base_url(base_url)
        validate_provider_base_url(provider, base_url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--base-url") from exc
    try:
        api_key_env = validate_remote_api_key_env(api_key_env)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--api-key-env") from exc

    if api_key_prompt and api_key_stdin:
        raise typer.BadParameter(
            "choose either --api-key-prompt or --api-key-stdin",
            param_hint="--api-key-prompt",
        )
    if clear_stored_api_key and (api_key_prompt or api_key_stdin):
        raise typer.BadParameter(
            "--clear-stored-api-key cannot be combined with API key input",
            param_hint="--clear-stored-api-key",
        )
    if not requires_api_key and (api_key_prompt or api_key_stdin):
        raise typer.BadParameter(
            "API key input cannot be used with --no-api-key",
            param_hint="--no-api-key",
        )
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > MAX_MODEL_TIMEOUT_SECONDS
    ):
        raise typer.BadParameter(
            f"timeout must be finite, greater than 0, and at most "
            f"{MAX_MODEL_TIMEOUT_SECONDS:g} seconds",
            param_hint="--timeout",
        )

    api_key: str | None = None
    if api_key_prompt:
        api_key = typer.prompt("API key", hide_input=True).strip()
    elif api_key_stdin:
        api_key = sys.stdin.read().strip()
    if (api_key_prompt or api_key_stdin) and not api_key:
        raise typer.BadParameter("API key input was empty")

    payload: dict[str, object] = {
        "provider": provider.value,
        "model": model_name,
        "request_mode": request_mode.value,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "requires_api_key": requires_api_key,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "clear_stored_api_key": clear_stored_api_key,
    }
    if api_key is not None:
        payload["api_key"] = api_key
    _run_with_client(
        context,
        lambda client: render_model_profile(
            console,
            client.configure_model_profile(profile_name, payload),
        ),
    )


@model_app.command("default")
def set_default_model_profile(
    context: typer.Context,
    profile_name: Annotated[str, typer.Argument(help="New default model profile.")],
) -> None:
    """Select the default model profile used for new Runs."""

    _run_with_client(
        context,
        lambda client: render_model_profiles(
            console,
            client.set_default_model_profile(profile_name),
        ),
    )


@model_app.command("remove")
def remove_model_profile(
    context: typer.Context,
    profile_name: Annotated[str, typer.Argument(help="Model profile to remove.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Remove a non-default model profile and its stored local API key."""

    if not yes and not typer.confirm(f"Remove model profile {profile_name!r}?"):
        raise typer.Abort()
    _run_with_client(
        context,
        lambda client: render_model_profiles(
            console,
            client.delete_model_profile(profile_name),
        ),
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
    model_profile: Annotated[
        str | None,
        typer.Option("--model", help="Model profile for this Run."),
    ] = None,
    success: Annotated[
        list[str] | None,
        typer.Option("--success", help="Repeatable success criterion."),
    ] = None,
    entry: Annotated[
        list[str] | None,
        typer.Option("--entry", help="Repeatable entry point as KIND=VALUE."),
    ] = None,
) -> None:
    """Create a durable Run that waits for the first concrete instruction."""

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
    if model_profile:
        payload["model_profile"] = model_profile

    state = _state(context)

    def operation(client: APIClient) -> None:
        run = client.create_run(payload)
        render_run(console, run)
        run_id = str(run.get("id") or "").strip()
        console.print(
            "[green]"
            + tr(
                "Run created. The objective and boundaries are saved; the Agent is "
                "waiting for your first concrete instruction."
            )
            + "[/green]"
        )
        console.print(
            f"[dim]{tr('No model or Tool will run before that instruction is sent.')}[/dim]"
        )
        if run_id:
            console.print(
                tr(
                    'Send it with: riftx run message {run_id} "YOUR INSTRUCTION"',
                    run_id=run_id,
                )
            )
            console.print(
                tr(
                    "Open the conversation: {url}",
                    url=f"{state.api_url.rstrip('/')}/runs/{run_id}",
                )
            )

    _run_with_client(context, operation)


@run_app.command("list")
def list_runs(
    context: typer.Context,
    run_status: Annotated[
        RunStatus | None,
        typer.Option("--status", case_sensitive=False, help="Filter by Run status."),
    ] = None,
    run_kind: Annotated[
        RunKind | None,
        typer.Option("--kind", case_sensitive=False, help="Filter by Run kind."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List persisted Runs."""

    def operation(client: APIClient) -> None:
        payload = client.list_runs(
            status=run_status.value if run_status else None,
            kind=run_kind.value if run_kind else None,
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


@run_app.command("metrics")
def show_run_metrics(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
) -> None:
    """Show the eleven QA-02 runtime metrics for a persisted Run."""

    _run_with_client(
        context,
        lambda client: render_runtime_metrics(console, client.get_run_metrics(run_id)),
    )


@run_app.command("pause")
def pause_run(context: typer.Context, run_id: str) -> None:
    """Pause a Run after confirming its active effects stopped."""

    _run_with_client(context, lambda client: client.pause_run(run_id))
    console.print(f"[green]{tr('Pause confirmed; active effects stopped.')}[/green]")


@run_app.command("resume")
def resume_run(context: typer.Context, run_id: str) -> None:
    """Resume a paused Run."""

    _run_with_client(context, lambda client: client.resume_run(run_id))
    console.print(f"[green]{tr('Resume requested.')}[/green]")


@run_app.command("cancel-current")
def cancel_current(context: typer.Context, run_id: str) -> None:
    """Cancel only the Run's current active execution."""

    _run_with_client(context, lambda client: client.cancel_current_execution(run_id))
    console.print(f"[green]{tr('Current execution stop confirmed.')}[/green]")


@run_app.command("cancel")
def cancel_run(context: typer.Context, run_id: str) -> None:
    """Cancel the durable Run and clean up its active executions."""

    _run_with_client(context, lambda client: client.cancel_run(run_id))
    console.print(f"[green]{tr('Run cancellation confirmed; active effects stopped.')}[/green]")


@run_app.command("compact")
def compact_run(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    max_history_items: Annotated[
        int,
        typer.Option("--max-items", min=1, max=10_000),
    ] = 100,
) -> None:
    """Request durable Agent context compaction."""

    _run_with_client(
        context,
        lambda client: client.compact_run(run_id, max_history_items=max_history_items),
    )
    console.print(f"[green]{tr('Context compaction requested.')}[/green]")


@run_app.command("model")
def switch_run_model(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    model_profile: Annotated[str, typer.Argument(help="Target model profile.")],
) -> None:
    """Checkpoint a Run and continue it with another model profile."""

    _run_with_client(
        context,
        lambda client: client.switch_run_model(run_id, model_profile),
    )
    console.print(
        f"[green]{tr('Model switch to {model} requested.', model=repr(model_profile))}[/green]"
    )


@run_app.command("message")
def send_message(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    message: Annotated[str, typer.Argument(help="Message to append to the Agent session.")],
    message_event_id: Annotated[
        str | None,
        typer.Option(
            "--message-event-id",
            help=(
                "Stable UUID for safe retry after an ambiguous network/Temporal failure. "
                "Reuse the same UUID only with the exact same Run and message."
            ),
        ),
    ] = None,
) -> None:
    """Queue a user message through the durable workflow."""

    resolved_message_event_id = message_event_id or str(uuid4())
    console.print(f"[dim]message_event_id={resolved_message_event_id}[/dim]")
    _run_with_client(
        context,
        lambda client: client.append_message(
            run_id,
            message,
            message_event_id=resolved_message_event_id,
        ),
    )
    console.print(f"[green]{tr('Message queued.')}[/green]")


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
        console.print(f"[dim]{tr('Stopped watching.')}[/dim]")
    except (RiftXAPIError, httpx.HTTPError) as exc:
        render_error(console, exc)
        raise typer.Exit(1) from exc


@memory_app.command("list")
def list_memories(
    context: typer.Context,
    scope_type: Annotated[
        MemoryScopeType | None,
        typer.Option("--scope", case_sensitive=False),
    ] = None,
    scope_id: Annotated[str | None, typer.Option("--scope-id")] = None,
    include_inactive: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    """List current Memory, optionally restricted to one exact Scope."""

    _run_with_client(
        context,
        lambda client: render_memories(
            console,
            client.list_memories(
                scope_type=scope_type.value if scope_type else None,
                scope_id=scope_id,
                include_inactive=include_inactive,
            ).get("items", []),
        ),
    )


@memory_app.command("show")
def show_memory(context: typer.Context, memory_id: str) -> None:
    """Show one long-term Memory record and its sources."""

    _run_with_client(
        context,
        lambda client: render_memory(console, client.get_memory(memory_id)),
    )


@memory_app.command("create")
def create_memory(
    context: typer.Context,
    memory_type: Annotated[MemoryType, typer.Argument(case_sensitive=False)],
    scope_type: Annotated[MemoryScopeType, typer.Argument(case_sensitive=False)],
    scope_id: Annotated[str, typer.Argument()],
    title: Annotated[str, typer.Argument()],
    content: Annotated[str, typer.Argument()],
    source_refs: Annotated[list[str] | None, typer.Option("--source")] = None,
    summary: Annotated[str | None, typer.Option("--summary")] = None,
    keywords: Annotated[list[str] | None, typer.Option("--keyword")] = None,
    confidence: Annotated[float, typer.Option(min=0.0, max=1.0)] = 1.0,
    importance: Annotated[float, typer.Option(min=0.0, max=1.0)] = 0.5,
    pinned: Annotated[bool, typer.Option("--pin")] = False,
) -> None:
    """Manually create a sourced long-term Memory record."""

    payload: dict[str, object] = {
        "memory_type": memory_type.value,
        "scope_type": scope_type.value,
        "scope_id": scope_id,
        "title": title,
        "content": content,
        "summary": summary or title,
        "source_refs": source_refs or [],
        "retrieval_keywords": keywords or [],
        "confidence": confidence,
        "importance": importance,
        "pinned": pinned,
    }
    _run_with_client(
        context,
        lambda client: render_memory(console, client.create_memory(payload)),
    )


@memory_app.command("edit")
def edit_memory(
    context: typer.Context,
    memory_id: str,
    title: Annotated[str | None, typer.Option()] = None,
    content: Annotated[str | None, typer.Option()] = None,
    summary: Annotated[str | None, typer.Option()] = None,
    sources: Annotated[list[str] | None, typer.Option("--source")] = None,
) -> None:
    """Edit the user-facing content or source references of active Memory."""

    payload: dict[str, object] = {
        key: value
        for key, value in {
            "title": title,
            "content": content,
            "summary": summary,
            "source_refs": sources,
        }.items()
        if value is not None
    }
    _run_with_client(
        context,
        lambda client: render_memory(
            console,
            client.update_memory(memory_id, payload),
        ),
    )


@memory_app.command("forget")
def forget_memory(context: typer.Context, memory_id: str) -> None:
    """Soft-delete Memory so it is never retrieved again."""

    _run_with_client(context, lambda client: client.delete_memory(memory_id))
    console.print(f"[green]{tr('Memory deleted.')}[/green]")


@memory_app.command("pin")
def pin_memory(
    context: typer.Context,
    memory_id: str,
    pinned: Annotated[bool, typer.Option("--on/--off")] = True,
) -> None:
    """Pin or unpin active Memory within its existing Scope."""

    _run_with_client(
        context,
        lambda client: client.pin_memory(memory_id, pinned=pinned),
    )
    console.print(f"[green]{tr('Memory pin updated.')}[/green]")


@execution_app.command("show")
def show_execution(
    context: typer.Context,
    execution_id: Annotated[str, typer.Argument(help="Execution ID.")],
) -> None:
    """Show one durable Execution."""

    _run_with_client(
        context,
        lambda client: render_execution(console, client.get_execution(execution_id)),
    )


@execution_app.command("list")
def list_executions(
    context: typer.Context,
    run_id: Annotated[str, typer.Option("--run", help="Run ID.")],
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List durable Executions for a Run."""

    def operation(client: APIClient) -> None:
        payload = client.list_executions(run_id, limit=limit, offset=offset)
        render_executions(console, payload.get("items", []))

    _run_with_client(context, operation)


@execution_app.command("wait")
def wait_execution(
    context: typer.Context,
    execution_id: Annotated[str, typer.Argument(help="Execution ID.")],
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout", min=0.001, max=120, help="Maximum wait duration."),
    ] = 30.0,
    stdout_cursor: Annotated[int, typer.Option("--stdout-cursor", min=0)] = 0,
    stderr_cursor: Annotated[int, typer.Option("--stderr-cursor", min=0)] = 0,
    max_bytes: Annotated[int, typer.Option("--max-bytes", min=1, max=1024 * 1024)] = (64 * 1024),
) -> None:
    """Wait for an Execution without treating a wait timeout as tool failure."""

    _run_with_client(
        context,
        lambda client: render_execution_wait(
            console,
            client.wait_execution(
                execution_id,
                timeout_seconds=timeout_seconds,
                stdout_cursor=stdout_cursor,
                stderr_cursor=stderr_cursor,
                max_bytes=max_bytes,
            ),
        ),
    )


@execution_app.command("cancel")
def cancel_execution(
    context: typer.Context,
    execution_id: Annotated[str, typer.Argument(help="Execution ID.")],
) -> None:
    """Request cancellation of one durable Execution."""

    _run_with_client(
        context,
        lambda client: render_execution(console, client.cancel_execution(execution_id)),
    )


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


@tools_app.command("show")
def show_tool(
    context: typer.Context,
    tool_id: Annotated[str, typer.Argument(help="Tool definition ID.")],
    node_id: Annotated[str, typer.Option("--node")] = "local",
) -> None:
    """Show one configured tool and its probed availability."""

    def operation(client: APIClient) -> None:
        payload = client.list_tools(node_id)
        tools = [
            item
            for item in payload.get("tools", [])
            if item.get("definition", {}).get("id") == tool_id
        ]
        if not tools:
            raise typer.BadParameter(f"tool {tool_id!r} was not found", param_hint="TOOL_ID")
        render_tools(console, {**payload, "tools": tools})

    _run_with_client(context, operation)


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


async def _run_temporal_worker(config: RiftXConfig) -> None:
    runtime = await build_temporal_worker(config)
    await runtime.run()


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
