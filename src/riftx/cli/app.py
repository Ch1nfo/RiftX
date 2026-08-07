"""Typer command-line interface for the RiftX control plane."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import platform
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from uuid import uuid4

import httpx
import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from riftx.config import (
    RiftXConfig,
    RiftXConfigError,
    default_user_config_path,
    load_riftx_config,
)
from riftx.doctor import (
    DoctorFixError,
    DoctorReport,
    apply_local_doctor_fixes,
    run_live_doctor,
    run_local_doctor,
)
from riftx.domain import ApprovalMode, EntryPointKind, RunKind, RunStatus, TerminalOwner
from riftx.memory import MemoryScopeType, MemoryType
from riftx.models import (
    MAX_MODEL_TIMEOUT_SECONDS,
    ModelAPI,
    ModelProfile,
    ModelProviderKind,
    validate_provider_base_url,
    validate_remote_api_key_env,
    validate_remote_base_url,
)
from riftx.onboarding import (
    OnboardError,
    initialize_local_onboarding,
    validate_existing_onboarding,
)
from riftx.security import DeploymentProfileError, is_loopback_host

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
    render_pentest_status,
    render_report,
    render_reports,
    render_run,
    render_runs,
    render_runtime_metrics,
    render_terminal,
    render_tools,
)
from .terminal import attach_terminal

if TYPE_CHECKING:
    from riftx.capability_management import LocalCapabilityState

console = Console()


_GETTING_STARTED_PANEL = "Getting started"
_PENTEST_PANEL = "Pentest workflow"
_SERVICE_PANEL = "Service operation"
_ADVANCED_PANEL = "Advanced"

app = typer.Typer(
    name="riftx",
    help="Pentest-first Agent for authorized security work.",
    epilog="Start with `riftx onboard`, then verify readiness with `riftx doctor`.",
    no_args_is_help=False,
    invoke_without_command=True,
    rich_markup_mode="rich",
)
run_app = typer.Typer(help="Create, inspect, and control Runs.")
pentest_app = typer.Typer(help="Start and control authorized Pentest Runs.")
execution_app = typer.Typer(help="Inspect, wait for, and cancel durable Executions.")
nodes_app = typer.Typer(help="Register and inspect execution nodes.")
tools_app = typer.Typer(help="Inspect the node-local Tool Registry.")
terminal_app = typer.Typer(help="Create and control interactive terminal sessions.")
artifact_app = typer.Typer(help="Register and inspect immutable Run artifacts.")
report_app = typer.Typer(help="Generate and inspect structured Run reports.")
memory_app = typer.Typer(help="Create and manage scope-aware long-term Memory.")
model_app = typer.Typer(help="Configure model provider profiles.")
demo_app = typer.Typer(help="Run sanitized offline security demonstrations.")
capabilities_app = typer.Typer(help="Inspect the local Capability catalog.")
packs_app = typer.Typer(help="Inspect and manage local Capability Packs.")
skills_app = typer.Typer(help="Validate and manage local Operator Skills.")
app.add_typer(run_app, name="run", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(pentest_app, name="pentest", rich_help_panel=_PENTEST_PANEL)
app.add_typer(execution_app, name="execution", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(nodes_app, name="node", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(tools_app, name="tools", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(terminal_app, name="terminal", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(artifact_app, name="artifact", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(report_app, name="report", rich_help_panel=_PENTEST_PANEL)
app.add_typer(memory_app, name="memory", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(model_app, name="model", rich_help_panel=_GETTING_STARTED_PANEL)
app.add_typer(demo_app, name="demo", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(capabilities_app, name="capabilities", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(packs_app, name="packs", rich_help_panel=_ADVANCED_PANEL)
app.add_typer(skills_app, name="skills", rich_help_panel=_PENTEST_PANEL)


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


@app.command(rich_help_panel=_GETTING_STARTED_PANEL)
def onboard(
    context: typer.Context,
    non_interactive: Annotated[
        bool,
        typer.Option("--non-interactive", help="Use only command options and defaults."),
    ] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config-path", help="User configuration file to create or resume."),
    ] = None,
    provider: Annotated[
        ModelProviderKind,
        typer.Option("--provider", case_sensitive=False),
    ] = ModelProviderKind.OPENAI,
    model_name: Annotated[
        str,
        typer.Option("--model", help="Primary model identifier."),
    ] = "gpt-5.6",
    request_mode: Annotated[
        ModelAPI,
        typer.Option("--request-mode", case_sensitive=False),
    ] = ModelAPI.CHAT_COMPLETIONS,
    base_url: Annotated[str | None, typer.Option("--base-url")] = None,
    api_key_env: Annotated[
        str | None,
        typer.Option("--api-key-env", help="RIFTX_MODEL_* credential environment variable."),
    ] = "RIFTX_MODEL_API_KEY",
    requires_api_key: Annotated[
        bool,
        typer.Option("--api-key/--no-api-key"),
    ] = True,
    workspace_root: Annotated[
        Path | None,
        typer.Option("--workspace-root", help="Local workspace root."),
    ] = None,
) -> None:
    """Create or resume a safe local RiftX setup."""

    selected_path = (config_path or default_user_config_path()).expanduser()
    target = Path(os.path.abspath(os.fspath(selected_path)))
    created = False
    disabled_tools: tuple[str, ...] = ()
    if target.exists() or target.is_symlink():
        try:
            target = validate_existing_onboarding(target)
            config = load_riftx_config(explicit_path=target)
        except (OnboardError, RiftXConfigError) as exc:
            console.print(f"[red]Onboarding failed:[/red] {exc}")
            raise typer.Exit(1) from exc
        console.print(f"Using existing configuration without overwriting it: {target}")
    else:
        if not non_interactive:
            try:
                provider = ModelProviderKind(
                    typer.prompt("Model provider", default=provider.value).strip().lower()
                )
                model_name = typer.prompt("Primary model", default=model_name).strip()
                request_mode = ModelAPI(
                    typer.prompt("Model request mode", default=request_mode.value)
                    .strip()
                    .lower()
                )
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
            if provider is ModelProviderKind.OPENAI_COMPATIBLE and base_url is None:
                base_url = typer.prompt(
                    "OpenAI-compatible base URL",
                    default="http://127.0.0.1:11434/v1",
                ).strip()
            requires_api_key = typer.confirm(
                "Does this model require an API key?",
                default=requires_api_key,
            )
            if requires_api_key:
                api_key_env = typer.prompt(
                    "API key environment variable",
                    default=api_key_env or "RIFTX_MODEL_API_KEY",
                ).strip()
            if not typer.confirm(f"Create local RiftX configuration at {target}?", default=True):
                raise typer.Abort()
        try:
            normalized_base_url = validate_remote_base_url(base_url)
            validate_provider_base_url(provider, normalized_base_url)
            normalized_api_key_env = (
                validate_remote_api_key_env(api_key_env) if requires_api_key else None
            )
            if requires_api_key and normalized_api_key_env is None:
                raise ValueError("API-key-backed onboarding requires --api-key-env")
            model_profile = ModelProfile(
                provider=provider,
                model=model_name,
                api=request_mode,
                base_url=normalized_base_url,
                api_key_env=normalized_api_key_env,
                requires_api_key=requires_api_key,
            )
            initialized = initialize_local_onboarding(
                target,
                model_profile=model_profile,
                workspace_root=workspace_root,
            )
            config = load_riftx_config(explicit_path=initialized.config_path)
        except (OnboardError, RiftXConfigError, ValueError) as exc:
            console.print(f"[red]Onboarding failed:[/red] {exc}")
            raise typer.Exit(1) from exc
        target = initialized.config_path
        disabled_tools = initialized.disabled_tools
        created = True
        console.print(f"Created runtime configuration: {initialized.config_path}")
        console.print(f"Created model configuration: {initialized.models_path}")
        console.print(f"Created Tool Registry configuration: {initialized.tools_path}")

    if not is_loopback_host(config.server.host):
        console.print(
            "[red]Onboarding failed:[/red] local onboarding requires a loopback server host."
        )
        raise typer.Exit(1)
    report = run_local_doctor(config, runtime_config_path=target)
    persistence_fix = _requires_stopped_control_plane(report)
    control_plane_reachable = False
    if persistence_fix:
        api_url = f"http://{config.server.host}:{config.server.port}"
        with APIClient(api_url, timeout_seconds=3) as client:
            control_plane_reachable = _control_plane_reachable(client)
    try:
        fixes = apply_local_doctor_fixes(
            config,
            report,
            runtime_config_path=target,
            allow_persistence_fix=not control_plane_reachable,
        )
    except DoctorFixError as exc:
        console.print(f"[red]Onboarding bootstrap failed:[/red] {exc}")
        console.print(f"Configuration was retained for recovery: {target}")
        render_doctor_report(console, report)
        raise typer.Exit(1) from exc
    for applied in fixes:
        console.print(f"Initialized {applied.check_id}: {applied.path}")
    report = run_local_doctor(config, runtime_config_path=target)
    if disabled_tools:
        console.print(
            "Optional tools disabled because their executables were not found: "
            + ", ".join(disabled_tools)
        )
    if (
        created
        and requires_api_key
        and normalized_api_key_env is not None
        and normalized_api_key_env not in os.environ
    ):
        console.print(
            f"Set {normalized_api_key_env} before starting model-backed tasks."
        )
    render_doctor_report(console, report)
    console.print("[green]Onboarding complete.[/green]")
    if target != default_user_config_path():
        console.print(f"Use this setup with RIFTX_CONFIG={target}")
    console.print("Next: set RIFTX_ADMIN_TOKEN, then run `riftx doctor`.")


@app.command(rich_help_panel=_GETTING_STARTED_PANEL)
def doctor(
    context: typer.Context,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Apply registered offline-safe local repairs."),
    ] = False,
) -> None:
    """Inspect RiftX readiness and optionally apply bounded local repairs."""

    state = _state(context)
    runtime_config_path = _doctor_runtime_config_path(state)
    report = run_local_doctor(
        state.config,
        runtime_config_path=runtime_config_path,
    )
    with APIClient(state.api_url, timeout_seconds=3) as client:
        if fix:
            persistence_fix = _requires_stopped_control_plane(report)
            control_plane_reachable = False
            if persistence_fix:
                control_plane_reachable = _control_plane_reachable(client)
            try:
                fixes = apply_local_doctor_fixes(
                    state.config,
                    report,
                    runtime_config_path=runtime_config_path,
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
            report = run_local_doctor(
                state.config,
                runtime_config_path=runtime_config_path,
            )
        report = run_live_doctor(state.config, report, client)
    render_doctor_report(console, report)
    if report.failed:
        raise typer.Exit(1)


def _doctor_runtime_config_path(state: CLIState) -> Path | None:
    if state.config_path is not None:
        return state.config_path
    user_path = default_user_config_path()
    return user_path if user_path.exists() or user_path.is_symlink() else None


def _requires_stopped_control_plane(report: DoctorReport) -> bool:
    return any(
        check.id in {"config_migrations", "database_migrations", "pack_integrity"}
        and check.fixable
        for check in report.checks
    )


def _control_plane_reachable(client: APIClient) -> bool:
    try:
        client.health()
    except httpx.TransportError:
        return False
    except Exception:
        return True
    return True


@app.command(rich_help_panel=_SERVICE_PANEL)
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

    from riftx.api import APISettings, create_app

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


@app.command(rich_help_panel=_SERVICE_PANEL)
def worker(context: typer.Context) -> None:
    """Start the production Temporal Worker."""

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_temporal_worker(_state(context).config))


@app.command("runner", rich_help_panel=_SERVICE_PANEL)
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

    from riftx.runner.daemon import RunnerDaemonConfig, run_runner_daemon

    config = _state(context).config
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
            )
        )
    )


@app.command(rich_help_panel=_SERVICE_PANEL)
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


@demo_app.command("pentest")
def demo_pentest(context: typer.Context) -> None:
    """Play an offline authorized-pentest transcript without touching a target."""

    from riftx.demo import DemoError, run_pentest_demo

    try:
        result = run_pentest_demo(_state(context).config)
    except DemoError as exc:
        console.print(f"[red]Pentest Demo failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print("[bold]SANITIZED OFFLINE PENTEST DEMO[/bold]")
    console.print(f"Target: {result.target} (reserved, never contacted)")
    console.print("Official Packs: " + ", ".join(result.pack_ids))
    for step in result.steps:
        console.print(f"- {step.activity} [{step.pack_id}]: {step.evidence}", markup=False)
    if result.available_optional_tools:
        console.print(
            "Optional tools available: " + ", ".join(result.available_optional_tools)
        )
    if result.unavailable_optional_tools:
        console.print(
            "Optional tools unavailable: " + ", ".join(result.unavailable_optional_tools)
        )
        console.print("Degradation path: " + result.degradation_path)
    if result.tool_config_issue:
        console.print("Tool configuration note: " + result.tool_config_issue, markup=False)


@capabilities_app.command("list")
def list_capabilities(context: typer.Context) -> None:
    """List active Capability versions from local authoritative persistence."""

    state = _capability_state(context)
    table = Table(title=f"{len(state.capabilities)} active capabilities", expand=True)
    table.add_column("Capability")
    table.add_column("Version")
    table.add_column("Kind")
    table.add_column("Source")
    table.add_column("Trust")
    for item in state.capabilities:
        table.add_row(
            item.capability_id,
            item.version,
            item.kind,
            item.source,
            item.trust_tier,
        )
    console.print(table)


@capabilities_app.command("verify")
def verify_capabilities(context: typer.Context) -> None:
    """Verify Official Capability versions, Packs, installs, and active locks."""

    state = _capability_state(context)
    console.print(
        f"Capability verification: {state.verification_status}; "
        f"{len(state.capabilities)} active capabilities; {len(state.packs)} Official Packs."
    )
    for issue in state.issues:
        console.print(f"- {issue}", markup=False)
    if state.verification_status != "ready":
        raise typer.Exit(1)


@packs_app.command("list")
def list_packs(context: typer.Context) -> None:
    """List packaged Official Packs and their persisted status."""

    state = _capability_state(context)
    table = Table(title=f"{len(state.packs)} Official Packs", expand=True)
    table.add_column("Pack")
    table.add_column("Version")
    table.add_column("Capabilities", justify="right")
    table.add_column("Persistence")
    for item in state.packs:
        table.add_row(
            item.pack_id,
            item.version,
            str(item.capability_count),
            item.persistence_status,
        )
    console.print(table)


@skills_app.command("validate")
def validate_skills(
    context: typer.Context,
    skill_id: Annotated[
        str | None,
        typer.Argument(help="Optional Operator Skill ID."),
    ] = None,
) -> None:
    """Validate Operator Skill packages without changing persistence."""

    from riftx import capability_management

    documents = _operator_skill_action(
        context,
        lambda config: capability_management.validate_operator_skills(config, skill_id),
    )
    console.print(f"Validated {len(documents)} Operator Skill package(s).")
    for document in documents:
        console.print(
            f"- {document.id} {document.version} {document.digest[:12]}",
            markup=False,
        )


@skills_app.command("register")
def register_skill(
    context: typer.Context,
    skill_id: Annotated[str, typer.Argument(help="Operator Skill ID.")],
) -> None:
    """Register the current Operator Skill package as approved."""

    from riftx import capability_management

    version = _operator_skill_action(
        context,
        lambda config: capability_management.register_operator_skill(config, skill_id),
    )
    console.print(
        f"Registered {skill_id} {version.manifest.version} as {version.status.value}; "
        f"source digest {version.manifest.provenance.source_digest}.",
        markup=False,
    )


@skills_app.command("activate")
def activate_skill(
    context: typer.Context,
    skill_id: Annotated[str, typer.Argument(help="Operator Skill ID.")],
    version: Annotated[str, typer.Argument(help="Registered Skill version.")],
) -> None:
    """Activate a registered Operator Skill for new Pentest Runs."""

    from riftx import capability_management

    activated = _operator_skill_action(
        context,
        lambda config: capability_management.activate_operator_skill(
            config, skill_id, version
        ),
    )
    console.print(
        f"Activated {skill_id} {activated.manifest.version}; new Pentest Runs may select it."
    )


@skills_app.command("disable")
def disable_skill(
    context: typer.Context,
    skill_id: Annotated[str, typer.Argument(help="Operator Skill ID.")],
    version: Annotated[
        str | None,
        typer.Argument(help="Version to disable; defaults to the active version."),
    ] = None,
) -> None:
    """Disable an Operator Skill version for new Pentest Runs."""

    from riftx import capability_management

    disabled = _operator_skill_action(
        context,
        lambda config: capability_management.disable_operator_skill(
            config, skill_id, version
        ),
    )
    console.print(
        f"Disabled {skill_id} {disabled.manifest.version}; existing Run snapshots are unchanged."
    )


@skills_app.command("rollback")
def rollback_skill(
    context: typer.Context,
    skill_id: Annotated[str, typer.Argument(help="Operator Skill ID.")],
    version: Annotated[str, typer.Argument(help="Restored registered Skill version.")],
) -> None:
    """Activate a restored old Operator Skill version."""

    from riftx import capability_management

    rolled_back = _operator_skill_action(
        context,
        lambda config: capability_management.rollback_operator_skill(
            config, skill_id, version
        ),
    )
    console.print(
        f"Rolled back {skill_id} to {rolled_back.manifest.version}; "
        "new Pentest Runs use the restored source package."
    )


@skills_app.command("list")
def list_skills(
    context: typer.Context,
    skill_id: Annotated[
        str | None,
        typer.Argument(help="Optional Operator Skill ID."),
    ] = None,
) -> None:
    """List local Operator Skill packages and registered versions."""

    from riftx import capability_management

    items = _operator_skill_action(
        context,
        lambda config: capability_management.inspect_operator_skills(config, skill_id),
    )
    table = Table(title=f"{len(items)} Operator Skill version(s)", expand=True)
    table.add_column("Skill")
    table.add_column("Version")
    table.add_column("Capability")
    table.add_column("Source")
    table.add_column("Digest")
    for item in items:
        table.add_row(
            item.skill_id,
            item.version,
            item.capability_status,
            item.source_status,
            item.source_digest[:12],
        )
    console.print(table)


def _capability_state(context: typer.Context) -> LocalCapabilityState:
    from riftx.capability_management import (
        CapabilityManagementError,
        inspect_local_capability_state,
    )

    try:
        return inspect_local_capability_state(_state(context).config)
    except CapabilityManagementError as exc:
        console.print(f"[red]Capability inspection failed:[/red] {exc}")
        raise typer.Exit(1) from exc


def _operator_skill_action[T](
    context: typer.Context,
    action: Callable[[RiftXConfig], T],
) -> T:
    from riftx.capability_management import CapabilityManagementError

    try:
        return action(_state(context).config)
    except CapabilityManagementError as exc:
        console.print(f"[red]Operator Skill operation failed:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command("approvals", rich_help_panel=_PENTEST_PANEL)
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


@app.command("approve", rich_help_panel=_PENTEST_PANEL)
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


@app.command("reject", rich_help_panel=_PENTEST_PANEL)
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


@app.command("attach", rich_help_panel=_ADVANCED_PANEL)
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


@pentest_app.command("start")
def start_pentest(
    context: typer.Context,
    objective: Annotated[
        str,
        typer.Option("--objective", help="Authorized Pentest objective."),
    ],
    authorization: Annotated[
        str,
        typer.Option("--authorization", help="Ticket, contract, or authorization reference."),
    ],
    target: Annotated[
        list[str] | None,
        typer.Option(
            "--target",
            help="Repeatable target URL, domain, IP, CIDR, or explicit KIND=VALUE.",
        ),
    ] = None,
    scope: Annotated[
        list[str] | None,
        typer.Option(
            "--scope",
            help="Repeatable authorized URL prefix, domain, IP, or CIDR.",
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Repeatable explicit Scope exclusion."),
    ] = None,
    engagement_name: Annotated[
        str | None,
        typer.Option("--engagement", help="Engagement display name."),
    ] = None,
    node_id: Annotated[str | None, typer.Option("--node", help="Execution node ID.")] = None,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Workspace path visible to the Worker."),
    ] = None,
    approval_mode: Annotated[
        ApprovalMode,
        typer.Option("--mode", case_sensitive=False, help="Approval mode."),
    ] = ApprovalMode.BALANCED,
    model_profile: Annotated[
        str | None,
        typer.Option("--model", help="Model profile for this Pentest."),
    ] = None,
    success: Annotated[
        list[str] | None,
        typer.Option("--success", help="Repeatable required success criterion."),
    ] = None,
    pack: Annotated[
        list[str] | None,
        typer.Option("--pack", help="Repeatable official Capability Pack ID."),
    ] = None,
    tool: Annotated[
        list[str] | None,
        typer.Option("--tool", help="Repeatable Tool capability ID."),
    ] = None,
    skill: Annotated[
        list[str] | None,
        typer.Option("--skill", help="Repeatable Skill capability ID."),
    ] = None,
    technique: Annotated[
        list[str] | None,
        typer.Option("--technique", help="Repeatable Technique capability ID."),
    ] = None,
    request_id: Annotated[
        str | None,
        typer.Option("--request-id", help="UUID reused for an explicit retry."),
    ] = None,
    max_duration_seconds: Annotated[
        int,
        typer.Option("--max-duration", min=1, help="Maximum elapsed seconds."),
    ] = 900,
    max_model_calls: Annotated[
        int,
        typer.Option("--max-model-calls", min=1),
    ] = 20,
    max_tokens: Annotated[int, typer.Option("--max-tokens", min=1)] = 100_000,
    max_tool_calls: Annotated[
        int,
        typer.Option("--max-tool-calls", min=1),
    ] = 50,
    max_target_interactions: Annotated[
        int,
        typer.Option("--max-target-interactions", min=1),
    ] = 100,
    max_concurrent_target_interactions: Annotated[
        int,
        typer.Option("--max-target-concurrency", min=1),
    ] = 2,
) -> None:
    """Admit and start one authorized Pentest Run."""

    targets = target or []
    scopes = scope or []
    if not targets:
        raise typer.BadParameter("at least one target is required", param_hint="--target")
    if not scopes:
        raise typer.BadParameter("at least one Scope value is required", param_hint="--scope")
    authorization_reference = authorization.strip()
    if not authorization_reference:
        raise typer.BadParameter(
            "authorization reference must not be blank",
            param_hint="--authorization",
        )
    payload: dict[str, object] = {
        "request_id": request_id or str(uuid4()),
        "objective": objective,
        "approval_mode": approval_mode.value,
        "success_criteria": [
            {"description": item, "required": True} for item in (success or [])
        ],
        "entry_points": [_parse_pentest_target(item) for item in targets],
        "scope": _parse_pentest_scope(scopes, exclusions=exclude or []),
        "admission": {
            "budget": {
                "max_duration_seconds": max_duration_seconds,
                "max_model_calls": max_model_calls,
                "max_tokens": max_tokens,
                "max_tool_calls": max_tool_calls,
                "max_target_interactions": max_target_interactions,
                "max_concurrent_target_interactions": (
                    max_concurrent_target_interactions
                ),
            }
        },
        "engagement": {
            "name": engagement_name or f"Pentest: {targets[0]}",
            "authorization_reference": authorization_reference,
        },
        "capabilities": {
            "pack_ids": pack if pack is not None else ["pentest-foundation"],
            "tool_ids": tool or [],
            "skill_ids": skill or [],
            "technique_ids": technique or [],
        },
    }
    if node_id:
        payload["node_id"] = node_id
    if workspace:
        payload["workspace_path"] = workspace
    if model_profile:
        payload["model_profile"] = model_profile

    def operation(client: APIClient) -> None:
        run = client.create_pentest(payload)
        run_id = str(run.get("id") or "").strip()
        if not run_id:
            raise ValueError("Pentest creation response did not include a Run ID")
        render_pentest_status(console, client.get_pentest_status(run_id))
        console.print(f"[green]{tr('Pentest admitted and started.')}[/green]")

    _run_with_client(context, operation)


@pentest_app.command("status")
def show_pentest_status(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Pentest Run ID.")],
) -> None:
    """Show the durable Pentest admission, usage, execution, and stop state."""

    _run_with_client(
        context,
        lambda client: render_pentest_status(
            console,
            client.get_pentest_status(run_id),
        ),
    )


@pentest_app.command("resume")
def resume_pentest(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Pentest Run ID.")],
) -> None:
    """Resume an admitted Pentest Run through the shared Run control path."""

    def operation(client: APIClient) -> None:
        client.get_pentest_status(run_id)
        client.resume_run(run_id)
        render_pentest_status(console, client.get_pentest_status(run_id))
        console.print(f"[green]{tr('Pentest resume requested.')}[/green]")

    _run_with_client(context, operation)


@pentest_app.command("stop")
def stop_pentest(
    context: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Pentest Run ID.")],
) -> None:
    """Cancel a Pentest Run and display its durable stop proof."""

    def operation(client: APIClient) -> None:
        client.get_pentest_status(run_id)
        client.cancel_run(run_id)
        render_pentest_status(console, client.get_pentest_status(run_id))
        console.print(f"[green]{tr('Pentest stop confirmed.')}[/green]")

    _run_with_client(context, operation)


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
    from riftx.temporal.worker_runtime import build_temporal_worker

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


def _parse_pentest_target(value: str) -> dict[str, str]:
    item = value.strip()
    if not item:
        raise typer.BadParameter("Pentest targets must not be blank")
    if "=" in item:
        parsed = _parse_entry_point(item)
        if parsed["kind"] not in {
            EntryPointKind.CIDR.value,
            EntryPointKind.IP.value,
            EntryPointKind.DOMAIN.value,
            EntryPointKind.URL.value,
        }:
            raise typer.BadParameter("Pentest targets must be CIDR, IP, Domain, or URL")
        return parsed
    if "://" in item:
        return {"kind": EntryPointKind.URL.value, "value": item}
    try:
        return {
            "kind": EntryPointKind.IP.value,
            "value": str(ipaddress.ip_address(item)),
        }
    except ValueError:
        pass
    if "/" in item:
        try:
            return {
                "kind": EntryPointKind.CIDR.value,
                "value": str(ipaddress.ip_network(item, strict=False)),
            }
        except ValueError as exc:
            raise typer.BadParameter(f"invalid Pentest target: {item}") from exc
    return {"kind": EntryPointKind.DOMAIN.value, "value": item}


def _parse_pentest_scope(
    values: list[str],
    *,
    exclusions: list[str],
) -> dict[str, object]:
    result: dict[str, list[str]] = {
        "cidrs": [],
        "ips": [],
        "domains": [],
        "url_prefixes": [],
        "asset_tags": [],
        "exclusions": list(dict.fromkeys(item.strip() for item in exclusions if item.strip())),
    }
    for raw in values:
        item = raw.strip()
        if not item:
            raise typer.BadParameter("Scope values must not be blank", param_hint="--scope")
        if "://" in item:
            result["url_prefixes"].append(item)
            continue
        try:
            result["ips"].append(str(ipaddress.ip_address(item)))
            continue
        except ValueError:
            pass
        if "/" in item:
            try:
                result["cidrs"].append(str(ipaddress.ip_network(item, strict=False)))
                continue
            except ValueError as exc:
                raise typer.BadParameter(
                    f"invalid Scope value: {item}",
                    param_hint="--scope",
                ) from exc
        result["domains"].append(item)
    return {key: list(dict.fromkeys(items)) for key, items in result.items()}


if __name__ == "__main__":
    app()
