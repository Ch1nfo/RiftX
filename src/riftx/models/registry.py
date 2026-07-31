"""Atomic model-profile metadata and local credential storage."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import yaml

from .config import (
    ModelProfile,
    ModelsConfig,
    default_models_config,
    normalize_profile_name,
    parse_models_config,
    same_credential_destination,
)

_PROCESS_FILE_LOCKS_GUARD = threading.Lock()
_PROCESS_FILE_LOCKS: dict[str, threading.RLock] = {}


class ModelRegistryError(RuntimeError):
    pass


class ModelProfileNotFoundError(ModelRegistryError):
    def __init__(self, profile_name: str) -> None:
        super().__init__(f"model profile {profile_name!r} is not configured")
        self.profile_name = profile_name


class ModelRegistryConflictError(ModelRegistryError):
    pass


@dataclass(frozen=True, slots=True)
class ModelRegistrySnapshot:
    config: ModelsConfig
    generation: int
    source_digest: str


@dataclass(frozen=True, slots=True)
class _StoredAPIKey:
    value: str
    profile_digest: str | None


class ModelSecretStore:
    """Store API keys outside YAML with strict local filesystem permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def get(self, profile_name: str, *, profile_digest: str) -> str | None:
        with self._lock:
            _, entries = self._read_entries()
            entry = entries.get(profile_name)
            if entry is None or entry.profile_digest != profile_digest:
                return None
            return entry.value

    def contains(self, profile_name: str, *, profile_digest: str) -> bool:
        return self.get(profile_name, profile_digest=profile_digest) is not None

    def entry(self, profile_name: str) -> _StoredAPIKey | None:
        """Return the raw versioned entry for transactional rollback only."""

        with self._lock:
            _, entries = self._read_entries()
            return entries.get(profile_name)

    def set(self, profile_name: str, api_key: str, *, profile_digest: str) -> None:
        normalized = api_key.strip()
        if not normalized:
            raise ValueError("API key must not be empty")
        with self._lock:
            _, entries = self._read_entries()
            entries[profile_name] = _StoredAPIKey(normalized, profile_digest)
            self._write_entries(entries)

    def restore(self, profile_name: str, entry: _StoredAPIKey | None) -> None:
        with self._lock:
            _, entries = self._read_entries()
            if entry is None:
                entries.pop(profile_name, None)
            else:
                entries[profile_name] = entry
            self._write_entries(entries)

    def delete(self, profile_name: str) -> None:
        with self._lock:
            _, entries = self._read_entries()
            if entries.pop(profile_name, None) is not None:
                self._write_entries(entries)

    def migrate_v1(self, profile_digests: dict[str, str]) -> None:
        """Bind legacy keys to the exact profile metadata observed during migration.

        A concurrent metadata update can make a migrated entry temporarily unavailable,
        but the digest check prevents a legacy key from being paired with that new
        endpoint. Orphaned legacy keys are discarded because they cannot be bound safely.
        """

        with self._lock:
            version, entries = self._read_entries()
            if version != 1:
                return
            migrated = {
                name: _StoredAPIKey(entry.value, profile_digests[name])
                for name, entry in entries.items()
                if name in profile_digests
            }
            self._write_entries(migrated)

    def digest(self) -> str:
        with self._lock:
            self._secure_existing_path()
            try:
                content = self.path.read_bytes()
            except FileNotFoundError:
                content = b""
            except OSError as exc:
                raise ModelRegistryError(
                    f"could not read model credential store {self.path}"
                ) from exc
            return hashlib.sha256(content).hexdigest()

    def _read_entries(self) -> tuple[int, dict[str, _StoredAPIKey]]:
        self._secure_existing_path()
        try:
            content = self.path.read_text()
        except FileNotFoundError:
            return 2, {}
        except OSError as exc:
            raise ModelRegistryError(f"could not read model credential store {self.path}") from exc
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelRegistryError(f"invalid model credential store {self.path}") from exc
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
            raise ModelRegistryError(f"invalid model credential store {self.path}")
        version = payload["version"]
        raw_keys = payload.get("api_keys")
        if not isinstance(raw_keys, dict):
            raise ModelRegistryError(f"invalid model credential store {self.path}")
        entries: dict[str, _StoredAPIKey] = {}
        for name, value in raw_keys.items():
            if not isinstance(name, str):
                raise ModelRegistryError(f"invalid model credential store {self.path}")
            if version == 1:
                if not isinstance(value, str):
                    raise ModelRegistryError(f"invalid model credential store {self.path}")
                entries[name] = _StoredAPIKey(value=value, profile_digest=None)
                continue
            if not isinstance(value, dict):
                raise ModelRegistryError(f"invalid model credential store {self.path}")
            api_key = value.get("value")
            profile_digest = value.get("profile_digest")
            if (
                not isinstance(api_key, str)
                or not isinstance(profile_digest, str)
                or len(profile_digest) != 64
                or any(character not in "0123456789abcdef" for character in profile_digest)
            ):
                raise ModelRegistryError(f"invalid model credential store {self.path}")
            entries[name] = _StoredAPIKey(
                value=api_key,
                profile_digest=profile_digest,
            )
        return version, entries

    def _secure_existing_path(self) -> None:
        absolute_path = self.path.absolute()
        for component in (absolute_path, *absolute_path.parents):
            try:
                component_status = component.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ModelRegistryError(
                    f"could not inspect model credential store path {self.path}"
                ) from exc
            if stat.S_ISLNK(component_status.st_mode):
                raise ModelRegistryError("model credential store must not use symbolic links")

        parent = self.path.parent
        try:
            parent_status = parent.stat()
        except FileNotFoundError:
            parent_status = None
        except OSError as exc:
            raise ModelRegistryError(
                f"could not inspect model credential store directory {parent}"
            ) from exc
        if parent_status is not None:
            if not stat.S_ISDIR(parent_status.st_mode):
                raise ModelRegistryError(
                    f"model credential store directory {parent} is not a directory"
                )
            if os.name == "posix":
                if parent_status.st_uid != os.geteuid():
                    raise ModelRegistryError(
                        "model credential store directory must be owned by the current user"
                    )
                try:
                    os.chmod(parent, 0o700)
                except OSError as exc:
                    raise ModelRegistryError(
                        f"could not secure model credential store directory {parent}"
                    ) from exc

        try:
            path_status = self.path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ModelRegistryError(
                f"could not inspect model credential store {self.path}"
            ) from exc
        if not stat.S_ISREG(path_status.st_mode):
            raise ModelRegistryError("model credential store must be a regular file")
        if os.name == "posix":
            if path_status.st_uid != os.geteuid():
                raise ModelRegistryError("model credential store must be owned by the current user")
            try:
                os.chmod(self.path, 0o600)
            except OSError as exc:
                raise ModelRegistryError(
                    f"could not secure model credential store {self.path}"
                ) from exc

    def _write_entries(self, entries: dict[str, _StoredAPIKey]) -> None:
        content = json.dumps(
            {
                "version": 2,
                "api_keys": {
                    name: {
                        "value": entry.value,
                        "profile_digest": entry.profile_digest,
                    }
                    for name, entry in entries.items()
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ModelRegistryError(
                f"could not create model credential store directory {self.path.parent}"
            ) from exc
        self._secure_existing_path()
        _atomic_write(self.path, content, file_mode=0o600, directory_mode=0o700)


class ModelProfileRegistry:
    """Keep model metadata and credentials reloadable across API and Worker processes."""

    def __init__(
        self,
        config_path: Path,
        secrets_path: Path,
        *,
        initial_config: ModelsConfig | None = None,
    ) -> None:
        self.config_path = config_path
        self.secrets = ModelSecretStore(secrets_path)
        self.lock_path = secrets_path.with_suffix(f"{secrets_path.suffix}.lock")
        self._initial_config = initial_config or default_models_config()
        self._lock = threading.RLock()
        self._snapshot: ModelRegistrySnapshot | None = None
        self._source_digest: str | None = None
        self._secret_digest: str | None = None
        self._generation = 0

    @property
    def snapshot(self) -> ModelRegistrySnapshot:
        if self._snapshot is None:
            return self.refresh()
        return self._snapshot

    def refresh(self) -> ModelRegistrySnapshot:
        with self._lock, _registry_file_lock(self.lock_path):
            return self._refresh_locked()

    def reload_if_changed(self) -> ModelRegistrySnapshot:
        with self._lock, _registry_file_lock(self.lock_path):
            return self._reload_if_changed_locked()

    def get(self, profile_name: str) -> ModelProfile:
        normalized = normalize_profile_name(profile_name)
        profile = self.reload_if_changed().config.models.get(normalized)
        if profile is None:
            raise ModelProfileNotFoundError(normalized)
        return profile

    def resolve(self, profile_name: str | None, *, override: str | None = None) -> str:
        snapshot = self.reload_if_changed()
        selected = normalize_profile_name(
            profile_name or override or snapshot.config.default_profile
        )
        if selected not in snapshot.config.models:
            raise ModelProfileNotFoundError(selected)
        return selected

    def upsert(
        self,
        profile_name: str,
        profile: ModelProfile,
        *,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> ModelRegistrySnapshot:
        normalized = normalize_profile_name(profile_name)
        if not profile.requires_api_key and api_key is not None:
            raise ValueError("api_key must not be supplied when requires_api_key is false")
        with self._lock, _registry_file_lock(self.lock_path):
            current = self._refresh_locked().config
            previous_config = self.config_path.read_bytes()
            previous_entry = self.secrets.entry(normalized)
            current_profile = current.models.get(normalized)
            current_digest = (
                _profile_digest(current_profile) if current_profile is not None else None
            )
            previous_api_key = (
                previous_entry.value
                if previous_entry is not None and previous_entry.profile_digest == current_digest
                else None
            )
            credential_destination_unchanged = bool(
                current_profile is not None
                and same_credential_destination(current_profile, profile)
            )
            models = dict(current.models)
            models[normalized] = profile
            updated = ModelsConfig(default_profile=current.default_profile, models=models)
            updated_digest = _profile_digest(profile)
            credential_changed = bool(
                api_key is not None
                or clear_api_key
                or (previous_entry is not None and previous_entry.profile_digest != updated_digest)
            )
            try:
                if api_key is not None:
                    self.secrets.set(
                        normalized,
                        api_key,
                        profile_digest=updated_digest,
                    )
                elif clear_api_key:
                    self.secrets.delete(normalized)
                elif (
                    previous_api_key is not None
                    and previous_entry is not None
                    and credential_destination_unchanged
                ):
                    if previous_entry.profile_digest != updated_digest:
                        self.secrets.set(
                            normalized,
                            previous_api_key,
                            profile_digest=updated_digest,
                        )
                elif previous_entry is not None:
                    # Never retain an unbound or stale credential that could become
                    # active again after a later metadata rollback.
                    self.secrets.delete(normalized)
                self._write_config(updated)
                return self._refresh_content(self.config_path.read_bytes())
            except Exception:
                self._rollback_mutation(
                    previous_config,
                    normalized,
                    previous_entry,
                    restore_credential=credential_changed,
                )
                raise

    def set_default(self, profile_name: str) -> ModelRegistrySnapshot:
        normalized = normalize_profile_name(profile_name)
        with self._lock, _registry_file_lock(self.lock_path):
            current = self._refresh_locked().config
            if normalized not in current.models:
                raise ModelProfileNotFoundError(normalized)
            updated = ModelsConfig(default_profile=normalized, models=dict(current.models))
            self._write_config(updated)
            return self._refresh_content(self.config_path.read_bytes())

    def delete(self, profile_name: str) -> ModelRegistrySnapshot:
        normalized = normalize_profile_name(profile_name)
        with self._lock, _registry_file_lock(self.lock_path):
            current = self._refresh_locked().config
            if normalized not in current.models:
                raise ModelProfileNotFoundError(normalized)
            if normalized == current.default_profile:
                raise ModelRegistryConflictError(
                    "the default model profile cannot be removed; select another default first"
                )
            models = dict(current.models)
            del models[normalized]
            updated = ModelsConfig(default_profile=current.default_profile, models=models)
            previous_config = self.config_path.read_bytes()
            previous_entry = self.secrets.entry(normalized)
            try:
                self._write_config(updated)
                self.secrets.delete(normalized)
                return self._refresh_content(self.config_path.read_bytes())
            except Exception:
                self._rollback_mutation(
                    previous_config,
                    normalized,
                    previous_entry,
                    restore_credential=True,
                )
                raise

    def api_key(
        self,
        profile_name: str,
        profile: ModelProfile | None = None,
    ) -> str | None:
        with self._lock, _registry_file_lock(self.lock_path):
            normalized, resolved_profile = self._resolve_profile_value_locked(
                profile_name,
                profile,
            )
            if not resolved_profile.requires_api_key:
                return None
            return self.secrets.get(
                normalized,
                profile_digest=_profile_digest(resolved_profile),
            )

    def has_stored_api_key(
        self,
        profile_name: str,
        profile: ModelProfile | None = None,
    ) -> bool:
        with self._lock, _registry_file_lock(self.lock_path):
            normalized, resolved_profile = self._resolve_profile_value_locked(
                profile_name,
                profile,
            )
            if not resolved_profile.requires_api_key:
                return False
            return self.secrets.contains(
                normalized,
                profile_digest=_profile_digest(resolved_profile),
            )

    def _resolve_profile_value_locked(
        self,
        profile_name: str,
        profile: ModelProfile | None,
    ) -> tuple[str, ModelProfile]:
        normalized = normalize_profile_name(profile_name)
        if profile is not None:
            return normalized, profile
        configured = self._reload_if_changed_locked().config.models.get(normalized)
        if configured is None:
            raise ModelProfileNotFoundError(normalized)
        return normalized, configured

    def _refresh_locked(self) -> ModelRegistrySnapshot:
        return self._refresh_content(self._read_or_initialize())

    def _reload_if_changed_locked(self) -> ModelRegistrySnapshot:
        content = self._read_or_initialize()
        source_digest = hashlib.sha256(content).hexdigest()
        secret_digest = self.secrets.digest()
        if (
            self._snapshot is not None
            and source_digest == self._source_digest
            and secret_digest == self._secret_digest
        ):
            return self._snapshot
        return self._refresh_content(content)

    def _read_or_initialize(self) -> bytes:
        try:
            return self.config_path.read_bytes()
        except FileNotFoundError:
            self._write_config(self._initial_config)
            return self.config_path.read_bytes()
        except OSError as exc:
            raise ModelRegistryError(f"could not read model config {self.config_path}") from exc

    def _write_config(self, config: ModelsConfig) -> None:
        content = yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        ).encode("utf-8")
        _atomic_write(self.config_path, content, file_mode=0o600)

    def _rollback_mutation(
        self,
        previous_config: bytes,
        profile_name: str,
        previous_entry: _StoredAPIKey | None,
        *,
        restore_credential: bool,
    ) -> None:
        failures: list[Exception] = []
        if restore_credential:
            try:
                self.secrets.restore(profile_name, previous_entry)
            except Exception as exc:
                failures.append(exc)

        restore_config = True
        try:
            restore_config = self.config_path.read_bytes() != previous_config
        except OSError:
            # A failed read must not suppress the best-effort restore attempt.
            pass
        if restore_config:
            try:
                _atomic_write(self.config_path, previous_config, file_mode=0o600)
            except Exception as exc:
                failures.append(exc)

        if not failures:
            try:
                self._refresh_content(previous_config)
            except Exception as exc:
                failures.append(exc)

        if failures:
            self._snapshot = None
            self._source_digest = None
            self._secret_digest = None
            failure_types = ", ".join(type(failure).__name__ for failure in failures)
            raise ModelRegistryError(
                "model profile update failed and local configuration rollback was incomplete "
                f"({failure_types})"
            ) from failures[0]

    def _refresh_content(self, content: bytes) -> ModelRegistrySnapshot:
        source_digest = hashlib.sha256(content).hexdigest()
        config = parse_models_config(content, source=str(self.config_path))
        self.secrets.migrate_v1(
            {name: _profile_digest(profile) for name, profile in config.models.items()}
        )
        secret_digest = self.secrets.digest()
        self._generation += 1
        snapshot = ModelRegistrySnapshot(
            config=config,
            generation=self._generation,
            source_digest=source_digest,
        )
        self._snapshot = snapshot
        self._source_digest = source_digest
        self._secret_digest = secret_digest
        return snapshot


def _atomic_write(
    path: Path,
    content: bytes,
    *,
    file_mode: int,
    directory_mode: int | None = None,
) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=directory_mode or 0o755)
    if directory_mode is not None and not parent_existed:
        os.chmod(path.parent, directory_mode)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, file_mode)
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _profile_digest(profile: ModelProfile) -> str:
    canonical = json.dumps(
        profile.model_dump(mode="json", exclude_none=False),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@contextmanager
def _registry_file_lock(path: Path) -> Iterator[None]:
    """Serialize metadata/credential snapshots across processes.

    The per-process lock avoids platform-specific self-contention when multiple
    registry instances in one process share a store. The OS lock supplies the
    corresponding cross-process boundary. Callers hold this lock while reading both
    files or while committing/rolling back either file.
    """

    key = str(path.absolute())
    with _PROCESS_FILE_LOCKS_GUARD:
        process_lock = _PROCESS_FILE_LOCKS.setdefault(key, threading.RLock())
    with process_lock:
        parent_existed = path.parent.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix" and not parent_existed:
                os.chmod(path.parent, 0o700)
            flags = os.O_CREAT | os.O_RDWR
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags | no_follow, 0o600)
        except OSError as exc:
            raise ModelRegistryError(f"could not open model registry lock {path}") from exc

        try:
            if os.name == "posix":
                os.chmod(path, 0o600)
            with os.fdopen(descriptor, "r+b", buffering=0) as handle:
                descriptor = -1
                _acquire_os_file_lock(handle)
                try:
                    yield
                finally:
                    _release_os_file_lock(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _acquire_os_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_os_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
