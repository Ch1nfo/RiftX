"""Layered RiftX configuration with deterministic precedence."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from riftx.domain import ApprovalMode, OperatorCapability, TrustProfile
from riftx.executors import EnvironmentMode
from riftx.security import (
    DeploymentProfileError,
    validate_runner_registration_credential,
)
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
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


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
    tls_enabled: bool = False
    tls_server_root_ca_path: Path | None = Field(default=None, exclude=True, repr=False)
    tls_server_name: str | None = Field(default=None, exclude=True, repr=False)
    tls_client_cert_path: Path | None = Field(default=None, exclude=True, repr=False)
    tls_client_private_key_path: Path | None = Field(default=None, exclude=True, repr=False)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_secure_connection(self) -> TemporalConfig:
        if self.api_key is not None and not self.api_key.get_secret_value().strip():
            raise ValueError("Temporal API key must not be empty")
        if (self.tls_client_cert_path is None) != (self.tls_client_private_key_path is None):
            raise ValueError(
                "tls_client_cert_path and tls_client_private_key_path must be configured together"
            )
        if not self.tls_enabled and any(
            value is not None
            for value in (
                self.tls_server_root_ca_path,
                self.tls_server_name,
                self.tls_client_cert_path,
                self.tls_client_private_key_path,
            )
        ):
            raise ValueError("Temporal TLS certificate and server-name settings require TLS")
        if not self.tls_enabled and self.api_key is not None:
            raise ValueError("Temporal API key authentication requires TLS")
        return self


class RunnerConfig(_ConfigModel):
    mode: str = "local"
    endpoint: str = "http://127.0.0.1:8790"
    node_id: str = "local"
    state_path: Path = Path(".riftx/runner")
    credential_path: Path = Path(".riftx/secrets/runner-credentials.json")
    registration_token: str | None = Field(default=None, exclude=True, repr=False)
    command_lease_seconds: float = Field(default=30.0, gt=0)
    node_offline_after_seconds: float = Field(default=30.0, gt=0)
    node_lost_after_seconds: float = Field(default=300.0, gt=0)

    @field_validator("registration_token")
    @classmethod
    def validate_registration_token(cls, value: str | None) -> str | None:
        try:
            return validate_runner_registration_credential(value)
        except DeploymentProfileError as exc:
            raise ValueError(str(exc)) from None


class ExecutionConfig(_ConfigModel):
    policy: ExecutionPolicy = ExecutionPolicy.REGISTERED_ONLY
    default_timeout: float = Field(default=1800, gt=0)
    environment_mode: EnvironmentMode = EnvironmentMode.INHERIT
    # Security-testing payloads may daemonize, create a new session, or fork
    # after their original leader exits.  Requiring a kernel ownership
    # boundary is therefore the safe production default; development hosts
    # without one must opt out explicitly and will never receive affirmative
    # complete-tree stop confirmation.
    require_containment: bool = True
    # A cgroup is an ownership boundary only when the executed payload cannot
    # administer the Runner's delegated subtree.  Linux launchers therefore
    # drop to this distinct numeric identity after joining the leaf and before
    # any target code is activated.
    payload_uid: int | None = Field(default=None, gt=0)
    payload_gid: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_payload_identity(self) -> ExecutionConfig:
        if (self.payload_uid is None) != (self.payload_gid is None):
            raise ValueError("payload_uid and payload_gid must be configured together")
        return self


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
    path: Path = Path("configs/tools.yaml")


class WebConfig(_ConfigModel):
    dist_path: Path = Path("apps/web/dist")


class ModelsRuntimeConfig(_ConfigModel):
    path: Path = Path("configs/models.yaml")
    secrets_path: Path = Path(".riftx/secrets/models.json")
    profile: str | None = None


class SecurityConfig(_ConfigModel):
    trust_profile: TrustProfile | None = None
    local_principal_path: Path = Path(".riftx/secrets/local-principal.json")
    local_operator_capabilities: frozenset[OperatorCapability] = Field(
        default_factory=lambda: frozenset(OperatorCapability)
    )
    admin_token: str | None = Field(default=None, exclude=True, repr=False)
    trust_proxy_auth: bool = False


class AgentConfig(_ConfigModel):
    max_history_items: int = Field(default=100, ge=1)
    max_turns: int = Field(default=10, ge=1, le=100)


class SubagentConfig(_ConfigModel):
    max_depth: int = Field(default=1, ge=1, le=1)
    max_parallel_per_run: int = Field(default=4, ge=1, le=64)
    max_total_per_run: int = Field(default=20, ge=1, le=1000)


class HooksConfig(_ConfigModel):
    default_timeout_seconds: float = Field(default=10, gt=0)
    failure_policy: str = Field(default="warn", pattern="^(warn|block)$")


class MCPCircuitBreakerConfig(_ConfigModel):
    failure_threshold: int = Field(default=3, ge=1, le=1000)
    cooldown_seconds: float = Field(default=60, gt=0)


class MCPConfig(_ConfigModel):
    max_concurrent_per_server: int = Field(default=2, ge=1, le=1000)
    max_concurrent_total: int = Field(default=16, ge=1, le=10_000)
    circuit_breaker: MCPCircuitBreakerConfig = Field(default_factory=MCPCircuitBreakerConfig)


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
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    subagents: SubagentConfig = Field(default_factory=SubagentConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)


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
    "RIFTX_TEMPORAL_TLS_ENABLED": ("temporal", "tls_enabled"),
    "RIFTX_TEMPORAL_TLS_SERVER_ROOT_CA_PATH": (
        "temporal",
        "tls_server_root_ca_path",
    ),
    "RIFTX_TEMPORAL_TLS_SERVER_NAME": ("temporal", "tls_server_name"),
    "RIFTX_TEMPORAL_TLS_CLIENT_CERT_PATH": ("temporal", "tls_client_cert_path"),
    "RIFTX_TEMPORAL_TLS_CLIENT_PRIVATE_KEY_PATH": (
        "temporal",
        "tls_client_private_key_path",
    ),
    "RIFTX_TEMPORAL_API_KEY": ("temporal", "api_key"),
    "RIFTX_NODE_ID": ("runner", "node_id"),
    "RIFTX_RUNNER_STATE": ("runner", "state_path"),
    "RIFTX_RUNNER_CREDENTIALS": ("runner", "credential_path"),
    "RIFTX_RUNNER_REGISTRATION_TOKEN": ("runner", "registration_token"),
    "RIFTX_RUNNER_COMMAND_LEASE_SECONDS": ("runner", "command_lease_seconds"),
    "RIFTX_NODE_OFFLINE_AFTER_SECONDS": ("runner", "node_offline_after_seconds"),
    "RIFTX_NODE_LOST_AFTER_SECONDS": ("runner", "node_lost_after_seconds"),
    "RIFTX_EXECUTION_POLICY": ("execution", "policy"),
    "RIFTX_DEFAULT_TIMEOUT": ("execution", "default_timeout"),
    "RIFTX_ENVIRONMENT_MODE": ("execution", "environment_mode"),
    "RIFTX_REQUIRE_CONTAINMENT": ("execution", "require_containment"),
    "RIFTX_PAYLOAD_UID": ("execution", "payload_uid"),
    "RIFTX_PAYLOAD_GID": ("execution", "payload_gid"),
    "RIFTX_WORKSPACE_ROOT": ("workspace", "root"),
    "RIFTX_DEFAULT_APPROVAL_MODE": ("approval", "default_mode"),
    "RIFTX_TOOLS_CONFIG": ("tools", "path"),
    "RIFTX_WEB_DIST": ("web", "dist_path"),
    "RIFTX_MODELS_CONFIG": ("models", "path"),
    "RIFTX_MODEL_SECRETS": ("models", "secrets_path"),
    "RIFTX_MODEL_PROFILE": ("models", "profile"),
    "RIFTX_ADMIN_TOKEN": ("security", "admin_token"),
    "RIFTX_TRUST_PROFILE": ("security", "trust_profile"),
    "RIFTX_LOCAL_PRINCIPAL_PATH": ("security", "local_principal_path"),
    "RIFTX_LOCAL_OPERATOR_CAPABILITIES": (
        "security",
        "local_operator_capabilities",
    ),
    "RIFTX_TRUST_PROXY_AUTH": ("security", "trust_proxy_auth"),
    "RIFTX_AGENT_MAX_HISTORY_ITEMS": ("agent", "max_history_items"),
    "RIFTX_AGENT_MAX_TURNS": ("agent", "max_turns"),
    "RIFTX_SUBAGENT_MAX_PARALLEL_PER_RUN": ("subagents", "max_parallel_per_run"),
    "RIFTX_SUBAGENT_MAX_TOTAL_PER_RUN": ("subagents", "max_total_per_run"),
    "RIFTX_HOOK_DEFAULT_TIMEOUT_SECONDS": ("hooks", "default_timeout_seconds"),
    "RIFTX_HOOK_FAILURE_POLICY": ("hooks", "failure_policy"),
    "RIFTX_MCP_MAX_CONCURRENT_PER_SERVER": ("mcp", "max_concurrent_per_server"),
    "RIFTX_MCP_MAX_CONCURRENT_TOTAL": ("mcp", "max_concurrent_total"),
    "RIFTX_MCP_FAILURE_THRESHOLD": ("mcp", "circuit_breaker", "failure_threshold"),
    "RIFTX_MCP_COOLDOWN_SECONDS": ("mcp", "circuit_breaker", "cooldown_seconds"),
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
        _reject_temporal_api_key_override(cli_overrides, "CLI overrides")
        _deep_merge(merged, dict(cli_overrides))
    if run_overrides:
        _reject_temporal_api_key_override(run_overrides, "Run overrides")
        _deep_merge(merged, dict(run_overrides))
    try:
        return RiftXConfig.model_validate(merged)
    except ValidationError as exc:
        raise RiftXConfigError(
            f"invalid RiftX configuration: {_validation_error_summary(exc)}"
        ) from exc


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text()
    except OSError as exc:
        raise RiftXConfigError(f"could not read configuration {path}") from exc
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        column = getattr(mark, "column", None)
        location = (
            f" at line {line + 1}, column {column + 1}"
            if isinstance(line, int) and isinstance(column, int)
            else ""
        )
        raise RiftXConfigError(f"invalid YAML in configuration {path}{location}") from exc
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
        elif name == "RIFTX_LOCAL_OPERATOR_CAPABILITIES":
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


def _reject_temporal_api_key_override(
    payload: Mapping[str, object],
    source: str,
) -> None:
    temporal = payload.get("temporal")
    api_key = temporal.get("api_key") if isinstance(temporal, Mapping) else None
    if api_key is not None and api_key != "":
        raise RiftXConfigError(
            f"temporal.api_key must come from RIFTX_TEMPORAL_API_KEY, not {source}"
        )


def _validation_error_summary(error: ValidationError) -> str:
    summaries: list[str] = []
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(str(part) for part in item.get("loc", ())) or "configuration"
        message = str(item.get("msg", "invalid value"))
        summaries.append(f"{location}: {message}")
    return "; ".join(summaries) or "validation failed"
