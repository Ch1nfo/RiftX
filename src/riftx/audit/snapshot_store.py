"""Crash-durable same-node implementation of the SnapshotStore v1 CAS."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from .snapshot import (
    SNAPSHOT_CAS_INDEX_SCHEMA_VERSION,
    SNAPSHOT_MANIFEST_INDEX_NAME,
    SnapshotBlobMetadata,
    SnapshotBlobObjectType,
    SnapshotCASBinding,
    SnapshotCASDescriptor,
    SnapshotCASObjectType,
    SnapshotIntegrityResult,
    SnapshotStagedTree,
    SnapshotStagingCleanupReceipt,
    SnapshotStoreCrash,
    SnapshotStoreError,
    SnapshotStoreFailure,
    StoredSnapshotTree,
    parse_snapshot_content_storage_key,
)

_COPY_CHUNK_SIZE = 1024 * 1024
_DEFAULT_MAX_BLOB_BYTES = 5 * 1024 * 1024
_DEFAULT_MAX_TREE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_INDEX_BYTES = 256 * 1024 * 1024
_STAGING_PREFIX = "snapshot-staging-"


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


class SnapshotBlobReader:
    """One verified, bounded Snapshot blob descriptor or symlink payload."""

    def __init__(
        self,
        reader: BinaryIO,
        *,
        size: int,
        fingerprint: _FileFingerprint | None,
    ) -> None:
        self._reader = reader
        self.size = size
        self._remaining = size
        self._fingerprint = fingerprint
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def read(self, max_bytes: int) -> bytes:
        if self._closed:
            raise RuntimeError("Snapshot blob descriptor is closed")
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if self._remaining == 0:
            return b""
        chunk = self._reader.read(min(max_bytes, self._remaining))
        if not chunk:
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
        self._remaining -= len(chunk)
        return chunk

    def verify_complete(self) -> None:
        if self._closed:
            raise RuntimeError("Snapshot blob descriptor is closed")
        if self._remaining != 0 or self._reader.read(1):
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
        if self._fingerprint is not None:
            current = _FileFingerprint.from_stat(os.fstat(self._reader.fileno()))
            if current != self._fingerprint:
                raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._reader.close()

    def __enter__(self) -> SnapshotBlobReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class LocalSnapshotStore:
    """Private local CAS with owner-bound locators and insert-is-seal objects."""

    def __init__(
        self,
        root: Path,
        *,
        max_blob_bytes: int = _DEFAULT_MAX_BLOB_BYTES,
        max_tree_bytes: int = _DEFAULT_MAX_TREE_BYTES,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
            raise ValueError("SnapshotStore root must be an absolute canonical path")
        if not isinstance(max_blob_bytes, int) or max_blob_bytes < 1:
            raise ValueError("max_blob_bytes must be positive")
        if (
            not isinstance(max_tree_bytes, int)
            or max_tree_bytes < max_blob_bytes
        ):
            raise ValueError("max_tree_bytes must be at least max_blob_bytes")
        absolute = Path(os.path.abspath(os.fspath(root)))
        absolute.mkdir(parents=True, exist_ok=True)
        if absolute.resolve() != absolute:
            raise ValueError("SnapshotStore root must not traverse symlinks")
        self._root = absolute
        self._objects = absolute / "objects"
        self._staging = absolute / "staging"
        self._quarantine = absolute / "quarantine"
        self._locks = absolute / "locks"
        self._max_blob_bytes = max_blob_bytes
        self._max_tree_bytes = max_tree_bytes
        self._fault_injector = fault_injector
        for directory in (
            self._root,
            self._objects,
            self._staging,
            self._quarantine,
            self._locks,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            _harden_private_directory(directory)

    @property
    def root(self) -> Path:
        return self._root

    def put_staged_tree(self, staged: SnapshotStagedTree) -> StoredSnapshotTree:
        if not isinstance(staged, SnapshotStagedTree):
            raise SnapshotStoreError(SnapshotStoreFailure.REQUEST_INVALID)
        descriptor = staged.descriptor
        if descriptor.total_bytes > self._max_tree_bytes or any(
            blob.size > self._max_blob_bytes for blob in descriptor.blobs
        ):
            raise SnapshotStoreError(SnapshotStoreFailure.SIZE_LIMIT_EXCEEDED)
        source_root = self._validated_source_root(staged.root)
        digest = descriptor.descriptor_digest
        destination = self._object_path(digest)
        staging_path: Path | None = None
        with _locked_file(self._locks / f"{digest}.lock"):
            if destination.exists() or destination.is_symlink():
                try:
                    persisted = self._verify_object(
                        destination,
                        expected_key=descriptor.content_storage_key,
                        expected_descriptor=descriptor,
                    )
                except SnapshotStoreError:
                    self._quarantine_object(destination, digest=digest)
                    raise
                return StoredSnapshotTree(
                    content_storage_key=persisted.content_storage_key,
                    descriptor_digest=persisted.descriptor_digest,
                    file_count=persisted.file_count,
                    total_bytes=persisted.total_bytes,
                    reused=True,
                )

            staging_path = self._staging / f"{_STAGING_PREFIX}{digest}-{uuid4().hex}"
            try:
                staging_path.mkdir(mode=0o700)
                self._inject("staging_created")
                self._copy_declared_tree(source_root, staging_path, descriptor)
                self._write_index(staging_path, descriptor)
                _seal_tree(staging_path, seal_root=False)
                self._inject("staging_synced")
                destination.parent.mkdir(parents=True, exist_ok=True)
                _harden_private_directory(destination.parent)
                os.rename(staging_path, destination)
                os.chmod(destination, 0o550, follow_symlinks=False)
                _fsync_directory(destination)
                _fsync_directory(destination.parent)
                staging_path = None
                self._inject("published")
                persisted = self._verify_object(
                    destination,
                    expected_key=descriptor.content_storage_key,
                    expected_descriptor=descriptor,
                )
                return StoredSnapshotTree(
                    content_storage_key=persisted.content_storage_key,
                    descriptor_digest=persisted.descriptor_digest,
                    file_count=persisted.file_count,
                    total_bytes=persisted.total_bytes,
                    reused=False,
                )
            except SnapshotStoreCrash:
                raise
            except SnapshotStoreError:
                if staging_path is not None:
                    _remove_tree(staging_path)
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                if staging_path is not None:
                    _remove_tree(staging_path)
                raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_UNAVAILABLE) from exc

    def open_blob(
        self,
        binding: SnapshotCASBinding,
        content_storage_key: str,
        relative_path: str,
        expected_blob_digest: str,
        *,
        max_bytes: int,
    ) -> SnapshotBlobReader:
        if not isinstance(binding, SnapshotCASBinding) or not isinstance(max_bytes, int):
            raise SnapshotStoreError(SnapshotStoreFailure.REQUEST_INVALID)
        try:
            descriptor = self._verify_object(
                self._path_for_key(content_storage_key),
                expected_key=content_storage_key,
            )
        except ValueError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.REQUEST_INVALID) from exc
        if not binding.accepts(descriptor):
            raise SnapshotStoreError(SnapshotStoreFailure.OWNER_MISMATCH)
        metadata = next(
            (blob for blob in descriptor.blobs if blob.relative_path == relative_path),
            None,
        )
        if metadata is None:
            raise SnapshotStoreError(SnapshotStoreFailure.BLOB_MISSING)
        if metadata.blob_digest != expected_blob_digest:
            raise SnapshotStoreError(SnapshotStoreFailure.MANIFEST_MISMATCH)
        if max_bytes < 0 or metadata.size > max_bytes:
            raise SnapshotStoreError(SnapshotStoreFailure.SIZE_LIMIT_EXCEEDED)
        object_path = self._path_for_key(content_storage_key)
        blob_path = object_path.joinpath(*metadata.relative_path.split("/"))
        if metadata.object_type is SnapshotBlobObjectType.SYMLINK:
            content = self._verified_symlink_bytes(blob_path, metadata)
            return SnapshotBlobReader(
                io.BytesIO(content),
                size=len(content),
                fingerprint=None,
            )
        return self._open_verified_regular(blob_path, metadata)

    def verify(
        self,
        binding: SnapshotCASBinding,
        content_storage_key: str,
    ) -> SnapshotIntegrityResult:
        if not isinstance(binding, SnapshotCASBinding):
            raise SnapshotStoreError(SnapshotStoreFailure.REQUEST_INVALID)
        try:
            object_path = self._path_for_key(content_storage_key)
        except ValueError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.REQUEST_INVALID) from exc
        try:
            descriptor = self._verify_object(
                object_path,
                expected_key=content_storage_key,
            )
            if not binding.accepts(descriptor):
                raise SnapshotStoreError(SnapshotStoreFailure.OWNER_MISMATCH)
        except SnapshotStoreError as exc:
            return SnapshotIntegrityResult(
                valid=False,
                content_storage_key=content_storage_key,
                descriptor_digest=None,
                file_count=0,
                total_bytes=0,
                failure=exc.failure,
            )
        return SnapshotIntegrityResult(
            valid=True,
            content_storage_key=content_storage_key,
            descriptor_digest=descriptor.descriptor_digest,
            file_count=descriptor.file_count,
            total_bytes=descriptor.total_bytes,
        )

    def cleanup_staging_orphans(
        self,
        *,
        older_than: datetime,
        dry_run: bool,
    ) -> SnapshotStagingCleanupReceipt:
        if not isinstance(older_than, datetime) or older_than.utcoffset() is None:
            raise ValueError("older_than must be timezone-aware")
        examined = 0
        eligible = 0
        removed = 0
        removed_bytes = 0
        cutoff = older_than.timestamp()
        with _locked_file(self._locks / "staging-cleanup.lock"):
            for candidate in sorted(self._staging.iterdir(), key=lambda path: path.name):
                examined += 1
                try:
                    value = candidate.lstat()
                except FileNotFoundError:
                    continue
                if (
                    not candidate.name.startswith(_STAGING_PREFIX)
                    or not stat.S_ISDIR(value.st_mode)
                    or stat.S_ISLNK(value.st_mode)
                    or value.st_mtime > cutoff
                ):
                    continue
                eligible += 1
                size = _tree_allocated_bytes(candidate)
                if not dry_run:
                    _remove_tree(candidate)
                    if candidate.exists() or candidate.is_symlink():
                        continue
                    removed += 1
                    removed_bytes += size
            if not dry_run:
                _fsync_directory(self._staging)
        return SnapshotStagingCleanupReceipt(
            examined=examined,
            eligible=eligible,
            removed=removed,
            removed_bytes=removed_bytes,
            dry_run=bool(dry_run),
            completed_at=datetime.now(UTC),
        )

    def _validated_source_root(self, value: Path) -> Path:
        absolute = Path(os.path.abspath(os.fspath(value)))
        try:
            source_stat = absolute.lstat()
        except OSError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_UNAVAILABLE) from exc
        if not stat.S_ISDIR(source_stat.st_mode) or stat.S_ISLNK(source_stat.st_mode):
            raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)
        try:
            resolved = absolute.resolve(strict=True)
        except OSError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_UNAVAILABLE) from exc
        if resolved != absolute or _paths_overlap(resolved, self._root):
            raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)
        return resolved

    def _copy_declared_tree(
        self,
        source_root: Path,
        destination_root: Path,
        descriptor: SnapshotCASDescriptor,
    ) -> None:
        observed = _collect_leaf_entries(source_root)
        declared = {blob.relative_path: blob for blob in descriptor.blobs}
        if set(observed) != set(declared):
            raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)
        total = 0
        for relative_path, metadata in declared.items():
            source = observed[relative_path]
            destination = destination_root.joinpath(*relative_path.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if metadata.object_type is SnapshotBlobObjectType.SYMLINK:
                total += self._copy_symlink(source, destination, metadata)
            else:
                total += self._copy_regular(source, destination, metadata)
            if total > self._max_tree_bytes:
                raise SnapshotStoreError(SnapshotStoreFailure.SIZE_LIMIT_EXCEEDED)
        if total != descriptor.total_bytes:
            raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)

    def _copy_regular(
        self,
        source: Path,
        destination: Path,
        metadata: SnapshotBlobMetadata,
    ) -> int:
        source_fd = -1
        destination_fd = -1
        try:
            source_fd = os.open(
                source,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            initial = _FileFingerprint.from_stat(os.fstat(source_fd))
            executable = bool(initial.mode & 0o111)
            if (
                not stat.S_ISREG(initial.mode)
                or initial.links != 1
                or initial.size != metadata.size
                or executable != (metadata.mode == 0o100755)
                or initial.size > self._max_blob_bytes
            ):
                raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)
            destination_fd = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            digest = hashlib.sha256()
            copied = 0
            while chunk := os.read(source_fd, _COPY_CHUNK_SIZE):
                copied += len(chunk)
                if copied > metadata.size or copied > self._max_blob_bytes:
                    raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)
                _write_all(destination_fd, chunk)
                digest.update(chunk)
            final_source = _FileFingerprint.from_stat(os.fstat(source_fd))
            current_entry = source.lstat()
            if (
                copied != metadata.size
                or digest.hexdigest() != metadata.blob_digest
                or final_source != initial
                or current_entry.st_dev != initial.device
                or current_entry.st_ino != initial.inode
            ):
                raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)
            os.fsync(destination_fd)
            os.fchmod(destination_fd, 0o550 if executable else 0o440)
            os.fsync(destination_fd)
            return copied
        except SnapshotStoreError:
            raise
        except OSError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_UNAVAILABLE) from exc
        finally:
            _close_fd(source_fd)
            _close_fd(destination_fd)

    def _copy_symlink(
        self,
        source: Path,
        destination: Path,
        metadata: SnapshotBlobMetadata,
    ) -> int:
        try:
            value = source.lstat()
            if not stat.S_ISLNK(value.st_mode):
                raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)
            target = os.readlink(source)
            content = os.fsencode(target)
            if (
                len(content) != metadata.size
                or len(content) > self._max_blob_bytes
                or hashlib.sha256(content).hexdigest() != metadata.blob_digest
            ):
                raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)
            os.symlink(target, destination)
            return len(content)
        except SnapshotStoreError:
            raise
        except OSError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_UNAVAILABLE) from exc

    def _write_index(self, root: Path, descriptor: SnapshotCASDescriptor) -> None:
        payload = {
            "content_storage_key": descriptor.content_storage_key,
            "descriptor": descriptor.canonical_payload(),
            "descriptor_digest": descriptor.descriptor_digest,
            "schema_version": SNAPSHOT_CAS_INDEX_SCHEMA_VERSION,
        }
        content = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(content) > _MAX_INDEX_BYTES:
            raise SnapshotStoreError(SnapshotStoreFailure.SIZE_LIMIT_EXCEEDED)
        path = root / SNAPSHOT_MANIFEST_INDEX_NAME
        descriptor_fd = -1
        try:
            descriptor_fd = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            _write_all(descriptor_fd, content)
            os.fsync(descriptor_fd)
            os.fchmod(descriptor_fd, 0o440)
            os.fsync(descriptor_fd)
        except OSError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_UNAVAILABLE) from exc
        finally:
            _close_fd(descriptor_fd)

    def _verify_object(
        self,
        object_path: Path,
        *,
        expected_key: str,
        expected_descriptor: SnapshotCASDescriptor | None = None,
    ) -> SnapshotCASDescriptor:
        try:
            root_stat = object_path.lstat()
        except FileNotFoundError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_MISSING) from exc
        except OSError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY) from exc
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_ISLNK(root_stat.st_mode)
            or root_stat.st_mode & 0o222
        ):
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
        descriptor = _read_descriptor(object_path / SNAPSHOT_MANIFEST_INDEX_NAME)
        if (
            descriptor.content_storage_key != expected_key
            or (expected_descriptor is not None and descriptor != expected_descriptor)
        ):
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
        try:
            observed = _collect_leaf_entries(
                object_path,
                ignored=frozenset({SNAPSHOT_MANIFEST_INDEX_NAME}),
            )
        except SnapshotStoreError as exc:
            raise SnapshotStoreError(
                SnapshotStoreFailure.STORAGE_INTEGRITY
            ) from exc
        declared = {blob.relative_path: blob for blob in descriptor.blobs}
        if set(observed) != set(declared):
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
        total = 0
        for relative_path, metadata in declared.items():
            path = observed[relative_path]
            if metadata.object_type is SnapshotBlobObjectType.SYMLINK:
                total += len(self._verified_symlink_bytes(path, metadata))
            else:
                total += _verify_sealed_regular(path, metadata)
        if total != descriptor.total_bytes:
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
        _verify_sealed_directories(object_path)
        return descriptor

    def _verified_symlink_bytes(
        self,
        path: Path,
        metadata: SnapshotBlobMetadata,
    ) -> bytes:
        try:
            value = path.lstat()
            if not stat.S_ISLNK(value.st_mode):
                raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
            content = os.fsencode(os.readlink(path))
        except SnapshotStoreError:
            raise
        except OSError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY) from exc
        if (
            len(content) != metadata.size
            or hashlib.sha256(content).hexdigest() != metadata.blob_digest
        ):
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
        return content

    def _open_verified_regular(
        self,
        path: Path,
        metadata: SnapshotBlobMetadata,
    ) -> SnapshotBlobReader:
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            initial = _FileFingerprint.from_stat(os.fstat(descriptor))
            _validate_sealed_fingerprint(initial, metadata)
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(descriptor, _COPY_CHUNK_SIZE):
                size += len(chunk)
                if size > metadata.size:
                    raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
                digest.update(chunk)
            final = _FileFingerprint.from_stat(os.fstat(descriptor))
            if (
                size != metadata.size
                or digest.hexdigest() != metadata.blob_digest
                or final != initial
            ):
                raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
            os.lseek(descriptor, 0, os.SEEK_SET)
            reader = os.fdopen(descriptor, "rb", buffering=0, closefd=True)
            descriptor = -1
            return SnapshotBlobReader(
                reader,
                size=metadata.size,
                fingerprint=initial,
            )
        except SnapshotStoreError:
            raise
        except OSError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY) from exc
        finally:
            _close_fd(descriptor)

    def _path_for_key(self, content_storage_key: str) -> Path:
        return self._object_path(parse_snapshot_content_storage_key(content_storage_key))

    def _object_path(self, digest: str) -> Path:
        return self._objects / digest[:2] / digest

    def _quarantine_object(self, path: Path, *, digest: str) -> None:
        try:
            quarantine = self._quarantine / f"{digest}-{uuid4().hex}"
            # macOS denies renaming a read-only directory across parents. The
            # object is already rejected and remains private, so restore only
            # owner-write on its root long enough to atomically isolate it.
            if not path.is_symlink():
                os.chmod(path, 0o700, follow_symlinks=False)
            os.rename(path, quarantine)
            _fsync_directory(path.parent)
            _fsync_directory(self._quarantine)
        except OSError as exc:
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY) from exc

    def _inject(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)


def _read_descriptor(path: Path) -> SnapshotCASDescriptor:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_size < 2
            or value.st_size > _MAX_INDEX_BYTES
            or value.st_mode & 0o222
        ):
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
        content = bytearray()
        while chunk := os.read(descriptor, _COPY_CHUNK_SIZE):
            content.extend(chunk)
            if len(content) > _MAX_INDEX_BYTES:
                raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
    except SnapshotStoreError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY) from exc
    finally:
        _close_fd(descriptor)
    try:
        payload = json.loads(bytes(content))
        if not isinstance(payload, dict) or set(payload) != {
            "content_storage_key",
            "descriptor",
            "descriptor_digest",
            "schema_version",
        }:
            raise ValueError("invalid index keys")
        if payload["schema_version"] != SNAPSHOT_CAS_INDEX_SCHEMA_VERSION:
            raise ValueError("invalid index schema")
        raw_descriptor = payload["descriptor"]
        if not isinstance(raw_descriptor, dict) or set(raw_descriptor) != {
            "blobs",
            "file_count",
            "manifest_digest",
            "object_type",
            "project_id",
            "schema_version",
            "snapshot_digest",
            "total_bytes",
        }:
            raise ValueError("invalid descriptor keys")
        raw_blobs = raw_descriptor["blobs"]
        if not isinstance(raw_blobs, list):
            raise ValueError("invalid descriptor blobs")
        blobs = tuple(
            SnapshotBlobMetadata(
                relative_path=blob["relative_path"],
                blob_digest=blob["blob_digest"],
                size=blob["size"],
                mode=blob["mode"],
                object_type=SnapshotBlobObjectType(blob["object_type"]),
            )
            for blob in raw_blobs
            if isinstance(blob, dict)
            and set(blob) == {
                "blob_digest",
                "mode",
                "object_type",
                "relative_path",
                "size",
            }
        )
        if len(blobs) != len(raw_blobs):
            raise ValueError("invalid descriptor blob")
        descriptor_value = SnapshotCASDescriptor(
            project_id=raw_descriptor["project_id"],
            snapshot_digest=raw_descriptor["snapshot_digest"],
            manifest_digest=raw_descriptor["manifest_digest"],
            blobs=blobs,
            object_type=SnapshotCASObjectType(raw_descriptor["object_type"]),
        )
        if (
            raw_descriptor["schema_version"] != descriptor_value.schema_version
            or raw_descriptor["file_count"] != descriptor_value.file_count
            or raw_descriptor["total_bytes"] != descriptor_value.total_bytes
            or payload["descriptor_digest"] != descriptor_value.descriptor_digest
            or payload["content_storage_key"] != descriptor_value.content_storage_key
        ):
            raise ValueError("descriptor redundancy mismatch")
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if canonical != bytes(content):
            raise ValueError("Snapshot CAS index is not canonical")
        return descriptor_value
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY) from exc


def _collect_leaf_entries(
    root: Path,
    *,
    ignored: frozenset[str] = frozenset(),
) -> dict[str, Path]:
    observed: dict[str, Path] = {}
    try:
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for name in tuple(directories):
                candidate = current_path / name
                value = candidate.lstat()
                if stat.S_ISLNK(value.st_mode):
                    directories.remove(name)
                    relative = candidate.relative_to(root).as_posix()
                    if relative not in ignored:
                        observed[relative] = candidate
                elif not stat.S_ISDIR(value.st_mode):
                    raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_INTEGRITY)
            for name in files:
                candidate = current_path / name
                relative = candidate.relative_to(root).as_posix()
                if relative not in ignored:
                    observed[relative] = candidate
    except SnapshotStoreError:
        raise
    except OSError as exc:
        raise SnapshotStoreError(SnapshotStoreFailure.SOURCE_UNAVAILABLE) from exc
    return observed


def _verify_sealed_regular(path: Path, metadata: SnapshotBlobMetadata) -> int:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        initial = _FileFingerprint.from_stat(os.fstat(descriptor))
        _validate_sealed_fingerprint(initial, metadata)
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, _COPY_CHUNK_SIZE):
            size += len(chunk)
            if size > metadata.size:
                raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
            digest.update(chunk)
        final = _FileFingerprint.from_stat(os.fstat(descriptor))
        if (
            size != metadata.size
            or digest.hexdigest() != metadata.blob_digest
            or final != initial
        ):
            raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
        return size
    except SnapshotStoreError:
        raise
    except OSError as exc:
        raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY) from exc
    finally:
        _close_fd(descriptor)


def _validate_sealed_fingerprint(
    value: _FileFingerprint,
    metadata: SnapshotBlobMetadata,
) -> None:
    executable = bool(value.mode & 0o111)
    if (
        not stat.S_ISREG(value.mode)
        or value.links != 1
        or value.size != metadata.size
        or value.mode & 0o222
        or executable != (metadata.mode == 0o100755)
    ):
        raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)


def _verify_sealed_directories(root: Path) -> None:
    try:
        for current, directories, _files in os.walk(root, topdown=True, followlinks=False):
            value = Path(current).lstat()
            if not stat.S_ISDIR(value.st_mode) or value.st_mode & 0o222:
                raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
            for name in directories:
                candidate = Path(current) / name
                entry = candidate.lstat()
                if stat.S_ISLNK(entry.st_mode):
                    continue
                if not stat.S_ISDIR(entry.st_mode) or entry.st_mode & 0o222:
                    raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY)
    except SnapshotStoreError:
        raise
    except OSError as exc:
        raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_INTEGRITY) from exc


def _seal_tree(root: Path, *, seal_root: bool) -> None:
    directories: list[Path] = []
    try:
        for current, child_directories, _files in os.walk(
            root,
            topdown=False,
            followlinks=False,
        ):
            current_path = Path(current)
            directories.append(current_path)
            for name in child_directories:
                candidate = current_path / name
                if candidate.is_symlink():
                    continue
        for directory in directories:
            os.chmod(
                directory,
                0o550 if seal_root or directory != root else 0o700,
                follow_symlinks=False,
            )
            _fsync_directory(directory)
    except OSError as exc:
        raise SnapshotStoreError(SnapshotStoreFailure.STORAGE_UNAVAILABLE) from exc


def _harden_private_directory(path: Path) -> None:
    try:
        value = path.lstat()
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
            raise ValueError("SnapshotStore path is not a directory")
        if hasattr(os, "geteuid") and value.st_uid != os.geteuid():
            raise ValueError("SnapshotStore directory has a foreign owner")
        if value.st_mode & 0o077:
            os.chmod(path, 0o700, follow_symlinks=False)
            value = path.lstat()
        if value.st_mode & 0o077:
            raise ValueError("SnapshotStore directory is not private")
    except OSError as exc:
        raise ValueError("SnapshotStore directory cannot be secured") from exc


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("SnapshotStore write made no progress")
        remaining = remaining[written:]


def _tree_allocated_bytes(root: Path) -> int:
    total = 0
    try:
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            total += current_path.lstat().st_size
            for name in tuple(directories):
                candidate = current_path / name
                value = candidate.lstat()
                if stat.S_ISLNK(value.st_mode):
                    directories.remove(name)
                    total += value.st_size
            for name in files:
                total += (current_path / name).lstat().st_size
    except OSError:
        return 0
    return total


def _remove_tree(path: Path) -> None:
    try:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
            return
        for current, directories, _files in os.walk(path, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in directories:
                candidate = current_path / name
                if not candidate.is_symlink():
                    os.chmod(candidate, 0o700, follow_symlinks=False)
            os.chmod(current_path, 0o700, follow_symlinks=False)
        shutil.rmtree(path)
    except OSError:
        return


@contextmanager
def _locked_file(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _close_fd(descriptor: int) -> None:
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError:
            pass


__all__ = ["LocalSnapshotStore", "SnapshotBlobReader"]
