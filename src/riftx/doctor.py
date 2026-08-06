"""Read-only local readiness checks for the RiftX CLI."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from riftx.config import RiftXConfig
from riftx.models.config import ModelConfigError, load_models_config
from riftx.packs import OfficialPackBundle, OfficialPackCatalog
from riftx.skills import ProgressiveSkillRegistry, SkillDocumentError
from riftx.tools.config import ToolConfigError, load_tool_config


class DoctorStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class DoctorLiveClient(Protocol):
    def health(self) -> dict[str, object]: ...

    def get_node(self, node_id: str) -> dict[str, object]: ...

    def list_tools(self, node_id: str) -> dict[str, object]: ...

    def system_diagnostics(self) -> dict[str, object]: ...


DOCTOR_CHECK_IDS = (
    "model_provider",
    "temporal",
    "runner",
    "browser",
    "tools",
    "skills",
    "mcp",
    "lsp",
    "scanner",
    "storage_permissions",
    "pack_integrity",
    "database_migrations",
    "backup_restore",
)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    id: str
    status: DoctorStatus
    detail: str
    remediation: str = ""
    fixable: bool = False


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def status(self) -> DoctorStatus:
        statuses = {check.status for check in self.checks}
        if DoctorStatus.FAILED in statuses:
            return DoctorStatus.FAILED
        if DoctorStatus.DEGRADED in statuses:
            return DoctorStatus.DEGRADED
        return DoctorStatus.READY

    @property
    def failed(self) -> bool:
        return self.status is DoctorStatus.FAILED

    def by_id(self, check_id: str) -> DoctorCheck:
        return next(check for check in self.checks if check.id == check_id)


def run_local_doctor(
    config: RiftXConfig,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    official_pack_catalog: OfficialPackCatalog | None = None,
) -> DoctorReport:
    """Inspect deterministic local configuration without changing host state."""

    env = os.environ if environment is None else environment
    root = Path.cwd() if cwd is None else cwd
    catalog = official_pack_catalog or OfficialPackCatalog()
    try:
        official_packs = catalog.load()
        pack_error: Exception | None = None
    except Exception as exc:  # diagnostic boundary: report malformed packaged input
        official_packs = ()
        pack_error = exc

    checks = (
        _check_model_provider(config, env, root),
        _check_temporal(config, root),
        _check_runner(config, root),
        DoctorCheck(
            id="browser",
            status=DoctorStatus.DEGRADED,
            detail="Browser readiness requires a live Runner probe.",
            remediation="Start the Control Plane and Runner before using browser workflows.",
        ),
        _check_tools(config, root),
        _check_skills(config, root, official_packs, pack_error),
        _check_mcp(config, env),
        _check_lsp(config, env),
        DoctorCheck(
            id="scanner",
            status=DoctorStatus.DEGRADED,
            detail=(
                "Built-in static audit is available; optional Scanner adapters "
                "are not configured."
            ),
            remediation=(
                "Use built-in static analysis or install a supported Scanner adapter later."
            ),
        ),
        _check_storage(config, root),
        _check_pack_integrity(official_packs, pack_error),
        _check_database(config, root),
        DoctorCheck(
            id="backup_restore",
            status=DoctorStatus.DEGRADED,
            detail="Backup and restore verification is not available yet.",
            remediation="Keep external backups until RiftX backup/restore support is installed.",
        ),
    )
    return DoctorReport(checks=checks)


def run_live_doctor(
    config: RiftXConfig,
    report: DoctorReport,
    client: DoctorLiveClient,
) -> DoctorReport:
    """Overlay read-only Control Plane evidence onto an offline Doctor report."""

    try:
        health = client.health()
    except Exception as exc:  # network/client boundary
        return _replace_checks(
            report,
            {
                "runner": DoctorCheck(
                    id="runner",
                    status=DoctorStatus.FAILED,
                    detail=f"Control Plane is unreachable: {exc}",
                    remediation="Start the RiftX Control Plane and retry `riftx doctor`.",
                )
            },
        )
    if health.get("status") != "ok":
        return _replace_checks(
            report,
            {
                "runner": DoctorCheck(
                    id="runner",
                    status=DoctorStatus.FAILED,
                    detail="Control Plane health response is not ready.",
                    remediation="Inspect Control Plane logs and retry `riftx doctor`.",
                )
            },
        )

    try:
        node = client.get_node(config.runner.node_id)
    except Exception as exc:  # API boundary
        node_updates = {
            "runner": DoctorCheck(
                id="runner",
                status=DoctorStatus.FAILED,
                detail=f"Runner {config.runner.node_id!r} is unavailable: {exc}",
                remediation="Start or register the configured Runner.",
            )
        }
    else:
        node_updates = _live_node_checks(config, node)

    try:
        tools = client.list_tools(config.runner.node_id)
    except Exception as exc:  # API boundary
        tool_check = DoctorCheck(
            id="tools",
            status=DoctorStatus.FAILED,
            detail=f"Live Tool Registry is unavailable: {exc}",
            remediation="Restore the Runner Tool Registry and retry `riftx doctor`.",
        )
    else:
        tool_check = _live_tool_check(tools)
    try:
        diagnostics = client.system_diagnostics()
    except Exception:
        system_updates: dict[str, DoctorCheck] = {}
    else:
        system_updates = _live_system_checks(diagnostics)
    return _replace_checks(
        report,
        {**node_updates, "tools": tool_check, **system_updates},
    )


def _replace_checks(
    report: DoctorReport,
    replacements: Mapping[str, DoctorCheck],
) -> DoctorReport:
    return DoctorReport(
        checks=tuple(replacements.get(check.id, check) for check in report.checks)
    )


def _live_node_checks(
    config: RiftXConfig,
    node: Mapping[str, object],
) -> dict[str, DoctorCheck]:
    status = str(node.get("status", "unknown"))
    version = str(node.get("runner_version", "unknown"))
    if status == "online":
        runner = DoctorCheck(
            id="runner",
            status=DoctorStatus.READY,
            detail=f"Runner {config.runner.node_id!r} is online (version {version}).",
        )
    elif status == "degraded":
        runner = DoctorCheck(
            id="runner",
            status=DoctorStatus.DEGRADED,
            detail=f"Runner {config.runner.node_id!r} reports degraded health.",
            remediation="Inspect Runner logs and active resource pressure.",
        )
    else:
        runner = DoctorCheck(
            id="runner",
            status=DoctorStatus.FAILED,
            detail=f"Runner {config.runner.node_id!r} is {status}.",
            remediation="Start or reconnect the configured Runner.",
        )

    labels = _string_mapping(node.get("labels"))
    capabilities = _string_set(node.get("capabilities"))
    updates = {"runner": runner}
    if status == "online" and labels.get("mode") == "worker-local":
        updates["temporal"] = DoctorCheck(
            id="temporal",
            status=DoctorStatus.READY,
            detail="The online production Worker provides current Temporal connectivity proof.",
        )
    if status == "online" and "browser_playwright" in capabilities:
        updates["browser"] = DoctorCheck(
            id="browser",
            status=DoctorStatus.READY,
            detail="The online Runner advertises Playwright browser capability.",
        )
    if any(server.enabled for server in config.mcp.servers.values()):
        updates["mcp"] = _live_mcp_check(labels)
    return updates


def _live_mcp_check(labels: Mapping[str, str]) -> DoctorCheck:
    refresh_status = labels.get("mcp_refresh_status")
    unavailable = _nonnegative_int(labels.get("mcp_unavailable_server_count"))
    open_circuits = _nonnegative_int(labels.get("mcp_open_circuit_count"))
    if refresh_status == "ready" and unavailable == 0 and open_circuits == 0:
        return DoctorCheck(
            id="mcp",
            status=DoctorStatus.READY,
            detail="Worker MCP discovery is current and all configured Servers are available.",
        )
    if refresh_status == "unavailable" or (unavailable is not None and unavailable > 0):
        return DoctorCheck(
            id="mcp",
            status=DoctorStatus.FAILED,
            detail="One or more enabled MCP Servers are unavailable.",
            remediation="Restore MCP Server connectivity and inspect Worker refresh logs.",
        )
    return DoctorCheck(
        id="mcp",
        status=DoctorStatus.DEGRADED,
        detail="MCP runtime health labels are incomplete or have open circuits.",
        remediation="Wait for Worker discovery refresh or inspect MCP circuit state.",
    )


def _live_tool_check(payload: Mapping[str, object]) -> DoctorCheck:
    raw_tools = payload.get("tools")
    if not isinstance(raw_tools, list):
        return DoctorCheck(
            id="tools",
            status=DoctorStatus.FAILED,
            detail="Live Tool Registry returned an invalid payload.",
            remediation="Upgrade or repair the Control Plane and Runner.",
        )
    enabled: list[tuple[str, Mapping[str, object], Mapping[str, object]]] = []
    for item in raw_tools:
        if not isinstance(item, Mapping):
            continue
        definition = item.get("definition")
        state = item.get("state")
        if (
            isinstance(definition, Mapping)
            and isinstance(state, Mapping)
            and definition.get("enabled") is True
        ):
            enabled.append((str(definition.get("id", "unknown")), definition, state))
    unavailable = sorted(
        tool_id
        for tool_id, _, state in enabled
        if state.get("availability") != "available"
    )
    if unavailable:
        return DoctorCheck(
            id="tools",
            status=DoctorStatus.FAILED,
            detail="Enabled tools are unavailable: " + ", ".join(unavailable),
            remediation="Install or repair the unavailable tools, then run `riftx tools doctor`.",
        )
    missing_versions = sorted(
        tool_id
        for tool_id, definition, state in enabled
        if definition.get("version_probe") is not None and not state.get("version")
    )
    if missing_versions:
        return DoctorCheck(
            id="tools",
            status=DoctorStatus.DEGRADED,
            detail="Tool versions were not resolved: " + ", ".join(missing_versions),
            remediation="Run `riftx tools doctor` after restoring each version probe.",
        )
    if not enabled:
        return DoctorCheck(
            id="tools",
            status=DoctorStatus.DEGRADED,
            detail="No external Runner tool is enabled; built-in code tools remain available.",
            remediation="Enable external tools only when the engagement requires them.",
        )
    return DoctorCheck(
        id="tools",
        status=DoctorStatus.READY,
        detail=f"All {len(enabled)} enabled Runner tools are available with required versions.",
    )


def _live_system_checks(payload: Mapping[str, object]) -> dict[str, DoctorCheck]:
    database = payload.get("database")
    packs = payload.get("official_packs")
    updates: dict[str, DoctorCheck] = {}
    if isinstance(database, Mapping):
        status = database.get("status")
        expected = str(database.get("expected_revision", "unknown"))
        current = _string_set(database.get("current_revisions"))
        if status == "ready":
            updates["database_migrations"] = DoctorCheck(
                id="database_migrations",
                status=DoctorStatus.READY,
                detail=f"Database revision matches Alembic head {expected}.",
            )
        else:
            observed = ", ".join(sorted(current)) or "unmanaged"
            updates["database_migrations"] = DoctorCheck(
                id="database_migrations",
                status=DoctorStatus.FAILED,
                detail=f"Database revision is {observed}; expected {expected}.",
                remediation="Back up the database and apply all Alembic migrations.",
                fixable=True,
            )
    if isinstance(packs, Mapping):
        if packs.get("status") == "ready":
            updates["pack_integrity"] = DoctorCheck(
                id="pack_integrity",
                status=DoctorStatus.READY,
                detail=(
                    f"{packs.get('installed_pack_count', 0)} Official Packs and "
                    f"{packs.get('active_lock_count', 0)} active locks match source digests."
                ),
            )
        else:
            issues = sorted(_string_set(packs.get("issues")))
            updates["pack_integrity"] = DoctorCheck(
                id="pack_integrity",
                status=DoctorStatus.FAILED,
                detail="Official Pack persistence drift: " + ", ".join(issues[:5]),
                remediation="Restore or reinstall Official Packs before starting new Runs.",
                fixable=True,
            )
    return updates


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _string_set(value: object) -> set[str]:
    if not isinstance(value, (list, tuple)):
        return set()
    return {item for item in value if isinstance(item, str)}


def _nonnegative_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _resolve(path: Path, cwd: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else cwd / expanded


def _check_model_provider(
    config: RiftXConfig,
    environment: Mapping[str, str],
    cwd: Path,
) -> DoctorCheck:
    path = _resolve(config.models.path, cwd)
    try:
        models = load_models_config(path)
    except ModelConfigError as exc:
        return DoctorCheck(
            id="model_provider",
            status=DoctorStatus.FAILED,
            detail=str(exc),
            remediation="Create or repair the configured models.yaml file.",
        )
    profile_name = config.models.profile or models.default_profile
    profile = models.models.get(profile_name)
    if profile is None:
        return DoctorCheck(
            id="model_provider",
            status=DoctorStatus.FAILED,
            detail=f"Configured model profile {profile_name!r} does not exist.",
            remediation="Select a profile declared in models.yaml.",
        )
    if profile.requires_api_key and (
        not profile.api_key_env or not environment.get(profile.api_key_env, "").strip()
    ):
        return DoctorCheck(
            id="model_provider",
            status=DoctorStatus.FAILED,
            detail=f"Model profile {profile_name!r} is missing its API credential.",
            remediation=(
                f"Set {profile.api_key_env or 'the configured API key environment variable'}."
            ),
        )
    return DoctorCheck(
        id="model_provider",
        status=DoctorStatus.READY,
        detail=(
            f"Model profile {profile_name!r} is valid and its credential reference "
            "is available."
        ),
    )


def _check_temporal(config: RiftXConfig, cwd: Path) -> DoctorCheck:
    tls_paths = tuple(
        path
        for path in (
            config.temporal.tls_server_root_ca_path,
            config.temporal.tls_client_cert_path,
            config.temporal.tls_client_private_key_path,
        )
        if path is not None
    )
    missing = [str(_resolve(path, cwd)) for path in tls_paths if not _resolve(path, cwd).is_file()]
    if missing:
        return DoctorCheck(
            id="temporal",
            status=DoctorStatus.FAILED,
            detail="Temporal TLS file is missing: " + ", ".join(missing),
            remediation="Restore the configured TLS files or update Temporal configuration.",
        )
    return DoctorCheck(
        id="temporal",
        status=DoctorStatus.DEGRADED,
        detail=f"Temporal configuration for {config.temporal.target!r} is valid but not probed.",
        remediation="Start Temporal and use a live Doctor probe to verify connectivity.",
    )


def _check_runner(config: RiftXConfig, cwd: Path) -> DoctorCheck:
    state_path = _resolve(config.runner.state_path, cwd)
    credential_path = _resolve(config.runner.credential_path, cwd)
    invalid = []
    if state_path.exists() and not state_path.is_dir():
        invalid.append(f"state path is not a directory: {state_path}")
    if credential_path.exists() and not credential_path.is_file():
        invalid.append(f"credential path is not a file: {credential_path}")
    if invalid:
        return DoctorCheck(
            id="runner",
            status=DoctorStatus.FAILED,
            detail="; ".join(invalid),
            remediation="Repair the Runner state and credential paths.",
        )
    return DoctorCheck(
        id="runner",
        status=DoctorStatus.DEGRADED,
        detail=f"Runner {config.runner.node_id!r} is configured; live status was not probed.",
        remediation="Start the Runner and use a live Doctor probe to verify registration.",
    )


def _check_tools(config: RiftXConfig, cwd: Path) -> DoctorCheck:
    path = _resolve(config.tools.path, cwd)
    try:
        tools = load_tool_config(path)
    except ToolConfigError as exc:
        return DoctorCheck(
            id="tools",
            status=DoctorStatus.FAILED,
            detail=str(exc),
            remediation="Create or repair the configured tools.yaml file.",
        )
    enabled = sum(tool.enabled for tool in tools.tools.values())
    return DoctorCheck(
        id="tools",
        status=DoctorStatus.DEGRADED,
        detail=f"Loaded {len(tools.tools)} tools ({enabled} enabled); versions were not probed.",
        remediation="Start the Runner and run `riftx tools doctor` to verify tool versions.",
    )


def _check_skills(
    config: RiftXConfig,
    cwd: Path,
    official_packs: tuple[OfficialPackBundle, ...],
    pack_error: Exception | None,
) -> DoctorCheck:
    if pack_error is not None:
        return DoctorCheck(
            id="skills",
            status=DoctorStatus.FAILED,
            detail=f"Official Pack skills are invalid: {pack_error}",
            remediation="Reinstall the RiftX package containing Official Packs.",
        )
    operator_root = _resolve(config.skills.path, cwd)
    if not operator_root.exists():
        return DoctorCheck(
            id="skills",
            status=DoctorStatus.DEGRADED,
            detail=(
                f"Validated {len(official_packs)} Official Packs; operator Skill root "
                "is absent."
            ),
            remediation="Create the operator Skill root when adding custom Skills.",
            fixable=True,
        )
    try:
        operator_skills = ProgressiveSkillRegistry(operator_root).validate()
    except (OSError, UnicodeError, SkillDocumentError, ValueError) as exc:
        return DoctorCheck(
            id="skills",
            status=DoctorStatus.FAILED,
            detail=f"Operator Skills are invalid: {exc}",
            remediation="Repair or remove the invalid operator Skill package.",
        )
    return DoctorCheck(
        id="skills",
        status=DoctorStatus.DEGRADED,
        detail=(
            f"Validated {len(official_packs)} Official Packs and "
            f"{len(operator_skills)} operator Skills; runtime dependencies were not probed."
        ),
        remediation="Use a live Doctor probe to verify Skill tool and service dependencies.",
    )


def _check_mcp(config: RiftXConfig, environment: Mapping[str, str]) -> DoctorCheck:
    enabled = {
        server_id: server for server_id, server in config.mcp.servers.items() if server.enabled
    }
    if not enabled:
        return DoctorCheck(
            id="mcp",
            status=DoctorStatus.DEGRADED,
            detail="No MCP Server is enabled; built-in tools remain available.",
            remediation="Configure MCP only when an external capability is required.",
        )
    missing = sorted(
        env_name
        for server in enabled.values()
        for env_name in server.header_env.values()
        if not environment.get(env_name, "").strip()
    )
    if missing:
        return DoctorCheck(
            id="mcp",
            status=DoctorStatus.FAILED,
            detail="Enabled MCP Server credentials are missing: " + ", ".join(missing),
            remediation="Set the missing MCP header environment variables.",
        )
    return DoctorCheck(
        id="mcp",
        status=DoctorStatus.DEGRADED,
        detail=(
            f"Validated {len(enabled)} enabled MCP Server configurations; "
            "discovery was not run."
        ),
        remediation="Start the MCP Servers and use a live Doctor probe to verify discovery.",
    )


def _check_lsp(config: RiftXConfig, environment: Mapping[str, str]) -> DoctorCheck:
    lsp = config.code.lsp
    if not lsp.enabled:
        return DoctorCheck(
            id="lsp",
            status=DoctorStatus.DEGRADED,
            detail="Controlled LSP is disabled; built-in static code navigation remains available.",
            remediation=(
                "Enable the controlled LSP gateway only when semantic navigation is required."
            ),
        )
    assert lsp.socket_path is not None
    assert lsp.token_env is not None
    if not lsp.socket_path.is_socket():
        return DoctorCheck(
            id="lsp",
            status=DoctorStatus.FAILED,
            detail=f"Controlled LSP socket is unavailable: {lsp.socket_path}",
            remediation="Start the trusted LSP gateway at the configured socket path.",
        )
    if not environment.get(lsp.token_env, "").strip():
        return DoctorCheck(
            id="lsp",
            status=DoctorStatus.FAILED,
            detail=f"Controlled LSP credential {lsp.token_env!r} is missing.",
            remediation=f"Set {lsp.token_env} before enabling controlled LSP.",
        )
    return DoctorCheck(
        id="lsp",
        status=DoctorStatus.DEGRADED,
        detail="Controlled LSP socket and credential exist; the handshake was not probed.",
        remediation="Use a live Doctor probe to verify the configured LSP backend identity.",
    )


def _check_storage(config: RiftXConfig, cwd: Path) -> DoctorCheck:
    paths = [("workspace.root", _resolve(config.workspace.root, cwd))]
    if config.audit.enabled:
        paths.extend(
            (label, path)
            for label, path in (
                ("audit.snapshot_root", config.audit.snapshot_root),
                ("audit.temp_root", config.audit.temp_root),
                ("audit.fix_root", config.audit.fix_root),
            )
        )
    missing: list[str] = []
    invalid: list[str] = []
    for label, path in paths:
        resolved = _resolve(path, cwd)
        if resolved.exists():
            if not resolved.is_dir() or not os.access(resolved, os.W_OK | os.X_OK):
                invalid.append(label)
            continue
        parent = _nearest_existing_parent(resolved)
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
            invalid.append(label)
        else:
            missing.append(label)
    if invalid:
        return DoctorCheck(
            id="storage_permissions",
            status=DoctorStatus.FAILED,
            detail="Storage path is unusable: " + ", ".join(invalid),
            remediation="Grant the RiftX process write and traversal access to these paths.",
        )
    if missing:
        return DoctorCheck(
            id="storage_permissions",
            status=DoctorStatus.DEGRADED,
            detail="Storage directory is not initialized: " + ", ".join(missing),
            remediation="Create the missing storage directories with owner-only permissions.",
            fixable=True,
        )
    return DoctorCheck(
        id="storage_permissions",
        status=DoctorStatus.READY,
        detail="Configured Snapshot and Artifact staging paths are writable.",
    )


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _check_pack_integrity(
    official_packs: tuple[OfficialPackBundle, ...],
    pack_error: Exception | None,
) -> DoctorCheck:
    if pack_error is not None:
        return DoctorCheck(
            id="pack_integrity",
            status=DoctorStatus.FAILED,
            detail=f"Official Pack digest validation failed: {pack_error}",
            remediation="Reinstall the RiftX package containing Official Packs.",
        )
    return DoctorCheck(
        id="pack_integrity",
        status=DoctorStatus.DEGRADED,
        detail=(
            f"Validated source digests for {len(official_packs)} Official Packs; "
            "DB locks were not read."
        ),
        remediation="Use a live Doctor probe to compare active Pack locks with source digests.",
    )


def _check_database(config: RiftXConfig, cwd: Path) -> DoctorCheck:
    try:
        url = make_url(config.database.url)
    except ArgumentError as exc:
        return DoctorCheck(
            id="database_migrations",
            status=DoctorStatus.FAILED,
            detail=f"Database URL is invalid: {exc}",
            remediation="Repair database.url in RiftX configuration.",
        )
    database = url.database
    if url.get_backend_name() == "sqlite" and database not in {None, "", ":memory:"}:
        assert database is not None
        database_path = _resolve(Path(database), cwd)
        if database_path.exists() and not database_path.is_file():
            return DoctorCheck(
                id="database_migrations",
                status=DoctorStatus.FAILED,
                detail=f"SQLite database path is not a file: {database_path}",
                remediation="Repair database.url or replace the invalid path.",
            )
        if not database_path.exists():
            return DoctorCheck(
                id="database_migrations",
                status=DoctorStatus.DEGRADED,
                detail="Database is not initialized; migration state is unavailable.",
                remediation="Initialize the database and apply all Alembic migrations.",
                fixable=True,
            )
    return DoctorCheck(
        id="database_migrations",
        status=DoctorStatus.DEGRADED,
        detail="Database configuration is valid; the live Alembic revision was not read.",
        remediation="Use a live Doctor probe to compare the database revision with Alembic head.",
    )


__all__ = [
    "DOCTOR_CHECK_IDS",
    "DoctorCheck",
    "DoctorReport",
    "DoctorStatus",
    "run_live_doctor",
    "run_local_doctor",
]
