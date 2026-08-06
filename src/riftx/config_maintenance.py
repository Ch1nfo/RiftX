"""Safe removal of retired runtime configuration fields."""

from __future__ import annotations

import copy
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import yaml
from yaml.nodes import MappingNode, ScalarNode

from riftx.config import AuditSourceIngestConfig
from riftx.local_fs import OwnerDirectoryError, ensure_owner_directories

_MAX_CONFIG_BYTES = 1_048_576
_LEGACY_COMMENTS = (
    "  # Real repository Preflight remains unavailable until an operator supplies\n",
    "  # a reviewed, immutable image digest for the local Linux SourceIngest worker.\n",
)


class RuntimeConfigMigrationStatus(StrEnum):
    READY = "ready"
    MIGRATABLE = "migratable"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class RuntimeConfigMigrationState:
    path: Path
    status: RuntimeConfigMigrationStatus
    detail: str
    fixable: bool


@dataclass(frozen=True, slots=True)
class RuntimeConfigRepairResult:
    path: Path
    backup_path: Path


class RuntimeConfigMigrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        backup_path: Path | None = None,
        rollback_complete: bool = False,
    ) -> None:
        super().__init__(message)
        self.backup_path = backup_path
        self.rollback_complete = rollback_complete


def inspect_runtime_config_migration(path: Path) -> RuntimeConfigMigrationState:
    """Classify the only retired runtime setting safe to migrate automatically."""

    normalized = Path(os.path.abspath(os.fspath(path)))
    if not normalized.exists() and not normalized.is_symlink():
        return RuntimeConfigMigrationState(
            path=normalized,
            status=RuntimeConfigMigrationStatus.READY,
            detail="Runtime configuration file does not exist; no migration is required.",
            fixable=False,
        )
    try:
        content, _ = _read_regular_file(normalized)
        payload = yaml.safe_load(content.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError, RuntimeConfigMigrationError) as exc:
        return RuntimeConfigMigrationState(
            path=normalized,
            status=RuntimeConfigMigrationStatus.MANUAL,
            detail=f"Runtime configuration requires manual review: {exc}",
            fixable=False,
        )
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return RuntimeConfigMigrationState(
            path=normalized,
            status=RuntimeConfigMigrationStatus.MANUAL,
            detail="Runtime configuration root must be a mapping.",
            fixable=False,
        )
    audit = payload.get("audit")
    if not isinstance(audit, dict) or "source_ingest" not in audit:
        return RuntimeConfigMigrationState(
            path=normalized,
            status=RuntimeConfigMigrationStatus.READY,
            detail="No retired audit.source_ingest configuration is present.",
            fixable=False,
        )
    expected = AuditSourceIngestConfig().model_dump(mode="python")
    if audit["source_ingest"] != expected:
        return RuntimeConfigMigrationState(
            path=normalized,
            status=RuntimeConfigMigrationStatus.MANUAL,
            detail=(
                "audit.source_ingest was customized and requires manual migration; "
                "RiftX will not discard operator settings."
            ),
            fixable=False,
        )
    try:
        _remove_legacy_source_ingest(content)
    except (UnicodeError, yaml.YAMLError, ValueError) as exc:
        return RuntimeConfigMigrationState(
            path=normalized,
            status=RuntimeConfigMigrationStatus.MANUAL,
            detail=f"Runtime configuration layout requires manual migration: {exc}",
            fixable=False,
        )
    return RuntimeConfigMigrationState(
        path=normalized,
        status=RuntimeConfigMigrationStatus.MIGRATABLE,
        detail="Retired default audit.source_ingest settings can be removed safely.",
        fixable=True,
    )


def repair_runtime_config(path: Path) -> RuntimeConfigRepairResult:
    """Back up, migrate, verify, and rollback one runtime YAML file."""

    normalized = Path(os.path.abspath(os.fspath(path)))
    state = inspect_runtime_config_migration(normalized)
    if not state.fixable:
        raise RuntimeConfigMigrationError(
            f"Runtime configuration migration is {state.status.value}: {state.detail}"
        )
    original, metadata = _read_regular_file(normalized)
    identity = (metadata.st_dev, metadata.st_ino)
    try:
        migrated = _remove_legacy_source_ingest(original)
    except (UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise RuntimeConfigMigrationError(f"Runtime configuration migration failed: {exc}") from exc

    backup_path = _write_backup(normalized, original)
    migrated_identity: tuple[int, int] | None = None
    try:
        migrated_identity = _atomic_replace(
            normalized,
            migrated,
            mode=stat.S_IMODE(metadata.st_mode),
            expected_identity=identity,
        )
        _verify_migrated_config(normalized)
    except Exception as exc:
        try:
            if migrated_identity is None:
                current = _regular_file_identity(normalized)
                if current != identity:
                    raise RuntimeConfigMigrationError(
                        "Runtime configuration identity changed before rollback."
                    )
            else:
                _atomic_replace(
                    normalized,
                    original,
                    mode=stat.S_IMODE(metadata.st_mode),
                    expected_identity=migrated_identity,
                )
        except Exception as rollback_error:
            raise RuntimeConfigMigrationError(
                "Runtime configuration migration failed and rollback was incomplete.",
                backup_path=backup_path,
                rollback_complete=False,
            ) from rollback_error
        raise RuntimeConfigMigrationError(
            "Runtime configuration migration failed and the original was restored "
            f"from memory; backup retained at {backup_path}.",
            backup_path=backup_path,
            rollback_complete=True,
        ) from exc
    return RuntimeConfigRepairResult(path=normalized, backup_path=backup_path)


def _verify_migrated_config(path: Path) -> None:
    state = inspect_runtime_config_migration(path)
    if state.status is not RuntimeConfigMigrationStatus.READY:
        raise RuntimeConfigMigrationError(state.detail)


def _remove_legacy_source_ingest(original: bytes) -> bytes:
    text = original.decode("utf-8")
    payload = yaml.safe_load(text)
    document = yaml.compose(text)
    if not isinstance(payload, dict) or not isinstance(document, MappingNode):
        raise ValueError("runtime configuration root must be a mapping")
    audit_payload = payload.get("audit")
    expected_source_ingest = AuditSourceIngestConfig().model_dump(mode="python")
    if (
        not isinstance(audit_payload, dict)
        or audit_payload.get("source_ingest") != expected_source_ingest
    ):
        raise ValueError("audit.source_ingest requires manual migration")
    audit_node = _mapping_value(document, "audit")
    if not isinstance(audit_node, MappingNode):
        raise ValueError("audit must be a mapping")
    source_key, source_value = _mapping_entry(audit_node, "source_ingest")
    start = source_key.start_mark.line
    end = source_value.end_mark.line
    lines = text.splitlines(keepends=True)
    if start >= 2 and tuple(lines[start - 2 : start]) == _LEGACY_COMMENTS:
        start -= 2
    migrated_text = "".join((*lines[:start], *lines[end:]))

    expected = copy.deepcopy(payload)
    expected_audit = expected["audit"]
    assert isinstance(expected_audit, dict)
    del expected_audit["source_ingest"]
    if yaml.safe_load(migrated_text) != expected:
        raise ValueError("migration changed fields other than audit.source_ingest")
    return migrated_text.encode("utf-8")


def _mapping_value(node: MappingNode, key: str) -> yaml.Node | None:
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return value_node
    return None


def _mapping_entry(node: MappingNode, key: str) -> tuple[yaml.Node, yaml.Node]:
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return key_node, value_node
    raise ValueError(f"missing {key}")


def _read_regular_file(path: Path) -> tuple[bytes, os.stat_result]:
    _reject_symbolic_components(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if path.is_symlink():
            raise RuntimeConfigMigrationError(
                f"Runtime configuration must not be a symbolic link: {path}"
            ) from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeConfigMigrationError(
                f"Runtime configuration is not a regular file: {path}"
            )
        if metadata.st_uid != os.geteuid():
            raise RuntimeConfigMigrationError(
                f"Runtime configuration is not owned by the current user: {path}"
            )
        if metadata.st_size > _MAX_CONFIG_BYTES:
            raise RuntimeConfigMigrationError(
                f"Runtime configuration exceeds {_MAX_CONFIG_BYTES} bytes: {path}"
            )
        chunks: list[bytes] = []
        remaining = _MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > _MAX_CONFIG_BYTES:
            raise RuntimeConfigMigrationError(
                f"Runtime configuration exceeds {_MAX_CONFIG_BYTES} bytes: {path}"
            )
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeConfigMigrationError(
                "Runtime configuration identity changed while it was read."
            )
        return content, metadata
    finally:
        os.close(descriptor)


def _reject_symbolic_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeConfigMigrationError(
                f"Runtime configuration path contains a symbolic link: {current}"
            )


def _write_backup(path: Path, content: bytes) -> Path:
    backup_directory = path.parent / ".riftx-backups"
    try:
        ensure_owner_directories((backup_directory,))
    except OwnerDirectoryError as exc:
        raise RuntimeConfigMigrationError(str(exc)) from exc
    backup_path = backup_directory / f"{path.name}.{uuid4().hex}.bak"
    descriptor = os.open(
        backup_path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, content)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(backup_directory)
    return backup_path


def _atomic_replace(
    path: Path,
    content: bytes,
    *,
    mode: int,
    expected_identity: tuple[int, int],
) -> tuple[int, int]:
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, content)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if _regular_file_identity(path) != expected_identity:
            raise RuntimeConfigMigrationError(
                "Runtime configuration identity changed before replacement."
            )
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
        return _regular_file_identity(path)
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _regular_file_identity(path: Path) -> tuple[int, int]:
    _reject_symbolic_components(path)
    metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeConfigMigrationError(f"Path is not a regular file: {path}")
    return metadata.st_dev, metadata.st_ino


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("short write while persisting runtime configuration")
        view = view[written:]


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


__all__ = [
    "RuntimeConfigMigrationError",
    "RuntimeConfigMigrationState",
    "RuntimeConfigMigrationStatus",
    "RuntimeConfigRepairResult",
    "inspect_runtime_config_migration",
    "repair_runtime_config",
]
