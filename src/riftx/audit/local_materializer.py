"""Descriptor-safe, deterministic materialization of a local source folder."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .paths import AuthorizedLocalSource, SourcePathAuthorizationError
from .source_manifest import (
    SourceCaptureDecision,
    SourceCapturePolicy,
    SourceCaptureReason,
    SourceClassification,
    SourceManifest,
    SourceManifestEntry,
    SourceManifestObjectType,
    SourceManifestOrigin,
    SourceManifestPath,
    SourceManifestSourceKind,
)

_STAGING_PREFIX = "local-source-materialization-"
_COPY_CHUNK_SIZE = 64 * 1024
_DEFAULT_MAX_PATH_BYTES = 4096
_DEFAULT_MAX_DIRECTORY_DEPTH = 128
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"


class LocalSourceMaterializationFailure(StrEnum):
    REQUEST_INVALID = "audit_local_snapshot_materializer_request_invalid"
    OUTPUT_INVALID = "audit_local_snapshot_materializer_output_invalid"
    SOURCE_CHANGED = "audit_local_source_changed_during_materialization"
    SOURCE_UNAVAILABLE = "audit_local_source_unavailable"
    SOURCE_LIMIT_EXCEEDED = "audit_local_source_limit_exceeded"
    OUTPUT_WRITE_FAILED = "audit_local_snapshot_materializer_write_failed"
    CLEANUP_FAILED = "audit_local_snapshot_materializer_cleanup_failed"


class LocalSourceMaterializationError(RuntimeError):
    """Stable, path-free local-source capture failure."""

    def __init__(self, failure: LocalSourceMaterializationFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class LocalMaterializedSource:
    root: Path = field(repr=False)
    manifest: SourceManifest
    source_identity_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("materialized source root must be absolute")
        if (
            not isinstance(self.source_identity_digest, str)
            or len(self.source_identity_digest) != 64
        ):
            raise ValueError("source identity digest is invalid")


@dataclass(frozen=True, slots=True)
class _NodeObservation:
    fingerprint: tuple[int, ...]
    content_digest: str | None = None


class LocalSourceMaterializer:
    """Copy selected source text without following links or invoking target tools."""

    def __init__(
        self,
        staging_parent: Path,
        *,
        max_path_bytes: int = _DEFAULT_MAX_PATH_BYTES,
        max_directory_depth: int = _DEFAULT_MAX_DIRECTORY_DEPTH,
        fault_injector: Callable[[str, bytes | None], None] | None = None,
    ) -> None:
        if not isinstance(staging_parent, Path) or not staging_parent.is_absolute():
            raise ValueError("materializer staging parent must be absolute")
        for label, value in (
            ("max_path_bytes", max_path_bytes),
            ("max_directory_depth", max_directory_depth),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} is invalid")
        absolute = Path(os.path.abspath(os.fspath(staging_parent)))
        try:
            canonical_parent = absolute.parent.resolve(strict=True)
        except OSError as exc:
            raise ValueError("materializer staging parent parent is unavailable") from exc
        if canonical_parent != absolute.parent:
            raise ValueError("materializer staging parent must not traverse symlinks")
        absolute.mkdir(exist_ok=True)
        if absolute.resolve(strict=True) != absolute:
            raise ValueError("materializer staging parent must not traverse symlinks")
        self._staging_parent = absolute
        self._max_path_bytes = max_path_bytes
        self._max_directory_depth = max_directory_depth
        self._fault_injector = fault_injector

    def materialize(
        self,
        source: AuthorizedLocalSource,
        *,
        policy: SourceCapturePolicy,
    ) -> LocalMaterializedSource:
        if not isinstance(source, AuthorizedLocalSource) or not isinstance(
            policy, SourceCapturePolicy
        ):
            raise LocalSourceMaterializationError(
                LocalSourceMaterializationFailure.REQUEST_INVALID
            )
        if (
            policy.include_paths != source.filters.include_paths
            or policy.exclude_paths != source.filters.exclude_paths
        ):
            raise LocalSourceMaterializationError(
                LocalSourceMaterializationFailure.REQUEST_INVALID
            )
        self._assert_output_disjoint(source)
        staging_root = Path(
            tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=self._staging_parent)
        )
        os.chmod(staging_root, 0o700)
        source_fd = staging_fd = -1
        try:
            source.verify_unchanged()
            source_fd = source.duplicate_source_fd()
            staging_fd = os.open(
                staging_root,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            observations: dict[bytes, _NodeObservation] = {}
            entries: list[SourceManifestEntry] = []
            counters = [0, 0]
            self._capture_directory(
                source_fd,
                staging_fd,
                prefix=b"",
                depth=0,
                policy=policy,
                observations=observations,
                entries=entries,
                counters=counters,
            )
            self._fault("before_source_recheck", None)
            self._verify_directory(
                source_fd,
                prefix=b"",
                depth=0,
                policy=policy,
                expected=observations,
                observed={},
                counters=[0],
            )
            source.verify_unchanged()
            manifest = SourceManifest.create(
                source_kind=SourceManifestSourceKind.DIRECTORY,
                commit_sha=None,
                head_commit_sha=None,
                capture_policy_digest=policy.digest,
                entries=tuple(entries),
            )
            return LocalMaterializedSource(
                root=staging_root,
                manifest=manifest,
                source_identity_digest=source.source_identity_digest,
            )
        except LocalSourceMaterializationError:
            self._cleanup_failed(staging_root)
            raise
        except SourcePathAuthorizationError as exc:
            self._cleanup_failed(staging_root)
            raise LocalSourceMaterializationError(
                LocalSourceMaterializationFailure.SOURCE_CHANGED
            ) from exc
        except OSError as exc:
            self._cleanup_failed(staging_root)
            failure = (
                LocalSourceMaterializationFailure.SOURCE_CHANGED
                if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}
                else LocalSourceMaterializationFailure.SOURCE_UNAVAILABLE
            )
            raise LocalSourceMaterializationError(failure) from exc
        except Exception as exc:
            self._cleanup_failed(staging_root)
            raise LocalSourceMaterializationError(
                LocalSourceMaterializationFailure.OUTPUT_WRITE_FAILED
            ) from exc
        finally:
            if staging_fd >= 0:
                os.close(staging_fd)
            if source_fd >= 0:
                os.close(source_fd)

    def discard(self, materialized: LocalMaterializedSource) -> None:
        if not isinstance(materialized, LocalMaterializedSource):
            raise ValueError("materialized source is invalid")
        self._remove_owned_staging(materialized.root)

    def _capture_directory(
        self,
        source_fd: int,
        staging_fd: int,
        *,
        prefix: bytes,
        depth: int,
        policy: SourceCapturePolicy,
        observations: dict[bytes, _NodeObservation],
        entries: list[SourceManifestEntry],
        counters: list[int],
    ) -> None:
        directory_before = _fingerprint(os.fstat(source_fd))
        names = _directory_names(source_fd)
        for name in names:
            path = name if not prefix else prefix + b"/" + name
            if name == b".git":
                continue
            if len(path) > self._max_path_bytes or depth + 1 > self._max_directory_depth:
                raise LocalSourceMaterializationError(
                    LocalSourceMaterializationFailure.SOURCE_LIMIT_EXCEEDED
                )
            counters[0] += 1
            if counters[0] > policy.max_manifest_entries:
                raise LocalSourceMaterializationError(
                    LocalSourceMaterializationFailure.SOURCE_LIMIT_EXCEEDED
                )
            value = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            before = _fingerprint(value)
            if stat.S_ISDIR(value.st_mode):
                observations[path] = _NodeObservation(before)
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=source_fd,
                )
                try:
                    if _fingerprint(os.fstat(child_fd)) != before:
                        self._changed()
                    self._capture_directory(
                        child_fd,
                        staging_fd,
                        prefix=path,
                        depth=depth + 1,
                        policy=policy,
                        observations=observations,
                        entries=entries,
                        counters=counters,
                    )
                    if _fingerprint(os.fstat(child_fd)) != before:
                        self._changed()
                finally:
                    os.close(child_fd)
                continue
            entry, content_digest, included_bytes = self._capture_node(
                source_fd,
                staging_fd,
                name=name,
                path=path,
                value=value,
                policy=policy,
                included_bytes=counters[1],
            )
            observations[path] = _NodeObservation(before, content_digest)
            entries.append(entry)
            counters[1] = included_bytes
            if _fingerprint(os.stat(name, dir_fd=source_fd, follow_symlinks=False)) != before:
                self._changed()
            self._fault("after_entry", path)
        if (
            _directory_names(source_fd) != names
            or _fingerprint(os.fstat(source_fd)) != directory_before
        ):
            self._changed()

    def _capture_node(
        self,
        source_fd: int,
        staging_fd: int,
        *,
        name: bytes,
        path: bytes,
        value: os.stat_result,
        policy: SourceCapturePolicy,
        included_bytes: int,
    ) -> tuple[SourceManifestEntry, str | None, int]:
        manifest_path = SourceManifestPath.from_bytes(path)
        language, classification = _classify(path)
        object_type = _object_type(value.st_mode)
        mode = _normalized_mode(value.st_mode, object_type)
        size = int(value.st_size) if not stat.S_ISFIFO(value.st_mode) else None
        reason = _path_capture_reason(path)
        decision = SourceCaptureDecision.DEFERRED if reason is not None else None
        if reason is None:
            reason = _policy_exclusion(path, classification, policy)
            if reason is not None:
                decision = SourceCaptureDecision.EXCLUDED
        if reason is None and object_type is SourceManifestObjectType.SPECIAL:
            reason = SourceCaptureReason.SPECIAL_FILE
            decision = SourceCaptureDecision.DEFERRED
        if (
            reason is None
            and object_type is SourceManifestObjectType.REGULAR_FILE
            and value.st_nlink > 1
        ):
            reason = SourceCaptureReason.HARDLINK
            decision = SourceCaptureDecision.DEFERRED
        if reason is None and size is not None and size > policy.max_file_bytes:
            reason = SourceCaptureReason.OVERSIZED_FILE
            decision = SourceCaptureDecision.DEFERRED

        content: bytes | None = None
        content_digest: str | None = None
        if reason is None and object_type is SourceManifestObjectType.REGULAR_FILE:
            content = _read_regular_file(source_fd, name, value, policy.max_file_bytes)
            content_digest = hashlib.sha256(content).hexdigest()
            reason = _content_defer_reason(content)
            if reason is not None:
                decision = SourceCaptureDecision.DEFERRED
        elif reason is None and object_type is SourceManifestObjectType.SYMLINK:
            content = os.readlink(name, dir_fd=source_fd)
            if not isinstance(content, bytes):
                content = os.fsencode(content)
            if len(content) > policy.max_file_bytes:
                reason = SourceCaptureReason.OVERSIZED_FILE
                decision = SourceCaptureDecision.DEFERRED
            else:
                content_digest = hashlib.sha256(content).hexdigest()

        if reason is None:
            assert content is not None and content_digest is not None
            if included_bytes + len(content) > policy.max_repository_bytes:
                reason = SourceCaptureReason.REPOSITORY_BUDGET_EXCEEDED
                decision = SourceCaptureDecision.DEFERRED
            else:
                _write_staging_entry(
                    staging_fd,
                    path,
                    content,
                    mode=mode,
                    object_type=object_type,
                )
                decision = SourceCaptureDecision.INCLUDED
                reason = SourceCaptureReason.INCLUDED
                included_bytes += len(content)
        assert decision is not None and reason is not None
        return (
            SourceManifestEntry(
                path=manifest_path,
                object_type=object_type,
                origin=SourceManifestOrigin.LOCAL_DIRECTORY,
                mode=mode,
                size=(len(content) if content is not None else size),
                sha256=(content_digest if decision is SourceCaptureDecision.INCLUDED else None),
                git_blob_id=None,
                language=language,
                classification=classification,
                decision=decision,
                reason=reason,
            ),
            content_digest if decision is SourceCaptureDecision.INCLUDED else None,
            included_bytes,
        )

    def _verify_directory(
        self,
        source_fd: int,
        *,
        prefix: bytes,
        depth: int,
        policy: SourceCapturePolicy,
        expected: dict[bytes, _NodeObservation],
        observed: dict[bytes, _NodeObservation],
        counters: list[int],
    ) -> None:
        for name in _directory_names(source_fd):
            if name == b".git":
                continue
            path = name if not prefix else prefix + b"/" + name
            if len(path) > self._max_path_bytes or depth + 1 > self._max_directory_depth:
                self._changed()
            counters[0] += 1
            if counters[0] > policy.max_manifest_entries:
                self._changed()
            value = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            fingerprint = _fingerprint(value)
            wanted = expected.get(path)
            if wanted is None or wanted.fingerprint != fingerprint:
                self._changed()
            content_digest = None
            if wanted.content_digest is not None:
                if stat.S_ISREG(value.st_mode):
                    content = _read_regular_file(
                        source_fd,
                        name,
                        value,
                        policy.max_file_bytes,
                    )
                elif stat.S_ISLNK(value.st_mode):
                    content = os.readlink(name, dir_fd=source_fd)
                    if not isinstance(content, bytes):
                        content = os.fsencode(content)
                else:
                    self._changed()
                content_digest = hashlib.sha256(content).hexdigest()
                if content_digest != wanted.content_digest:
                    self._changed()
            observed[path] = _NodeObservation(fingerprint, content_digest)
            if stat.S_ISDIR(value.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=source_fd,
                )
                try:
                    self._verify_directory(
                        child_fd,
                        prefix=path,
                        depth=depth + 1,
                        policy=policy,
                        expected=expected,
                        observed=observed,
                        counters=counters,
                    )
                finally:
                    os.close(child_fd)
        if observed.keys() == expected.keys() and not prefix:
            return
        if not prefix:
            self._changed()

    def _assert_output_disjoint(self, source: AuthorizedLocalSource) -> None:
        try:
            source_path = Path(source.canonical_source)
            staging_path = self._staging_parent.resolve(strict=True)
            source_path.relative_to(staging_path)
        except ValueError:
            pass
        else:
            raise LocalSourceMaterializationError(
                LocalSourceMaterializationFailure.OUTPUT_INVALID
            )
        try:
            staging_path.relative_to(source_path)
        except ValueError:
            return
        raise LocalSourceMaterializationError(
            LocalSourceMaterializationFailure.OUTPUT_INVALID
        )

    def _cleanup_failed(self, root: Path) -> None:
        try:
            self._remove_owned_staging(root)
        except Exception as exc:
            raise LocalSourceMaterializationError(
                LocalSourceMaterializationFailure.CLEANUP_FAILED
            ) from exc

    def _remove_owned_staging(self, root: Path) -> None:
        absolute = Path(os.path.abspath(os.fspath(root)))
        if absolute.parent != self._staging_parent or not absolute.name.startswith(
            _STAGING_PREFIX
        ):
            raise ValueError("materializer staging root is not owned")
        value = absolute.lstat()
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
            raise ValueError("materializer staging root is invalid")
        shutil.rmtree(absolute, ignore_errors=False)

    def _fault(self, stage: str, path: bytes | None) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage, path)

    @staticmethod
    def _changed() -> None:
        raise LocalSourceMaterializationError(
            LocalSourceMaterializationFailure.SOURCE_CHANGED
        )


def _directory_names(directory_fd: int) -> tuple[bytes, ...]:
    names = tuple(os.fsencode(name) for name in os.listdir(directory_fd))
    if any(not name or b"/" in name or b"\0" in name for name in names):
        raise LocalSourceMaterializationError(
            LocalSourceMaterializationFailure.SOURCE_UNAVAILABLE
        )
    return tuple(sorted(names))


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_uid),
        int(value.st_gid),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _read_regular_file(
    parent_fd: int,
    name: bytes,
    expected: os.stat_result,
    maximum_bytes: int,
) -> bytes:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        if _fingerprint(os.fstat(descriptor)) != _fingerprint(expected):
            LocalSourceMaterializer._changed()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_COPY_CHUNK_SIZE, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                LocalSourceMaterializer._changed()
        if _fingerprint(os.fstat(descriptor)) != _fingerprint(expected):
            LocalSourceMaterializer._changed()
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_staging_entry(
    root_fd: int,
    path: bytes,
    content: bytes,
    *,
    mode: int,
    object_type: SourceManifestObjectType,
) -> None:
    components = path.split(b"/")
    parent_fd = os.dup(root_fd)
    descriptor = -1
    try:
        for component in components[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        final = components[-1]
        if object_type is SourceManifestObjectType.SYMLINK:
            os.symlink(content, final, dir_fd=parent_fd)
            return
        descriptor = os.open(
            final,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        written = 0
        while written < len(content):
            written += os.write(descriptor, content[written:])
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o500 if mode == 0o100755 else 0o400)
        os.fsync(descriptor)
    except OSError as exc:
        raise LocalSourceMaterializationError(
            LocalSourceMaterializationFailure.OUTPUT_WRITE_FAILED
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _object_type(mode: int) -> SourceManifestObjectType:
    if stat.S_ISREG(mode):
        return SourceManifestObjectType.REGULAR_FILE
    if stat.S_ISLNK(mode):
        return SourceManifestObjectType.SYMLINK
    return SourceManifestObjectType.SPECIAL


def _normalized_mode(mode: int, object_type: SourceManifestObjectType) -> int:
    if object_type is SourceManifestObjectType.REGULAR_FILE:
        return 0o100755 if mode & 0o111 else 0o100644
    if object_type is SourceManifestObjectType.SYMLINK:
        return 0o120000
    return int(mode & 0o177777)


def _path_capture_reason(path: bytes) -> SourceCaptureReason | None:
    try:
        decoded = path.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return SourceCaptureReason.INVALID_UTF8_PATH
    if (
        unicodedata.normalize("NFC", decoded) != decoded
        or any(unicodedata.category(character).startswith("C") for character in decoded)
        or any(part == ".git" for part in decoded.split("/"))
    ):
        return SourceCaptureReason.NONCANONICAL_PATH
    return None


def _policy_exclusion(
    path: bytes,
    classification: SourceClassification,
    policy: SourceCapturePolicy,
) -> SourceCaptureReason | None:
    if not policy.selects(path):
        return SourceCaptureReason.PATH_EXCLUDED
    if classification is SourceClassification.GENERATED and not policy.include_generated:
        return SourceCaptureReason.GENERATED_EXCLUDED
    if classification is SourceClassification.VENDOR and not policy.include_vendor:
        return SourceCaptureReason.VENDOR_EXCLUDED
    return None


def _content_defer_reason(content: bytes) -> SourceCaptureReason | None:
    if _is_lfs_pointer(content):
        return SourceCaptureReason.LFS_POINTER
    try:
        content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return SourceCaptureReason.INVALID_UTF8_CONTENT
    return None


def _is_lfs_pointer(content: bytes) -> bool:
    if not content.startswith(_LFS_POINTER_PREFIX) or len(content) > 1024:
        return False
    lines = content.rstrip(b"\n").split(b"\n")
    if len(lines) < 3:
        return False
    oid = lines[1].removeprefix(b"oid sha256:")
    size = lines[2].removeprefix(b"size ")
    return (
        lines[1].startswith(b"oid sha256:")
        and len(oid) == 64
        and all(value in b"0123456789abcdef" for value in oid)
        and lines[2].startswith(b"size ")
        and size.isdigit()
    )


_LANGUAGES = {
    ".bash": "shell",
    ".c": "c",
    ".cc": "cpp",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
}


def _classify(path: bytes) -> tuple[str, SourceClassification]:
    try:
        decoded = path.decode("utf-8", errors="strict").lower()
    except UnicodeDecodeError:
        return "unknown", SourceClassification.UNKNOWN
    parts = decoded.split("/")
    suffix = Path(decoded).suffix
    language = _LANGUAGES.get(suffix, "unknown")
    if any(
        part
        in {
            ".venv",
            "bower_components",
            "node_modules",
            "site-packages",
            "third_party",
            "vendor",
        }
        for part in parts
    ):
        return language, SourceClassification.VENDOR
    if any(
        part
        in {
            ".cache",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".tox",
            "__pycache__",
            "build",
            "coverage",
            "dist",
            "generated",
            "target",
        }
        for part in parts
    ):
        return language, SourceClassification.GENERATED
    if suffix in {".md", ".rst", ".txt"}:
        return language, SourceClassification.DOCUMENTATION
    if suffix in {".json", ".yaml", ".yml", ".toml", ".xml", ".csv"}:
        return language, SourceClassification.DATA
    if Path(decoded).name in {"dockerfile", "makefile", "containerfile"} or suffix in {
        ".ini",
        ".cfg",
        ".conf",
    }:
        return language, SourceClassification.CONFIGURATION
    if language != "unknown":
        return language, SourceClassification.SOURCE
    return language, SourceClassification.UNKNOWN


__all__ = [
    "LocalMaterializedSource",
    "LocalSourceMaterializationError",
    "LocalSourceMaterializationFailure",
    "LocalSourceMaterializer",
]
