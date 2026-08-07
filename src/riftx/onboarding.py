"""Safe first-run local configuration scaffolding."""

from __future__ import annotations

import os
import shutil
import stat
import sysconfig
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from riftx.config import RiftXConfig
from riftx.local_fs import OwnerDirectoryBatch, OwnerDirectoryError
from riftx.models.config import ModelProfile, ModelsConfig, parse_models_config
from riftx.tools.config import parse_tool_config


class OnboardError(RuntimeError):
    """Raised when first-run state cannot be created without overwriting data."""


@dataclass(frozen=True, slots=True)
class OnboardResult:
    config_path: Path
    models_path: Path
    tools_path: Path
    disabled_tools: tuple[str, ...]


def initialize_local_onboarding(
    config_path: Path,
    *,
    model_profile: ModelProfile,
    environment: Mapping[str, str] | None = None,
    workspace_root: Path | None = None,
    tool_template_path: Path | None = None,
) -> OnboardResult:
    """Create new user-owned RiftX, model, and tool configuration files."""

    env = os.environ if environment is None else environment
    normalized_config = _absolute(config_path)
    config_root = normalized_config.parent
    models_path = config_root / "models.yaml"
    tools_path = config_root / "tools.yaml"
    targets = (normalized_config, models_path, tools_path)
    existing = tuple(path for path in targets if path.exists() or path.is_symlink())
    if existing:
        raise OnboardError(
            "Onboarding refuses to overwrite a file that already exists: "
            + ", ".join(str(path) for path in existing)
        )

    state_root = _xdg_root(env, "XDG_STATE_HOME", "~/.local/state") / "riftx"
    data_root = _xdg_root(env, "XDG_DATA_HOME", "~/.local/share") / "riftx"
    workspace = (
        _absolute(workspace_root) if workspace_root is not None else data_root / "workspaces"
    )
    secrets_root = state_root / "secrets"
    audit_snapshot_root = data_root / "audit" / "snapshots"
    web_dist = resolve_onboard_web_dist()

    models = ModelsConfig(default_profile="primary", models={"primary": model_profile})
    models_content = _yaml_bytes(models.model_dump(mode="json", exclude_none=False))
    parse_models_config(models_content, source="generated onboarding model config")

    template = resolve_onboard_tool_template(tool_template_path)
    tools = parse_tool_config(template.read_bytes(), source=str(template))
    tools_payload = tools.model_dump(mode="json", exclude_none=True)
    disabled_tools = _disable_unavailable_tools(tools_payload, env)
    tools_content = _yaml_bytes(tools_payload)
    parse_tool_config(tools_content, source="generated onboarding tool config")

    runtime_payload = {
        "database": {"url": f"sqlite+aiosqlite:///{state_root / 'riftx.db'}"},
        "runner": {
            "state_path": str(state_root / "runner"),
            "credential_path": str(secrets_root / "runner-credentials.json"),
        },
        "workspace": {"root": str(workspace)},
        "tools": {"path": str(tools_path)},
        "skills": {"path": str(data_root / "skills")},
        "models": {
            "path": str(models_path),
            "secrets_path": str(secrets_root / "models.json"),
        },
        "web": {"dist_path": str(web_dist)},
        "security": {
            "trust_profile": "local_single_operator",
            "local_principal_path": str(secrets_root / "local-principal.json"),
        },
        "audit": {
            "snapshot_root": str(audit_snapshot_root),
        },
    }
    RiftXConfig.model_validate(runtime_payload)
    runtime_content = _yaml_bytes(runtime_payload)

    directories = (
        config_root,
        state_root,
        secrets_root,
        state_root / "runner",
        data_root,
        workspace,
        data_root / "skills",
        audit_snapshot_root,
    )
    directory_batch = OwnerDirectoryBatch()
    created_files: list[tuple[Path, tuple[int, int]]] = []
    try:
        directory_batch.ensure(directories)
        for path, content in (
            (models_path, models_content),
            (tools_path, tools_content),
            (normalized_config, runtime_content),
        ):
            created_files.append((path, _write_new_file(path, content)))
    except Exception as exc:
        file_failures = _rollback_files(created_files)
        try:
            directory_batch.rollback()
        except OwnerDirectoryError as rollback_error:
            raise OnboardError(
                "Onboarding initialization failed and rollback was incomplete."
            ) from rollback_error
        if file_failures:
            raise OnboardError(
                "Onboarding initialization failed and file rollback was incomplete: "
                + ", ".join(str(path) for path in file_failures)
            ) from exc
        raise OnboardError(
            f"Onboarding initialization failed and new state was rolled back: {exc}"
        ) from exc
    directory_batch.commit()
    return OnboardResult(
        config_path=normalized_config,
        models_path=models_path,
        tools_path=tools_path,
        disabled_tools=disabled_tools,
    )


def resolve_onboard_tool_template(explicit_path: Path | None = None) -> Path:
    candidates = (
        (explicit_path,)
        if explicit_path is not None
        else (
            Path(__file__).resolve().parents[2] / "configs" / "tools.example.yaml",
            Path(sysconfig.get_path("data"))
            / "share"
            / "riftx"
            / "templates"
            / "tools.example.yaml",
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise OnboardError("Packaged onboarding Tool Registry template is unavailable.")


def resolve_onboard_web_dist() -> Path:
    candidates = (
        Path(__file__).resolve().parent / "_webui",
        Path(__file__).resolve().parents[2] / "apps" / "web" / "dist",
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    raise OnboardError("Packaged RiftX WebUI is unavailable.")


def validate_existing_onboarding(path: Path) -> Path:
    normalized = _absolute(path)
    _reject_symbolic_components(normalized)
    try:
        metadata = normalized.lstat()
    except OSError as exc:
        raise OnboardError(
            f"Existing onboarding configuration is unavailable: {normalized}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise OnboardError(f"Existing onboarding configuration is not a file: {normalized}")
    if metadata.st_uid != os.geteuid():
        raise OnboardError(
            f"Existing onboarding configuration is not owned by the current user: {normalized}"
        )
    return normalized


def _disable_unavailable_tools(
    payload: dict[str, object],
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    raw_tools = payload.get("tools")
    assert isinstance(raw_tools, dict)
    disabled: list[str] = []
    search_path = environment.get("PATH", os.defpath)
    for tool_id, raw_tool in raw_tools.items():
        assert isinstance(tool_id, str)
        assert isinstance(raw_tool, dict)
        if not raw_tool.get("enabled", True):
            continue
        command = raw_tool.get("command")
        assert isinstance(command, list) and command
        executable = str(command[0])
        if shutil.which(executable, path=search_path) is None:
            raw_tool["enabled"] = False
            disabled.append(tool_id)
    return tuple(sorted(disabled))


def _write_new_file(path: Path, content: bytes) -> tuple[int, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    identity: tuple[int, int] | None = None
    try:
        metadata = os.fstat(descriptor)
        identity = metadata.st_dev, metadata.st_ino
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("short write while creating onboarding configuration")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _fsync_directory(path.parent)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if identity is not None:
            _unlink_if_identity(path, identity)
        raise
    assert identity is not None
    return identity


def _rollback_files(
    created_files: list[tuple[Path, tuple[int, int]]],
) -> tuple[Path, ...]:
    failures: list[Path] = []
    for path, identity in reversed(created_files):
        try:
            _unlink_if_identity(path, identity)
            _fsync_directory(path.parent)
        except Exception:
            failures.append(path)
    return tuple(failures)


def _unlink_if_identity(path: Path, identity: tuple[int, int]) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity:
        raise OnboardError(f"Onboarding file identity changed before rollback: {path}")
    path.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _reject_symbolic_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise OnboardError(f"Onboarding path contains a symbolic link: {current}")


def _xdg_root(environment: Mapping[str, str], name: str, fallback: str) -> Path:
    return _absolute(Path(environment.get(name, fallback)))


def _yaml_bytes(payload: object) -> bytes:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode("utf-8")


__all__ = [
    "OnboardError",
    "OnboardResult",
    "initialize_local_onboarding",
    "resolve_onboard_tool_template",
    "validate_existing_onboarding",
]
