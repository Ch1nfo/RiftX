"""Layered RiftX configuration with deterministic precedence."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from riftx.domain import ApprovalMode
from riftx.executors import EnvironmentMode
from riftx.tools import ExecutionPolicy


class SecretProvider(Protocol):
    """Lookup interface for environment, keyring, or external secret backends."""

    def get(self, name: str) -> str | None: ...


class EnvironmentSecretProvider:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def get(self, name: str) -> str | None:
        return self._environment.get(name)


class KeyringSecretProvider:
    """Optional OS keyring adapter; returns no value when keyring is unavailable."""

    def __init__(self, service_name: str = "riftx") -> None:
        self._service_name = service_name

    def get(self, name: str) -> str | None:
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError:
            return None
        return keyring.get_password(self._service_name, name)


def resolve_secret(name: str, providers: list[SecretProvider]) -> str | None:
    for provider in providers:
        value = provider.get(name)
        if value is not None:
            return value
    return None


class RiftXConfigError(ValueError):
    """Raised when a RiftX YAML or override layer is invalid."""


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_ConfigModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    sse_poll_interval_seconds: float = Field(default=0.5, gt=0)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)


class DatabaseConfig(_ConfigModel):
    url: str = "sqlite+aiosqlite:///./.riftx/riftx.db"


class TemporalConfig(_ConfigModel):
    target: str = "127.0.0.1:7233"
    namespace: str = "default"
    task_queue: str = "riftx-v2"
    workflow_id_prefix: str = "riftx-run"
    max_concurrent_activities: int = Field(default=20, ge=1)
    max_cached_workflows: int = Field(default=1000, ge=0)


class RunnerConfig(_ConfigModel):
    mode: str = "local"
    endpoint: str = "http://127.0.0.1:8790"
    node_id: str = "local"
    state_path: Path = Path(".riftx/runner")
    registration_token: str | None = None
    command_lease_seconds: float = Field(default=30.0, gt=0)
    node_offline_after_seconds: float = Field(default=30.0, gt=0)
    node_lost_after_seconds: float = Field(default=300.0, gt=0)


class ExecutionConfig(_ConfigModel):
    policy: ExecutionPolicy = ExecutionPolicy.OPEN
    default_timeout: float = Field(default=1800, gt=0)
    environment_mode: EnvironmentMode = EnvironmentMode.INHERIT


class ExecutionOutputConfig(_ConfigModel):
    max_inline_bytes: int = Field(default=32768, ge=1024)
    preview_head_bytes: int = Field(default=8192, ge=0)
    preview_tail_bytes: int = Field(default=8192, ge=0)
    max_context_tokens: int = Field(default=2000, ge=100)


class WorkspaceConfig(_ConfigModel):
    root: Path = Path(".riftx/workspaces")


class ApprovalConfig(_ConfigModel):
    default_mode: ApprovalMode = ApprovalMode.BALANCED


class ToolsConfig(_ConfigModel):
    path: Path = Path("configs/tools.example.yaml")


class WebConfig(_ConfigModel):
    dist_path: Path = Path("apps/web/dist")


class ModelsRuntimeConfig(_ConfigModel):
    path: Path = Path("configs/models.example.yaml")
    profile: str | None = None


class AgentConfig(_ConfigModel):
    max_history_items: int = Field(default=100, ge=1)
    max_turns: int = Field(default=10, ge=1, le=100)


class SubagentConfig(_ConfigModel):
    max_depth: int = Field(default=1, ge=1, le=1)
    max_parallel_per_run: int = Field(default=4, ge=1, le=64)
    max_total_per_run: int = Field(default=20, ge=1, le=1000)


class RiftXConfig(_ConfigModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    temporal: TemporalConfig = Field(default_factory=TemporalConfig)
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    execution_output: ExecutionOutputConfig = Field(default_factory=ExecutionOutputConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    models: ModelsRuntimeConfig = Field(default_factory=ModelsRuntimeConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    subagents: SubagentConfig = Field(default_factory=SubagentConfig)


_ENVIRONMENT_PATHS: dict[str, tuple[str, ...]] = {
    "RIFTX_SERVER_HOST": ("server", "host"),
    "RIFTX_SERVER_PORT": ("server", "port"),
    "RIFTX_CORS_ORIGINS": ("server", "cors_origins"),
    "RIFTX_SSE_POLL_INTERVAL_SECONDS": ("server", "sse_poll_interval_seconds"),
    "RIFTX_SSE_HEARTBEAT_SECONDS": ("server", "sse_heartbeat_seconds"),
    "RIFTX_DATABASE_URL": ("database", "url"),
    "RIFTX_TEMPORAL_ADDRESS": ("temporal", "target"),
    "RIFTX_TEMPORAL_NAMESPACE": ("temporal", "namespace"),
    "RIFTX_TEMPORAL_TASK_QUEUE": ("temporal", "task_queue"),
    "RIFTX_TEMPORAL_WORKFLOW_ID_PREFIX": ("temporal", "workflow_id_prefix"),
    "RIFTX_NODE_ID": ("runner", "node_id"),
    "RIFTX_RUNNER_STATE": ("runner", "state_path"),
    "RIFTX_RUNNER_REGISTRATION_TOKEN": ("runner", "registration_token"),
    "RIFTX_RUNNER_COMMAND_LEASE_SECONDS": ("runner", "command_lease_seconds"),
    "RIFTX_NODE_OFFLINE_AFTER_SECONDS": ("runner", "node_offline_after_seconds"),
    "RIFTX_NODE_LOST_AFTER_SECONDS": ("runner", "node_lost_after_seconds"),
    "RIFTX_EXECUTION_POLICY": ("execution", "policy"),
    "RIFTX_DEFAULT_TIMEOUT": ("execution", "default_timeout"),
    "RIFTX_ENVIRONMENT_MODE": ("execution", "environment_mode"),
    "RIFTX_WORKSPACE_ROOT": ("workspace", "root"),
    "RIFTX_DEFAULT_APPROVAL_MODE": ("approval", "default_mode"),
    "RIFTX_TOOLS_CONFIG": ("tools", "path"),
    "RIFTX_WEB_DIST": ("web", "dist_path"),
    "RIFTX_MODELS_CONFIG": ("models", "path"),
    "RIFTX_MODEL_PROFILE": ("models", "profile"),
    "RIFTX_AGENT_MAX_HISTORY_ITEMS": ("agent", "max_history_items"),
    "RIFTX_AGENT_MAX_TURNS": ("agent", "max_turns"),
    "RIFTX_SUBAGENT_MAX_PARALLEL_PER_RUN": ("subagents", "max_parallel_per_run"),
    "RIFTX_SUBAGENT_MAX_TOTAL_PER_RUN": ("subagents", "max_total_per_run"),
}


def default_system_config_path() -> Path:
    return Path("/etc/riftx/riftx.yaml")


def default_user_config_path(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    root = Path(env.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return root / "riftx" / "riftx.yaml"


def load_riftx_config(
    *,
    system_path: Path | None = None,
    user_path: Path | None = None,
    explicit_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, object] | None = None,
    run_overrides: Mapping[str, object] | None = None,
) -> RiftXConfig:
    """Load defaults < system < user < explicit < env < CLI < Run layers."""

    env = os.environ if environment is None else environment
    configured_explicit = explicit_path
    if configured_explicit is None and env.get("RIFTX_CONFIG"):
        configured_explicit = Path(env["RIFTX_CONFIG"]).expanduser()
    merged: dict[str, Any] = RiftXConfig().model_dump(mode="python")
    for path, required in (
        (system_path or default_system_config_path(), False),
        (user_path or default_user_config_path(env), False),
        (configured_explicit, configured_explicit is not None),
    ):
        if path is None:
            continue
        if not path.exists():
            if required:
                raise RiftXConfigError(f"configuration file does not exist: {path}")
            continue
        _deep_merge(merged, _read_yaml_mapping(path))
    _deep_merge(merged, _environment_layer(env))
    if cli_overrides:
        _deep_merge(merged, dict(cli_overrides))
    if run_overrides:
        _deep_merge(merged, dict(run_overrides))
    try:
        return RiftXConfig.model_validate(merged)
    except ValidationError as exc:
        raise RiftXConfigError(f"invalid RiftX configuration: {exc}") from exc


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise RiftXConfigError(f"could not load configuration {path}: {exc}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise RiftXConfigError(f"configuration root must be a mapping: {path}")
    _reject_plaintext_secrets(payload, path)
    return payload


def _environment_layer(environment: Mapping[str, str]) -> dict[str, Any]:
    layer: dict[str, Any] = {}
    for name, path in _ENVIRONMENT_PATHS.items():
        raw = environment.get(name)
        if raw is None:
            continue
        value: object = raw
        if name == "RIFTX_CORS_ORIGINS":
            value = [item.strip() for item in raw.split(",") if item.strip()]
        _set_nested(layer, path, value)
    return layer


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    current = target
    for item in path[:-1]:
        child = current.get(item)
        if not isinstance(child, dict):
            child = {}
            current[item] = child
        current = child
    current[path[-1]] = value


def _deep_merge(target: dict[str, Any], source: Mapping[str, object]) -> None:
    for key, value in source.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _deep_merge(current, value)
        else:
            target[key] = value


def _reject_plaintext_secrets(payload: Mapping[str, object], path: Path) -> None:
    runner = payload.get("runner")
    if isinstance(runner, Mapping) and runner.get("registration_token") not in {None, ""}:
        raise RiftXConfigError(
            f"runner.registration_token must come from a secret provider, not {path}"
        )
    for key, value in payload.items():
        normalized = key.lower()
        if (
            value is not None
            and value != ""
            and (normalized.endswith("api_key") or normalized.endswith("token"))
        ):
            raise RiftXConfigError(f"secret field {key!r} must not be stored in {path}")
        if isinstance(value, Mapping):
            _reject_plaintext_secrets(value, path)
