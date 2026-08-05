"""Layered RiftX configuration with deterministic precedence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

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


class _AuditConfigModel(_ConfigModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        frozen=True,
    )


AUDIT_CONFIG_DIGEST_VERSION = "riftx.audit-config/v2"
AUDIT_SOURCE_INGEST_POLICY_VERSION = "riftx.audit-source-ingest-policy/v1"
_MAX_AUDIT_REPOSITORY_BYTES = 2_147_483_648
_MAX_AUDIT_FILE_BYTES = 5_242_880
_MAX_AUDIT_FILES = 200_000
_MAX_AUDIT_PATH_BYTES = 4_096
_MAX_AUDIT_DIRECTORY_DEPTH = 256
_MAX_AUDIT_ARTIFACT_BYTES = 67_108_864
_MAX_AUDIT_TOTAL_ARTIFACT_BYTES = 268_435_456
_MAX_AUDIT_PARALLEL_WORKERS = 4
_MAX_AUDIT_EPOCHS = 8
_MAX_AUDIT_SATURATION_EPOCHS = 2
_MAX_AUDIT_WALL_SECONDS = 7_200
_MAX_AUDIT_MODEL_CALLS = 100
_MAX_AUDIT_INPUT_TOKENS = 2_000_000
_MAX_AUDIT_OUTPUT_TOKENS = 200_000
_MAX_AUDIT_WORKER_JOBS = 64
_MAX_AUDIT_CANDIDATES = 1_000
_MAX_AUDIT_MODEL_BYTES_PER_CALL = 131_072
_MAX_AUDIT_MODEL_BYTES_PER_AUDIT = 16_777_216
_MAX_AUDIT_VALIDATION_WALL_SECONDS = 900
_MAX_AUDIT_VALIDATION_MEMORY_MIB = 2_048
_MAX_AUDIT_VALIDATION_PIDS = 128
_MAX_AUDIT_PREFLIGHT_WALL_SECONDS = 120
_MAX_AUDIT_PREFLIGHT_MEMORY_MIB = 512
_MAX_AUDIT_PREFLIGHT_PIDS = 32
_MAX_AUDIT_PREFLIGHT_RESULT_BYTES = 262_144
_MAX_AUDIT_PREFLIGHT_OUTPUT_BYTES = 1_048_576
_MAX_AUDIT_PREFLIGHT_LEASE_SECONDS = 120
_MAX_AUDIT_PREFLIGHT_TTL_SECONDS = 3_600
_DECIMAL_INTEGER_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_DNS_ORIGIN_LABEL_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


def _parse_audit_boolean(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError("must be the boolean true or false")


def _parse_audit_integer(value: object) -> object:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if _DECIMAL_INTEGER_PATTERN.fullmatch(normalized):
            return int(normalized)
    raise ValueError("must be a base-10 integer")


AuditBoolean = Annotated[bool, BeforeValidator(_parse_audit_boolean)]
AuditInteger = Annotated[int, BeforeValidator(_parse_audit_integer)]


def _canonical_source_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("Audit source roots must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Audit source root cannot be resolved") from exc
    if not resolved.is_dir():
        raise ValueError("Audit source root must resolve to a directory")
    return resolved


def _canonical_storage_path(
    path: Path,
    *,
    label: str,
    directory: bool,
) -> Path:
    resolved, _ = _storage_path_boundary(
        path,
        label=label,
        directory=directory,
    )
    return resolved


def _storage_path_boundary(
    path: Path,
    *,
    label: str,
    directory: bool,
) -> tuple[Path, Path]:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    candidate = path
    try:
        while not candidate.exists():
            if candidate.is_symlink():
                raise ValueError(f"{label} contains an unresolved symlink")
            parent = candidate.parent
            if parent == candidate:
                raise ValueError(f"{label} has no resolvable parent")
            candidate = parent
        if not candidate.is_dir() and candidate != path:
            raise ValueError(f"{label} has a non-directory parent")
        if candidate == path:
            if directory and not candidate.is_dir():
                raise ValueError(f"{label} must resolve to a directory")
            if not directory and candidate.is_dir():
                raise ValueError(f"{label} must resolve to a file path")
        return path.resolve(strict=False), candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} cannot be resolved") from exc


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _normalize_https_origin(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or "*" in value
        or "\\" in value
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("remote model origins must be explicit HTTPS origins")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("remote model origin is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("remote model origins must be origin-only HTTPS URLs")
    raw_host = parsed.hostname.rstrip(".")
    if not raw_host or "%" in raw_host:
        raise ValueError("remote model origin hostname is invalid")
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("remote model origin hostname is invalid") from exc
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or any(_DNS_ORIGIN_LABEL_PATTERN.fullmatch(label) is None for label in labels)
            or (host.replace(".", "").isdigit())
        ):
            raise ValueError("remote model origin hostname is invalid") from None
    else:
        host = address.compressed
        if address.version == 6:
            host = f"[{host}]"
    normalized_port = None if port == 443 else port
    return f"https://{host}{f':{normalized_port}' if normalized_port is not None else ''}"


class AuditModelEgressConfig(_AuditConfigModel):
    default_mode: Literal["local_only", "remote_redacted"] = "local_only"
    max_bytes_per_call: AuditInteger = Field(
        default=_MAX_AUDIT_MODEL_BYTES_PER_CALL,
        ge=1,
        le=_MAX_AUDIT_MODEL_BYTES_PER_CALL,
    )
    max_bytes_per_audit: AuditInteger = Field(
        default=_MAX_AUDIT_MODEL_BYTES_PER_AUDIT,
        ge=1,
        le=_MAX_AUDIT_MODEL_BYTES_PER_AUDIT,
    )
    allow_remote_origins: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("allow_remote_origins")
    @classmethod
    def validate_remote_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(_normalize_https_origin(value) for value in values))
        if len(normalized) != len(set(normalized)):
            raise ValueError("remote model origins must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_egress_limits(self) -> AuditModelEgressConfig:
        if self.max_bytes_per_call > self.max_bytes_per_audit:
            raise ValueError("max_bytes_per_call must not exceed max_bytes_per_audit")
        if self.default_mode == "remote_redacted" and not self.allow_remote_origins:
            raise ValueError("remote_redacted requires at least one allowed remote origin")
        return self


class AuditWorkersConfig(_AuditConfigModel):
    max_parallel: AuditInteger = Field(
        default=_MAX_AUDIT_PARALLEL_WORKERS,
        ge=1,
        le=_MAX_AUDIT_PARALLEL_WORKERS,
    )
    max_epochs: AuditInteger = Field(
        default=_MAX_AUDIT_EPOCHS,
        ge=1,
        le=_MAX_AUDIT_EPOCHS,
    )
    saturation_epochs: AuditInteger = Field(
        default=_MAX_AUDIT_SATURATION_EPOCHS,
        ge=1,
        le=_MAX_AUDIT_SATURATION_EPOCHS,
    )

    @model_validator(mode="after")
    def validate_epoch_limits(self) -> AuditWorkersConfig:
        if self.saturation_epochs > self.max_epochs:
            raise ValueError("saturation_epochs must not exceed max_epochs")
        return self


class AuditBudgetConfig(_AuditConfigModel):
    max_wall_seconds: AuditInteger = Field(
        default=_MAX_AUDIT_WALL_SECONDS,
        ge=1,
        le=_MAX_AUDIT_WALL_SECONDS,
    )
    max_model_calls: AuditInteger = Field(
        default=_MAX_AUDIT_MODEL_CALLS,
        ge=1,
        le=_MAX_AUDIT_MODEL_CALLS,
    )
    max_input_tokens: AuditInteger = Field(
        default=_MAX_AUDIT_INPUT_TOKENS,
        ge=1,
        le=_MAX_AUDIT_INPUT_TOKENS,
    )
    max_output_tokens: AuditInteger = Field(
        default=_MAX_AUDIT_OUTPUT_TOKENS,
        ge=1,
        le=_MAX_AUDIT_OUTPUT_TOKENS,
    )
    max_worker_jobs: AuditInteger = Field(
        default=_MAX_AUDIT_WORKER_JOBS,
        ge=1,
        le=_MAX_AUDIT_WORKER_JOBS,
    )
    max_candidates: AuditInteger = Field(
        default=_MAX_AUDIT_CANDIDATES,
        ge=1,
        le=_MAX_AUDIT_CANDIDATES,
    )


class AuditSandboxConfig(_AuditConfigModel):
    default_policy: Literal[
        "static_only",
        "isolated_build",
        "isolated_test",
        "isolated_poc",
        "isolated_fix_and_retest",
    ] = "static_only"
    require_sandbox: AuditBoolean = True
    default_network: Literal["none"] = "none"
    max_wall_seconds: AuditInteger = Field(
        default=_MAX_AUDIT_VALIDATION_WALL_SECONDS,
        ge=1,
        le=_MAX_AUDIT_VALIDATION_WALL_SECONDS,
    )
    max_memory_mib: AuditInteger = Field(
        default=_MAX_AUDIT_VALIDATION_MEMORY_MIB,
        ge=1,
        le=_MAX_AUDIT_VALIDATION_MEMORY_MIB,
    )
    max_pids: AuditInteger = Field(
        default=_MAX_AUDIT_VALIDATION_PIDS,
        ge=1,
        le=_MAX_AUDIT_VALIDATION_PIDS,
    )


AuditValidationConfig = AuditSandboxConfig


class AuditSourceIngestConfig(_AuditConfigModel):
    """Legacy Docker SourceIngest settings retained for config compatibility.

    The v3 local-static audit path does not instantiate this backend.
    """

    backend_id: Literal["linux_container"] = "linux_container"
    runtime: Literal["docker"] = "docker"
    image_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    policy_version: Literal["riftx.audit-source-ingest-policy/v1"] = (
        AUDIT_SOURCE_INGEST_POLICY_VERSION
    )
    max_wall_seconds: AuditInteger = Field(
        default=_MAX_AUDIT_PREFLIGHT_WALL_SECONDS,
        ge=1,
        le=_MAX_AUDIT_PREFLIGHT_WALL_SECONDS,
    )
    max_memory_mib: AuditInteger = Field(
        default=_MAX_AUDIT_PREFLIGHT_MEMORY_MIB,
        ge=64,
        le=_MAX_AUDIT_PREFLIGHT_MEMORY_MIB,
    )
    max_pids: AuditInteger = Field(
        default=_MAX_AUDIT_PREFLIGHT_PIDS,
        ge=1,
        le=_MAX_AUDIT_PREFLIGHT_PIDS,
    )
    max_result_bytes: AuditInteger = Field(
        default=_MAX_AUDIT_PREFLIGHT_RESULT_BYTES,
        ge=1_024,
        le=_MAX_AUDIT_PREFLIGHT_RESULT_BYTES,
    )
    max_output_bytes: AuditInteger = Field(
        default=_MAX_AUDIT_PREFLIGHT_OUTPUT_BYTES,
        ge=1_024,
        le=_MAX_AUDIT_PREFLIGHT_OUTPUT_BYTES,
    )
    lease_seconds: AuditInteger = Field(
        default=_MAX_AUDIT_PREFLIGHT_LEASE_SECONDS,
        ge=5,
        le=_MAX_AUDIT_PREFLIGHT_LEASE_SECONDS,
    )
    job_ttl_seconds: AuditInteger = Field(
        default=_MAX_AUDIT_PREFLIGHT_TTL_SECONDS,
        ge=60,
        le=_MAX_AUDIT_PREFLIGHT_TTL_SECONDS,
    )


class AuditConfig(_AuditConfigModel):
    enabled: AuditBoolean = False
    node_mode: Literal["local_same_node"] = "local_same_node"
    allowed_node_ids: tuple[Literal["local"], ...] = ("local",)
    preflight_token_key_id: str = Field(
        default="primary",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,63}$",
    )
    preflight_token_key: SecretStr | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    source_roots: tuple[Path, ...] = Field(default_factory=tuple, repr=False)
    snapshot_root: Path = Path("/var/lib/riftx/audit/snapshots")
    temp_root: Path = Path("/var/lib/riftx/audit/tmp")
    fix_root: Path = Path("/var/lib/riftx/audit/fixes")
    default_mode: Literal["standard", "deep", "diff"] = "standard"
    default_analysis_profile: Literal["deterministic", "hybrid"] = "deterministic"
    model_egress: AuditModelEgressConfig = Field(
        default_factory=AuditModelEgressConfig
    )
    max_repository_bytes: AuditInteger = Field(
        default=_MAX_AUDIT_REPOSITORY_BYTES,
        ge=1,
        le=_MAX_AUDIT_REPOSITORY_BYTES,
    )
    max_file_bytes: AuditInteger = Field(
        default=_MAX_AUDIT_FILE_BYTES,
        ge=1,
        le=_MAX_AUDIT_FILE_BYTES,
    )
    max_files: AuditInteger = Field(
        default=_MAX_AUDIT_FILES,
        ge=1,
        le=_MAX_AUDIT_FILES,
    )
    max_path_bytes: AuditInteger = Field(
        default=_MAX_AUDIT_PATH_BYTES,
        ge=1,
        le=_MAX_AUDIT_PATH_BYTES,
    )
    max_directory_depth: AuditInteger = Field(
        default=_MAX_AUDIT_DIRECTORY_DEPTH,
        ge=1,
        le=_MAX_AUDIT_DIRECTORY_DEPTH,
    )
    max_artifact_bytes: AuditInteger = Field(
        default=_MAX_AUDIT_ARTIFACT_BYTES,
        ge=1,
        le=_MAX_AUDIT_ARTIFACT_BYTES,
    )
    max_total_artifact_bytes: AuditInteger = Field(
        default=_MAX_AUDIT_TOTAL_ARTIFACT_BYTES,
        ge=1,
        le=_MAX_AUDIT_TOTAL_ARTIFACT_BYTES,
    )
    workers: AuditWorkersConfig = Field(default_factory=AuditWorkersConfig)
    budget: AuditBudgetConfig = Field(default_factory=AuditBudgetConfig)
    source_ingest: AuditSourceIngestConfig = Field(
        default_factory=AuditSourceIngestConfig
    )
    validation: AuditSandboxConfig = Field(default_factory=AuditSandboxConfig)

    @field_validator("allowed_node_ids")
    @classmethod
    def validate_allowed_node_ids(
        cls,
        values: tuple[Literal["local"], ...],
    ) -> tuple[Literal["local"], ...]:
        if values != ("local",):
            raise ValueError("RiftX 3.0 Audit requires allowed_node_ids=[local]")
        return values

    @field_validator("preflight_token_key", mode="before")
    @classmethod
    def validate_preflight_token_key(cls, value: object) -> SecretStr | None:
        if value is None or value == "":
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or len(raw) != 43 or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", raw
        ):
            raise ValueError(
                "audit preflight token key must be canonical unpadded base64url"
            )
        try:
            decoded = base64.urlsafe_b64decode(raw + "=")
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "audit preflight token key must be canonical unpadded base64url"
            ) from exc
        if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode(
            "ascii"
        ) != raw:
            raise ValueError("audit preflight token key must decode to exactly 32 bytes")
        return SecretStr(raw)

    @field_validator("source_roots")
    @classmethod
    def validate_source_roots(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        resolved = tuple(sorted((_canonical_source_root(path) for path in values), key=str))
        for index, root in enumerate(resolved):
            if any(_paths_overlap(root, other) for other in resolved[index + 1 :]):
                raise ValueError("Audit source roots must be distinct and non-overlapping")
        return resolved

    @field_validator("snapshot_root", "temp_root", "fix_root")
    @classmethod
    def validate_storage_root(cls, value: Path) -> Path:
        return _canonical_storage_path(
            value,
            label="Audit storage root",
            directory=True,
        )

    @model_validator(mode="after")
    def validate_audit_contract_defaults(self) -> AuditConfig:
        if self.default_mode == "deep" and self.default_analysis_profile != "hybrid":
            raise ValueError("Deep Audit mode requires the hybrid analysis profile")
        if self.max_file_bytes > self.max_repository_bytes:
            raise ValueError("max_file_bytes must not exceed max_repository_bytes")
        if self.max_artifact_bytes > self.max_total_artifact_bytes:
            raise ValueError("max_artifact_bytes must not exceed max_total_artifact_bytes")
        if self.workers.max_parallel > self.budget.max_worker_jobs:
            raise ValueError("workers.max_parallel must not exceed budget.max_worker_jobs")
        if self.validation.max_wall_seconds > self.budget.max_wall_seconds:
            raise ValueError(
                "validation.max_wall_seconds must not exceed budget.max_wall_seconds"
            )
        if self.enabled and not self.validation.require_sandbox:
            raise ValueError("enabled Audit requires validation.require_sandbox=true")
        roots = (self.snapshot_root, self.temp_root, self.fix_root)
        if any(
            _paths_overlap(root, other)
            for index, root in enumerate(roots)
            for other in roots[index + 1 :]
        ):
            raise ValueError("Audit storage roots must be distinct and non-overlapping")
        return self


def audit_config_digest(config: AuditConfig, *, path_digest_key: bytes) -> str:
    """Return a versioned digest without embedding sensitive absolute paths."""

    if not isinstance(path_digest_key, bytes) or len(path_digest_key) < 32:
        raise ValueError("path_digest_key must contain at least 32 bytes")

    def keyed_path(path: str) -> str:
        return hmac.new(path_digest_key, path.encode(), hashlib.sha256).hexdigest()

    payload = config.model_dump(mode="json")
    payload["source_roots"] = sorted(keyed_path(path) for path in payload["source_roots"])
    for field_name in ("snapshot_root", "temp_root", "fix_root"):
        payload[field_name] = keyed_path(payload[field_name])
    encoded = json.dumps(
        {"version": AUDIT_CONFIG_DIGEST_VERSION, "config": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def audit_source_ingest_policy_digest(config: AuditSourceIngestConfig) -> str:
    """Digest the non-secret, fixed SourceIngest containment policy."""

    encoded = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(
        AUDIT_SOURCE_INGEST_POLICY_VERSION.encode("ascii") + b"\0" + encoded
    ).hexdigest()


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


class SkillsConfig(_ConfigModel):
    path: Path = Path(".riftx/skills")


class WebSearchConfig(_ConfigModel):
    enabled: bool = True
    providers: tuple[Literal["openai_hosted", "searxng"], ...] = ("openai_hosted",)
    searxng_endpoint: str | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=60)

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> WebSearchConfig:
        if len(set(self.providers)) != len(self.providers):
            raise ValueError("web.search.providers must not contain duplicates")
        if self.enabled and not self.providers:
            raise ValueError("web.search.providers must not be empty when search is enabled")
        if "searxng" in self.providers:
            endpoint = (self.searxng_endpoint or "").strip()
            parsed = urlsplit(endpoint)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("web.search.searxng_endpoint must be an absolute HTTP(S) URL")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("web.search.searxng_endpoint must not contain credentials")
            if parsed.query or parsed.fragment:
                raise ValueError("web.search.searxng_endpoint must not contain a query or fragment")
            object.__setattr__(self, "searxng_endpoint", endpoint.rstrip("/"))
        return self


class WebConfig(_ConfigModel):
    dist_path: Path = Path("apps/web/dist")
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


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


def _sqlite_database_storage_path(database_url: str) -> Path | None:
    try:
        parsed = make_url(database_url)
    except ArgumentError as exc:
        raise ValueError("database URL cannot be checked for Audit path isolation") from exc
    if parsed.get_backend_name() != "sqlite":
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    if database.startswith("file:"):
        raise ValueError("SQLite URI paths are not supported for Audit path isolation")
    return Path(database).expanduser()


def validate_audit_storage_isolation(
    *,
    audit: AuditConfig,
    workspace_root: Path,
    runner_state_path: Path,
    runner_credential_path: Path,
    models_secrets_path: Path,
    local_principal_path: Path,
    database_url: str,
    temporal_tls_server_root_ca_path: Path | None = None,
    temporal_tls_client_cert_path: Path | None = None,
    temporal_tls_client_private_key_path: Path | None = None,
) -> None:
    """Revalidate deployment storage against authorized source roots."""

    if not audit.source_roots:
        return

    current_source_roots = tuple(
        _canonical_source_root(path) for path in audit.source_roots
    )

    storage_paths: list[tuple[str, Path, bool]] = [
        ("audit.snapshot_root", audit.snapshot_root, True),
        ("audit.temp_root", audit.temp_root, True),
        ("audit.fix_root", audit.fix_root, True),
        ("workspace.root", workspace_root.expanduser(), True),
        ("runner.state_path", runner_state_path.expanduser(), True),
        ("runner.credential_path", runner_credential_path.expanduser(), False),
        ("models.secrets_path", models_secrets_path.expanduser(), False),
        ("security.local_principal_path", local_principal_path.expanduser(), False),
    ]
    for label, path in (
        ("temporal.tls_server_root_ca_path", temporal_tls_server_root_ca_path),
        ("temporal.tls_client_cert_path", temporal_tls_client_cert_path),
        (
            "temporal.tls_client_private_key_path",
            temporal_tls_client_private_key_path,
        ),
    ):
        if path is not None:
            storage_paths.append((label, path.expanduser(), False))

    database_path = _sqlite_database_storage_path(database_url)
    if database_path is not None:
        storage_paths.append(("database.url", database_path, False))

    canonical_storage = [
        (
            label,
            *_storage_path_boundary(path, label=label, directory=directory),
        )
        for label, path, directory in storage_paths
    ]
    for source_root in current_source_roots:
        for label, storage_path, existing_parent in canonical_storage:
            if _paths_overlap(source_root, storage_path) or _paths_overlap(
                source_root,
                existing_parent,
            ):
                raise ValueError(
                    f"Audit source roots must not overlap protected storage ({label})"
                )


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
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    models: ModelsRuntimeConfig = Field(default_factory=ModelsRuntimeConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    subagents: SubagentConfig = Field(default_factory=SubagentConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)

    @model_validator(mode="after")
    def validate_audit_path_isolation(self) -> RiftXConfig:
        validate_audit_storage_isolation(
            audit=self.audit,
            workspace_root=self.workspace.root,
            runner_state_path=self.runner.state_path,
            runner_credential_path=self.runner.credential_path,
            models_secrets_path=self.models.secrets_path,
            local_principal_path=self.security.local_principal_path,
            database_url=self.database.url,
            temporal_tls_server_root_ca_path=self.temporal.tls_server_root_ca_path,
            temporal_tls_client_cert_path=self.temporal.tls_client_cert_path,
            temporal_tls_client_private_key_path=self.temporal.tls_client_private_key_path,
        )
        return self


_AUDIT_ENVIRONMENT_PATHS: dict[str, tuple[str, ...]] = {
    "RIFTX_AUDIT_ENABLED": ("audit", "enabled"),
    "RIFTX_AUDIT_NODE_MODE": ("audit", "node_mode"),
    "RIFTX_AUDIT_ALLOWED_NODE_IDS": ("audit", "allowed_node_ids"),
    "RIFTX_AUDIT_PREFLIGHT_TOKEN_KEY_ID": ("audit", "preflight_token_key_id"),
    "RIFTX_AUDIT_PREFLIGHT_TOKEN_KEY": ("audit", "preflight_token_key"),
    "RIFTX_AUDIT_SOURCE_ROOTS": ("audit", "source_roots"),
    "RIFTX_AUDIT_SNAPSHOT_ROOT": ("audit", "snapshot_root"),
    "RIFTX_AUDIT_TEMP_ROOT": ("audit", "temp_root"),
    "RIFTX_AUDIT_FIX_ROOT": ("audit", "fix_root"),
    "RIFTX_AUDIT_DEFAULT_MODE": ("audit", "default_mode"),
    "RIFTX_AUDIT_DEFAULT_ANALYSIS_PROFILE": (
        "audit",
        "default_analysis_profile",
    ),
    "RIFTX_AUDIT_MODEL_EGRESS_DEFAULT_MODE": (
        "audit",
        "model_egress",
        "default_mode",
    ),
    "RIFTX_AUDIT_MODEL_EGRESS_MAX_BYTES_PER_CALL": (
        "audit",
        "model_egress",
        "max_bytes_per_call",
    ),
    "RIFTX_AUDIT_MODEL_EGRESS_MAX_BYTES_PER_AUDIT": (
        "audit",
        "model_egress",
        "max_bytes_per_audit",
    ),
    "RIFTX_AUDIT_MODEL_EGRESS_ALLOW_REMOTE_ORIGINS": (
        "audit",
        "model_egress",
        "allow_remote_origins",
    ),
    "RIFTX_AUDIT_MAX_REPOSITORY_BYTES": ("audit", "max_repository_bytes"),
    "RIFTX_AUDIT_MAX_FILE_BYTES": ("audit", "max_file_bytes"),
    "RIFTX_AUDIT_MAX_FILES": ("audit", "max_files"),
    "RIFTX_AUDIT_MAX_PATH_BYTES": ("audit", "max_path_bytes"),
    "RIFTX_AUDIT_MAX_DIRECTORY_DEPTH": ("audit", "max_directory_depth"),
    "RIFTX_AUDIT_MAX_ARTIFACT_BYTES": ("audit", "max_artifact_bytes"),
    "RIFTX_AUDIT_MAX_TOTAL_ARTIFACT_BYTES": (
        "audit",
        "max_total_artifact_bytes",
    ),
    "RIFTX_AUDIT_WORKERS_MAX_PARALLEL": ("audit", "workers", "max_parallel"),
    "RIFTX_AUDIT_WORKERS_MAX_EPOCHS": ("audit", "workers", "max_epochs"),
    "RIFTX_AUDIT_WORKERS_SATURATION_EPOCHS": (
        "audit",
        "workers",
        "saturation_epochs",
    ),
    "RIFTX_AUDIT_BUDGET_MAX_WALL_SECONDS": (
        "audit",
        "budget",
        "max_wall_seconds",
    ),
    "RIFTX_AUDIT_BUDGET_MAX_MODEL_CALLS": (
        "audit",
        "budget",
        "max_model_calls",
    ),
    "RIFTX_AUDIT_BUDGET_MAX_INPUT_TOKENS": (
        "audit",
        "budget",
        "max_input_tokens",
    ),
    "RIFTX_AUDIT_BUDGET_MAX_OUTPUT_TOKENS": (
        "audit",
        "budget",
        "max_output_tokens",
    ),
    "RIFTX_AUDIT_BUDGET_MAX_WORKER_JOBS": (
        "audit",
        "budget",
        "max_worker_jobs",
    ),
    "RIFTX_AUDIT_BUDGET_MAX_CANDIDATES": (
        "audit",
        "budget",
        "max_candidates",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_BACKEND_ID": (
        "audit",
        "source_ingest",
        "backend_id",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_RUNTIME": (
        "audit",
        "source_ingest",
        "runtime",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_IMAGE_DIGEST": (
        "audit",
        "source_ingest",
        "image_digest",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_POLICY_VERSION": (
        "audit",
        "source_ingest",
        "policy_version",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_MAX_WALL_SECONDS": (
        "audit",
        "source_ingest",
        "max_wall_seconds",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_MAX_MEMORY_MIB": (
        "audit",
        "source_ingest",
        "max_memory_mib",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_MAX_PIDS": (
        "audit",
        "source_ingest",
        "max_pids",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_MAX_RESULT_BYTES": (
        "audit",
        "source_ingest",
        "max_result_bytes",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_MAX_OUTPUT_BYTES": (
        "audit",
        "source_ingest",
        "max_output_bytes",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_LEASE_SECONDS": (
        "audit",
        "source_ingest",
        "lease_seconds",
    ),
    "RIFTX_AUDIT_SOURCE_INGEST_JOB_TTL_SECONDS": (
        "audit",
        "source_ingest",
        "job_ttl_seconds",
    ),
    "RIFTX_AUDIT_VALIDATION_DEFAULT_POLICY": (
        "audit",
        "validation",
        "default_policy",
    ),
    "RIFTX_AUDIT_VALIDATION_REQUIRE_SANDBOX": (
        "audit",
        "validation",
        "require_sandbox",
    ),
    "RIFTX_AUDIT_VALIDATION_DEFAULT_NETWORK": (
        "audit",
        "validation",
        "default_network",
    ),
    "RIFTX_AUDIT_VALIDATION_MAX_WALL_SECONDS": (
        "audit",
        "validation",
        "max_wall_seconds",
    ),
    "RIFTX_AUDIT_VALIDATION_MAX_MEMORY_MIB": (
        "audit",
        "validation",
        "max_memory_mib",
    ),
    "RIFTX_AUDIT_VALIDATION_MAX_PIDS": (
        "audit",
        "validation",
        "max_pids",
    ),
}
_AUDIT_JSON_LIST_ENVIRONMENT = frozenset(
    {
        "RIFTX_AUDIT_ALLOWED_NODE_IDS",
        "RIFTX_AUDIT_SOURCE_ROOTS",
        "RIFTX_AUDIT_MODEL_EGRESS_ALLOW_REMOTE_ORIGINS",
    }
)


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
    "RIFTX_WEB_SEARCH_ENABLED": ("web", "search", "enabled"),
    "RIFTX_WEB_SEARCH_PROVIDERS": ("web", "search", "providers"),
    "RIFTX_SEARXNG_ENDPOINT": ("web", "search", "searxng_endpoint"),
    "RIFTX_WEB_SEARCH_TIMEOUT_SECONDS": ("web", "search", "timeout_seconds"),
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
    **_AUDIT_ENVIRONMENT_PATHS,
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
        _reject_audit_override(cli_overrides, "CLI overrides")
        _deep_merge(merged, dict(cli_overrides))
    if run_overrides:
        _reject_temporal_api_key_override(run_overrides, "Run overrides")
        _reject_audit_override(run_overrides, "Run overrides")
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
    unknown_audit_names = sorted(
        name
        for name in environment
        if name.startswith("RIFTX_AUDIT_") and name not in _AUDIT_ENVIRONMENT_PATHS
    )
    if unknown_audit_names:
        raise RiftXConfigError(
            "unsupported Audit environment variable(s): "
            + ", ".join(unknown_audit_names)
        )
    layer: dict[str, Any] = {}
    for name, path in _ENVIRONMENT_PATHS.items():
        raw = environment.get(name)
        if raw is None:
            continue
        value: object = raw
        if name in {"RIFTX_CORS_ORIGINS", "RIFTX_WEB_SEARCH_PROVIDERS"}:
            value = [item.strip() for item in raw.split(",") if item.strip()]
        elif name == "RIFTX_LOCAL_OPERATOR_CAPABILITIES":
            value = [item.strip() for item in raw.split(",") if item.strip()]
        elif name in _AUDIT_JSON_LIST_ENVIRONMENT:
            value = _parse_audit_json_array(raw, name=name)
        _set_nested(layer, path, value)
    return layer


def _parse_audit_json_array(raw: str, *, name: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RiftXConfigError(f"{name} must be a JSON array of strings") from exc
    if (
        not isinstance(value, list)
        or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in value
        )
    ):
        raise RiftXConfigError(f"{name} must be a JSON array of non-empty strings")
    return value


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
            and (
                normalized.endswith("api_key")
                or normalized.endswith("token")
                or normalized == "preflight_token_key"
            )
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


def _reject_audit_override(payload: Mapping[str, object], source: str) -> None:
    if "audit" in payload:
        raise RiftXConfigError(
            f"Audit deployment policy cannot be changed through {source}"
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
