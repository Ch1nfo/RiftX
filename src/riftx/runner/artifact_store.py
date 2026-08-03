"""Descriptor-safe local storage for immutable Artifact bytes."""

from __future__ import annotations

import hashlib
import os
import stat
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from .paths import RunnerPaths

_COPY_CHUNK_SIZE = 1024 * 1024
_PARTIAL_NAME = ".content.partial"
_DEFAULT_VERIFIED_FINGERPRINTS = 128
_VERIFICATION_LOCK_STRIPES = 64


class ArtifactContentFailure(StrEnum):
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_OUTSIDE_ROOT = "source_outside_root"
    SOURCE_NOT_REGULAR = "source_not_regular"
    SOURCE_LINKED = "source_linked"
    SOURCE_CHANGED = "source_changed"
    SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"
    DECLARED_CONTENT_MISMATCH = "declared_content_mismatch"
    STORAGE_MISSING = "storage_missing"
    STORAGE_INTEGRITY = "storage_integrity"
    STORAGE_UNAVAILABLE = "storage_unavailable"


class ArtifactContentStoreError(RuntimeError):
    """Stable, path-free failure raised by the local Artifact byte store."""

    def __init__(self, failure: ArtifactContentFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class StoredArtifactContent:
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _FileFingerprint:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            links=value.st_nlink,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
        )


class OpenedArtifactContent:
    """One already-verified content descriptor; this object never reopens a path."""

    def __init__(
        self,
        reader: BinaryIO,
        *,
        size: int,
        fingerprint: _FileFingerprint,
    ) -> None:
        self._reader = reader
        self.size = size
        self._fingerprint = fingerprint
        self._remaining = size
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def seek(self, offset: int) -> None:
        if self._closed:
            raise RuntimeError("Artifact content descriptor is closed")
        if offset < 0 or offset > self.size:
            raise ValueError("Artifact offset is outside the content bounds")
        self._reader.seek(offset)
        self._remaining = self.size - offset

    def read(self, max_bytes: int) -> bytes:
        if self._closed:
            raise RuntimeError("Artifact content descriptor is closed")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if self._remaining == 0:
            return b""
        chunk = self._reader.read(min(max_bytes, self._remaining))
        if not chunk:
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
        self._remaining -= len(chunk)
        return chunk

    def verify_complete(self) -> None:
        if self._closed:
            raise RuntimeError("Artifact content descriptor is closed")
        if self._remaining != 0 or self._reader.read(1):
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
        current = _FileFingerprint.from_stat(os.fstat(self._reader.fileno()))
        if current != self._fingerprint:
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)

    def verify_unchanged(self) -> None:
        """Revalidate the held inode after a bounded slice read."""

        if self._closed:
            raise RuntimeError("Artifact content descriptor is closed")
        current = _FileFingerprint.from_stat(os.fstat(self._reader.fileno()))
        if current != self._fingerprint:
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._reader.close()

    def __enter__(self) -> OpenedArtifactContent:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class LocalArtifactContentStore:
    """Store and open immutable bytes below one private Runner root."""

    def __init__(
        self,
        paths: RunnerPaths,
        *,
        max_artifact_bytes: int,
        max_verified_fingerprints: int = _DEFAULT_VERIFIED_FINGERPRINTS,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        if max_verified_fingerprints < 1:
            raise ValueError("max_verified_fingerprints must be positive")
        _require_descriptor_features()
        self._paths = paths
        self._max_artifact_bytes = max_artifact_bytes
        self._max_verified_fingerprints = max_verified_fingerprints
        self._verified_fingerprints: OrderedDict[tuple[str, str, int], _FileFingerprint] = (
            OrderedDict()
        )
        self._verified_fingerprints_lock = Lock()
        self._verification_locks = tuple(Lock() for _ in range(_VERIFICATION_LOCK_STRIPES))

    @property
    def max_artifact_bytes(self) -> int:
        return self._max_artifact_bytes

    def snapshot_file(
        self,
        source_path: str,
        *,
        allowed_roots: Iterable[Path],
        storage_key: str,
    ) -> StoredArtifactContent:
        source_fd = -1
        source_parent_fd = -1
        destination_fd = -1
        destination_parent_fd = -1
        staging_fd = -1
        source_name = ""
        destination_directory_name = ""
        source_opened = False
        try:
            source_fd, source_parent_fd, source_name = _open_source_beneath(
                source_path,
                allowed_roots=allowed_roots,
            )
            source_opened = True
            initial_stat = os.fstat(source_fd)
            initial = _validate_source(initial_stat, self._max_artifact_bytes)
            (
                destination_fd,
                destination_parent_fd,
                destination_directory_name,
                destination_name,
            ) = self._create_destination_directory(storage_key)
            digest = hashlib.sha256()
            size = 0
            staging_fd = _open_staging(destination_fd)
            writer: BinaryIO | None = None
            try:
                writer = os.fdopen(staging_fd, "w+b", closefd=True)
                staging_fd = -1
                try:
                    while chunk := _read_fd_chunk(source_fd, _COPY_CHUNK_SIZE):
                        size += len(chunk)
                        if size > initial.size:
                            raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_CHANGED)
                        if size > self._max_artifact_bytes:
                            raise ArtifactContentStoreError(
                                ArtifactContentFailure.SIZE_LIMIT_EXCEEDED
                            )
                        _write_all(writer, chunk)
                        digest.update(chunk)
                    if size != initial.size:
                        raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_CHANGED)
                    _verify_source_unchanged(
                        source_fd,
                        source_parent_fd,
                        source_name,
                        initial,
                    )
                    _sync_staging(writer)
                    _verify_staging_content(
                        writer,
                        expected_sha256=digest.hexdigest(),
                        expected_size=size,
                    )
                    _publish_staging(
                        destination_fd,
                        destination_name,
                        staging_fd=writer.fileno(),
                        expected_size=size,
                    )
                finally:
                    writer.close()
            except Exception:
                _close_fd(staging_fd)
                staging_fd = -1
                _unlink_if_present(destination_fd, _PARTIAL_NAME)
                _unlink_if_present(destination_fd, destination_name)
                raise
            return StoredArtifactContent(sha256=digest.hexdigest(), size=size)
        except ArtifactContentStoreError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArtifactContentStoreError(
                ArtifactContentFailure.STORAGE_UNAVAILABLE
                if source_opened
                else ArtifactContentFailure.SOURCE_UNAVAILABLE
            ) from exc
        finally:
            _close_fd(source_fd)
            _close_fd(source_parent_fd)
            _close_fd(destination_fd)
            _remove_empty_directory_at(
                destination_parent_fd,
                destination_directory_name,
            )
            _close_fd(destination_parent_fd)

    def snapshot_bytes(
        self,
        content: bytes,
        *,
        storage_key: str,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> StoredArtifactContent:
        size = len(content)
        if size > self._max_artifact_bytes:
            raise ArtifactContentStoreError(ArtifactContentFailure.SIZE_LIMIT_EXCEEDED)
        digest = hashlib.sha256(content).hexdigest()
        if (expected_size is not None and expected_size != size) or (
            expected_sha256 is not None and expected_sha256 != digest
        ):
            raise ArtifactContentStoreError(ArtifactContentFailure.DECLARED_CONTENT_MISMATCH)

        destination_fd = -1
        destination_parent_fd = -1
        staging_fd = -1
        destination_directory_name = ""
        try:
            (
                destination_fd,
                destination_parent_fd,
                destination_directory_name,
                destination_name,
            ) = self._create_destination_directory(storage_key)
            staging_fd = _open_staging(destination_fd)
            writer: BinaryIO | None = None
            try:
                writer = os.fdopen(staging_fd, "w+b", closefd=True)
                staging_fd = -1
                try:
                    for start in range(0, size, _COPY_CHUNK_SIZE):
                        _write_all(writer, content[start : start + _COPY_CHUNK_SIZE])
                    _sync_staging(writer)
                    _verify_staging_content(
                        writer,
                        expected_sha256=digest,
                        expected_size=size,
                    )
                    _publish_staging(
                        destination_fd,
                        destination_name,
                        staging_fd=writer.fileno(),
                        expected_size=size,
                    )
                finally:
                    writer.close()
            except Exception:
                _close_fd(staging_fd)
                staging_fd = -1
                _unlink_if_present(destination_fd, _PARTIAL_NAME)
                _unlink_if_present(destination_fd, destination_name)
                raise
            return StoredArtifactContent(sha256=digest, size=size)
        except ArtifactContentStoreError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_UNAVAILABLE) from exc
        finally:
            _close_fd(destination_fd)
            _remove_empty_directory_at(
                destination_parent_fd,
                destination_directory_name,
            )
            _close_fd(destination_parent_fd)

    def open_verified(
        self,
        *,
        storage_key: str,
        expected_sha256: str,
        expected_size: int,
    ) -> OpenedArtifactContent:
        if expected_size < 0 or expected_size > self._max_artifact_bytes:
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
        file_fd = -1
        parent_fd = -1
        try:
            relative = self._validated_storage_relative(storage_key)
            file_fd, parent_fd, name = _open_relative_file(self._paths.root, relative.parts)
            initial_stat = os.fstat(file_fd)
            initial = _validate_stored(initial_stat, expected_size)
            cache_key = (storage_key, expected_sha256, expected_size)
            verification_lock = self._verification_locks[
                hash(storage_key) % len(self._verification_locks)
            ]
            with verification_lock:
                _verify_entry_unchanged(file_fd, parent_fd, name, initial)
                if not self._is_verified_fingerprint_cached(cache_key, initial):
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := _read_fd_chunk(file_fd, _COPY_CHUNK_SIZE):
                        size += len(chunk)
                        if size > expected_size or size > self._max_artifact_bytes:
                            raise ArtifactContentStoreError(
                                ArtifactContentFailure.STORAGE_INTEGRITY
                            )
                        digest.update(chunk)
                    if size != expected_size or digest.hexdigest() != expected_sha256:
                        raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
                    _verify_entry_unchanged(file_fd, parent_fd, name, initial)
                    self._remember_verified_fingerprint(cache_key, initial)
                os.lseek(file_fd, 0, os.SEEK_SET)
                reader = os.fdopen(file_fd, "rb", buffering=0, closefd=True)
                file_fd = -1
                return OpenedArtifactContent(
                    reader,
                    size=expected_size,
                    fingerprint=initial,
                )
        except ArtifactContentStoreError:
            raise
        except FileNotFoundError as exc:
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_MISSING) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY) from exc
        finally:
            _close_fd(file_fd)
            _close_fd(parent_fd)

    def discard(self, storage_key: str) -> None:
        """Best-effort removal of an uncommitted Artifact directory."""

        parent_fd = -1
        directory_fd = -1
        try:
            relative = self._validated_storage_relative(storage_key)
            parent_fd = _open_relative_directory(self._paths.root, relative.parts[:-2])
            artifact_id = relative.parts[-2]
            directory_fd = os.open(artifact_id, _directory_flags(), dir_fd=parent_fd)
            for name in (_PARTIAL_NAME, relative.parts[-1]):
                _unlink_if_present(directory_fd, name)
            _close_fd(directory_fd)
            directory_fd = -1
            _remove_empty_directory_at(parent_fd, artifact_id)
        except (ArtifactContentStoreError, OSError):
            return
        finally:
            _close_fd(directory_fd)
            _close_fd(parent_fd)

    def _create_destination_directory(
        self,
        storage_key: str,
    ) -> tuple[int, int, str, str]:
        relative = self._validated_storage_relative(storage_key)
        self._paths.root.mkdir(parents=True, exist_ok=True)
        current_fd = os.open(self._paths.root, _directory_flags())
        try:
            _validate_storage_directory(current_fd, private=False)
            for index, component in enumerate(relative.parts[:-1]):
                exclusive = index == len(relative.parts) - 2
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    if exclusive:
                        raise
                if exclusive:
                    try:
                        os.fsync(current_fd)
                        next_fd = os.open(
                            component,
                            _directory_flags(),
                            dir_fd=current_fd,
                        )
                        try:
                            _validate_storage_directory(next_fd, private=index >= 2)
                        except Exception:
                            _close_fd(next_fd)
                            raise
                    except Exception:
                        _remove_empty_directory_at(current_fd, component)
                        raise
                    parent_fd = current_fd
                    current_fd = -1
                    return next_fd, parent_fd, component, relative.parts[-1]
                os.fsync(current_fd)
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
                try:
                    _validate_storage_directory(next_fd, private=index >= 2)
                except Exception:
                    _close_fd(next_fd)
                    raise
                _close_fd(current_fd)
                current_fd = next_fd
            raise ValueError("Artifact storage key has no object directory")
        except Exception:
            _close_fd(current_fd)
            raise

    def _validated_storage_relative(self, storage_key: str) -> Path:
        content = self._paths.artifact_from_storage_key(storage_key)
        return content.relative_to(self._paths.root)

    def _is_verified_fingerprint_cached(
        self,
        cache_key: tuple[str, str, int],
        fingerprint: _FileFingerprint,
    ) -> bool:
        with self._verified_fingerprints_lock:
            if self._verified_fingerprints.get(cache_key) != fingerprint:
                return False
            self._verified_fingerprints.move_to_end(cache_key)
            return True

    def _remember_verified_fingerprint(
        self,
        cache_key: tuple[str, str, int],
        fingerprint: _FileFingerprint,
    ) -> None:
        with self._verified_fingerprints_lock:
            self._verified_fingerprints[cache_key] = fingerprint
            self._verified_fingerprints.move_to_end(cache_key)
            while len(self._verified_fingerprints) > self._max_verified_fingerprints:
                self._verified_fingerprints.popitem(last=False)


def _require_descriptor_features() -> None:
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required):
        raise RuntimeError("platform lacks required descriptor-safe Artifact operations")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _file_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _open_staging(directory_fd: int) -> int:
    return os.open(
        _PARTIAL_NAME,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )


def _open_source_beneath(
    source_path: str,
    *,
    allowed_roots: Iterable[Path],
) -> tuple[int, int, str]:
    expanded_source = Path(source_path).expanduser()
    if ".." in expanded_source.parts:
        raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_UNAVAILABLE)
    source = Path(os.path.abspath(os.fspath(expanded_source)))
    roots = sorted(
        {Path(os.path.abspath(os.fspath(root.expanduser()))) for root in allowed_roots},
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for root in roots:
        try:
            relative = source.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_NOT_REGULAR)
        try:
            return _open_relative_file(root, relative.parts, source_components=True)
        except ArtifactContentStoreError:
            raise
        except (OSError, ValueError) as exc:
            raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_UNAVAILABLE) from exc
    raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_OUTSIDE_ROOT)


def _open_relative_directory(
    root: Path,
    parts: tuple[str, ...],
    *,
    source_components: bool = False,
) -> int:
    current_fd = os.open(root, _directory_flags())
    try:
        if not source_components:
            _validate_storage_directory(current_fd, private=False)
        for index, component in enumerate(parts):
            if source_components:
                _validate_source_component(component)
            else:
                _validate_component(component)
            next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            if not source_components:
                try:
                    _validate_storage_directory(next_fd, private=index >= 2)
                except Exception:
                    _close_fd(next_fd)
                    raise
            _close_fd(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        _close_fd(current_fd)
        raise


def _open_relative_file(
    root: Path,
    parts: tuple[str, ...],
    *,
    source_components: bool = False,
) -> tuple[int, int, str]:
    if not parts:
        raise ValueError("relative file path is empty")
    parent_fd = _open_relative_directory(
        root,
        parts[:-1],
        source_components=source_components,
    )
    name = parts[-1]
    if source_components:
        _validate_source_component(name)
    else:
        _validate_component(name)
    try:
        file_fd = os.open(name, _file_flags(), dir_fd=parent_fd)
    except Exception:
        _close_fd(parent_fd)
        raise
    return file_fd, parent_fd, name


def _validate_component(value: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(not 0x20 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("unsafe path component")


def _validate_source_component(value: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\x00" in value:
        raise ValueError("unsafe source path component")


def _validate_storage_directory(file_fd: int, *, private: bool) -> None:
    value = os.fstat(file_fd)
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o022:
        raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
    if private and value.st_mode & 0o077:
        os.fchmod(file_fd, 0o700)
        os.fsync(file_fd)
        hardened = os.fstat(file_fd)
        if (
            not stat.S_ISDIR(hardened.st_mode)
            or hardened.st_uid != os.geteuid()
            or hardened.st_mode & 0o077
        ):
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)


def _validate_source(value: os.stat_result, max_bytes: int) -> _FileFingerprint:
    if not stat.S_ISREG(value.st_mode):
        raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_NOT_REGULAR)
    if value.st_nlink != 1:
        raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_LINKED)
    if value.st_size < 0 or value.st_size > max_bytes:
        raise ArtifactContentStoreError(ArtifactContentFailure.SIZE_LIMIT_EXCEEDED)
    return _FileFingerprint.from_stat(value)


def _validate_stored(value: os.stat_result, expected_size: int) -> _FileFingerprint:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_nlink != 1
        or value.st_size != expected_size
        or value.st_mode & 0o222
    ):
        raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
    return _FileFingerprint.from_stat(value)


def _verify_source_unchanged(
    file_fd: int,
    parent_fd: int,
    name: str,
    initial: _FileFingerprint,
) -> None:
    current = _FileFingerprint.from_stat(os.fstat(file_fd))
    if current != initial:
        raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_CHANGED)
    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if entry.st_dev != initial.device or entry.st_ino != initial.inode:
        raise ArtifactContentStoreError(ArtifactContentFailure.SOURCE_CHANGED)


def _verify_entry_unchanged(
    file_fd: int,
    parent_fd: int,
    name: str,
    initial: _FileFingerprint,
) -> None:
    current = _FileFingerprint.from_stat(os.fstat(file_fd))
    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if current != initial or entry.st_dev != initial.device or entry.st_ino != initial.inode:
        raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)


def _sync_staging(writer: BinaryIO) -> None:
    writer.flush()
    os.fsync(writer.fileno())
    os.fchmod(writer.fileno(), 0o444)
    os.fsync(writer.fileno())


def _write_all(writer: BinaryIO, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = writer.write(remaining)
        if written is None or written <= 0:
            raise OSError("Artifact staging write made no progress")
        remaining = remaining[written:]


def _verify_staging_content(
    writer: BinaryIO,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    writer.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := writer.read(_COPY_CHUNK_SIZE):
        size += len(chunk)
        if size > expected_size:
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
        digest.update(chunk)
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)


def _publish_staging(
    directory_fd: int,
    destination_name: str,
    *,
    staging_fd: int,
    expected_size: int,
) -> None:
    initial = _validate_publishable_staging(os.fstat(staging_fd), expected_size)
    partial = os.stat(_PARTIAL_NAME, dir_fd=directory_fd, follow_symlinks=False)
    if partial.st_dev != initial.device or partial.st_ino != initial.inode:
        raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
    os.replace(
        _PARTIAL_NAME,
        destination_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )
    published = os.stat(destination_name, dir_fd=directory_fd, follow_symlinks=False)
    current = _validate_publishable_staging(os.fstat(staging_fd), expected_size)
    if (
        published.st_dev != current.device
        or published.st_ino != current.inode
        or current.device != initial.device
        or current.inode != initial.inode
        or current.mode != initial.mode
        or current.links != initial.links
        or current.size != initial.size
        or current.modified_ns != initial.modified_ns
    ):
        raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
    os.fsync(directory_fd)
    final = _validate_publishable_staging(os.fstat(staging_fd), expected_size)
    final_entry = os.stat(destination_name, dir_fd=directory_fd, follow_symlinks=False)
    if final != current or final_entry.st_dev != final.device or final_entry.st_ino != final.inode:
        raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)


def _validate_publishable_staging(
    value: os.stat_result,
    expected_size: int,
) -> _FileFingerprint:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_nlink != 1
        or value.st_size != expected_size
        or value.st_mode & 0o222
    ):
        raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
    return _FileFingerprint.from_stat(value)


def _read_fd_chunk(file_fd: int, max_bytes: int) -> bytes:
    return os.read(file_fd, max_bytes)


def _unlink_if_present(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _close_fd(file_fd: int) -> None:
    if file_fd >= 0:
        try:
            os.close(file_fd)
        except OSError:
            pass


def _remove_empty_directory_at(parent_fd: int, name: str) -> None:
    if parent_fd < 0 or not name:
        return
    try:
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError:
        pass


__all__ = [
    "ArtifactContentFailure",
    "ArtifactContentStoreError",
    "LocalArtifactContentStore",
    "OpenedArtifactContent",
    "StoredArtifactContent",
]
