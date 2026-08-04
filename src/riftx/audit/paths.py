"""Descriptor-safe POSIX source-root admission for Code Audit Preflight.

This module deliberately stops at path authorization.  It never discovers Git
administrative paths, reads repository content, or starts an external process.
The SourceIngest Capsule owns those later and less-trusted operations.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import posixpath
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Self

DEFAULT_SOURCE_PATH_POLICY_VERSION = "riftx.audit-source-path-policy/v1"
SOURCE_ROOT_IDENTITY_DIGEST_DOMAIN = "riftx.audit-source-root-identity/v1"
REPOSITORY_DESCRIPTOR_CHAIN_DIGEST_DOMAIN = "riftx.audit-repository-descriptor-chain/v1"
REPOSITORY_IDENTITY_DIGEST_DOMAIN = "riftx.audit-repository-descriptor-identity/v1"

DEFAULT_MAX_REPOSITORY_FILTER_PATHS = 512
DEFAULT_MAX_REPOSITORY_FILTER_PATH_BYTES = 4096
DEFAULT_MAX_REPOSITORY_FILTER_TOTAL_BYTES = 64 * 1024

_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
_STAT_SUPPORTS_NOFOLLOW = os.stat in os.supports_follow_symlinks


class SourcePathFailure(StrEnum):
    """Stable, path-free source admission failures."""

    PLATFORM_UNSUPPORTED = "audit_source_platform_unsupported"
    INVALID_ABSOLUTE_PATH = "audit_source_absolute_path_invalid"
    INVALID_RELATIVE_PATH = "audit_repository_relative_path_invalid"
    FILTER_LIMIT_EXCEEDED = "audit_repository_path_limit_exceeded"
    FILTER_CONFLICT = "audit_repository_path_filter_conflict"
    SOURCE_ROOTS_EMPTY = "audit_source_roots_empty"
    SOURCE_OUTSIDE_ROOT = "audit_source_outside_allowed_root"
    SOURCE_SYMLINK = "audit_source_symlink_rejected"
    SOURCE_NOT_DIRECTORY = "audit_source_not_directory"
    SOURCE_UNAVAILABLE = "audit_source_unavailable"
    SOURCE_CHANGED = "audit_source_path_changed"
    DESCRIPTOR_CLOSED = "audit_source_descriptor_closed"


class SourcePathAuthorizationError(RuntimeError):
    """Path-free error raised when source admission cannot be proven safe."""

    def __init__(self, failure: SourcePathFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class RepositoryPathFilters:
    """Canonical, bounded repository-relative include/exclude paths."""

    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceDirectoryIdentity:
    """An fstat-derived directory identity bound to one digest domain."""

    schema_version: str
    platform: str
    canonical_path: str
    root_relative_path: str | None
    filesystem_device: int
    inode: int
    mode: int
    owner_uid: int
    owner_gid: int
    identity_digest: str


@dataclass(frozen=True, slots=True)
class _DirectoryFingerprint:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _DirectoryFingerprint:
        if not stat.S_ISDIR(value.st_mode):
            raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_NOT_DIRECTORY)
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            uid=int(value.st_uid),
            gid=int(value.st_gid),
        )

    def canonical_value(self) -> dict[str, int]:
        return {
            "filesystem_device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "owner_gid": self.gid,
            "owner_uid": self.uid,
        }


@dataclass(frozen=True, slots=True)
class _DescriptorChainEntry:
    component: str
    fingerprint: _DirectoryFingerprint

    def canonical_value(self) -> dict[str, object]:
        return {
            "component": self.component,
            "directory": self.fingerprint.canonical_value(),
        }


class AuthorizedSourceRepository:
    """One authorized source path anchored by held root and repository fds.

    ``root_fd`` and ``repository_fd`` are borrowed descriptors and remain valid
    only until :meth:`close`.  Capsule launchers should call
    :meth:`verify_unchanged` immediately before using the descriptors and use a
    duplicate when ownership must cross a component boundary.
    """

    def __init__(
        self,
        *,
        root_fd: int,
        repository_fd: int,
        canonical_root: str,
        canonical_repository: str,
        repository_relative_path: str,
        policy_version: str,
        root_fingerprint: _DirectoryFingerprint,
        repository_fingerprint: _DirectoryFingerprint,
        root_chain: tuple[_DescriptorChainEntry, ...],
        repository_chain: tuple[_DescriptorChainEntry, ...],
        root_identity: SourceDirectoryIdentity,
        repository_identity: SourceDirectoryIdentity,
        descriptor_chain_digest: str,
    ) -> None:
        self._root_fd = root_fd
        self._repository_fd = repository_fd
        self.canonical_root = canonical_root
        self.canonical_repository = canonical_repository
        self.repository_relative_path = repository_relative_path
        self.policy_version = policy_version
        self.root_identity = root_identity
        self.repository_identity = repository_identity
        self.descriptor_chain_digest = descriptor_chain_digest
        self._root_fingerprint = root_fingerprint
        self._repository_fingerprint = repository_fingerprint
        self._root_chain = root_chain
        self._repository_chain = repository_chain

    @property
    def closed(self) -> bool:
        return self._root_fd < 0 and self._repository_fd < 0

    @property
    def root_fd(self) -> int:
        self._require_open()
        return self._root_fd

    @property
    def repository_fd(self) -> int:
        self._require_open()
        return self._repository_fd

    @property
    def source_root_identity_digest(self) -> str:
        return self.root_identity.identity_digest

    @property
    def repository_identity_digest(self) -> str:
        return self.repository_identity.identity_digest

    @property
    def repository_descriptor_identity_digest(self) -> str:
        """Return the pre-Git descriptor identity, not a content identity."""

        return self.repository_identity.identity_digest

    def duplicate_root_fd(self) -> int:
        """Return a non-inheritable duplicate of the held source-root fd."""

        return self._duplicate_verified(self.root_fd, self._root_fingerprint)

    def duplicate_repository_fd(self) -> int:
        """Return a non-inheritable duplicate of the held repository fd."""

        return self._duplicate_verified(self.repository_fd, self._repository_fingerprint)

    def verify_unchanged(self) -> None:
        """Verify that held inodes and both named descriptor chains still agree."""

        self._require_open()
        _require_descriptor_platform()
        try:
            if _fingerprint_fd(self._root_fd) != self._root_fingerprint:
                raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_CHANGED)
            if _fingerprint_fd(self._repository_fd) != self._repository_fingerprint:
                raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_CHANGED)

            reopened_root_fd = -1
            reopened_repository_fd = -1
            try:
                reopened_root_fd, root_chain = _open_absolute_directory(self.canonical_root)
                if (
                    root_chain != self._root_chain
                    or _fingerprint_fd(reopened_root_fd) != self._root_fingerprint
                ):
                    raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_CHANGED)

                relative_components = _relative_components(self.repository_relative_path)
                reopened_repository_fd, repository_chain = _open_relative_directory(
                    self._root_fd,
                    relative_components,
                )
                if (
                    repository_chain != self._repository_chain
                    or _fingerprint_fd(reopened_repository_fd) != self._repository_fingerprint
                ):
                    raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_CHANGED)
            finally:
                _close_fd(reopened_repository_fd)
                _close_fd(reopened_root_fd)
        except SourcePathAuthorizationError as exc:
            if exc.failure in {
                SourcePathFailure.PLATFORM_UNSUPPORTED,
                SourcePathFailure.DESCRIPTOR_CLOSED,
                SourcePathFailure.SOURCE_CHANGED,
            }:
                raise
            raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_CHANGED) from exc
        except (NotImplementedError, OSError, RuntimeError, ValueError) as exc:
            raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_CHANGED) from exc

    def close(self) -> None:
        """Close both descriptors exactly once."""

        repository_fd = self._repository_fd
        root_fd = self._root_fd
        self._repository_fd = -1
        self._root_fd = -1
        _close_fd(repository_fd)
        _close_fd(root_fd)

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._root_fd < 0 or self._repository_fd < 0:
            raise SourcePathAuthorizationError(SourcePathFailure.DESCRIPTOR_CLOSED)

    @staticmethod
    def _duplicate_verified(
        file_descriptor: int,
        expected: _DirectoryFingerprint,
    ) -> int:
        duplicate = -1
        try:
            duplicate = os.dup(file_descriptor)
            os.set_inheritable(duplicate, False)
            if _fingerprint_fd(duplicate) != expected:
                raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_CHANGED)
            return duplicate
        except SourcePathAuthorizationError:
            _close_fd(duplicate)
            raise
        except (NotImplementedError, OSError) as exc:
            _close_fd(duplicate)
            raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_UNAVAILABLE) from exc


def validate_posix_absolute_path(path: str | os.PathLike[str]) -> str:
    """Return a strictly canonical POSIX absolute wire path.

    Canonical here is deliberately lexical.  Symlink-free filesystem
    canonicality is proven later by the descriptor walk rather than by a
    resolve-then-reopen sequence.
    """

    value = _coerce_path_string(path, SourcePathFailure.INVALID_ABSOLUTE_PATH)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_ABSOLUTE_PATH) from exc
    if (
        not value.startswith("/")
        or "\x00" in value
        or _contains_unsafe_control(value)
        or (value != "/" and value.endswith("/"))
        or "//" in value
        or posixpath.normpath(value) != value
    ):
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_ABSOLUTE_PATH)
    components = () if value == "/" else value.split("/")[1:]
    if any(component in {"", ".", ".."} for component in components):
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_ABSOLUTE_PATH)
    return value


def validate_repository_relative_path(
    path: str | os.PathLike[str],
    *,
    max_path_bytes: int = DEFAULT_MAX_REPOSITORY_FILTER_PATH_BYTES,
) -> str:
    """Return one canonical, bounded repository-relative POSIX path."""

    _validate_positive_limit(max_path_bytes, "max_path_bytes")
    value = _coerce_path_string(path, SourcePathFailure.INVALID_RELATIVE_PATH)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_RELATIVE_PATH) from exc
    candidate = PurePosixPath(value)
    if (
        not value
        or candidate.is_absolute()
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or "\x00" in value
        or _contains_unsafe_control(value)
        or candidate.as_posix() != value
        or len(encoded) > max_path_bytes
    ):
        failure = (
            SourcePathFailure.FILTER_LIMIT_EXCEEDED
            if len(encoded) > max_path_bytes
            else SourcePathFailure.INVALID_RELATIVE_PATH
        )
        raise SourcePathAuthorizationError(failure)
    if any(component in {"", ".", ".."} for component in value.split("/")):
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_RELATIVE_PATH)
    return value


def validate_repository_relative_paths(
    paths: Iterable[str | os.PathLike[str]],
    *,
    max_paths: int = DEFAULT_MAX_REPOSITORY_FILTER_PATHS,
    max_path_bytes: int = DEFAULT_MAX_REPOSITORY_FILTER_PATH_BYTES,
    max_total_bytes: int = DEFAULT_MAX_REPOSITORY_FILTER_TOTAL_BYTES,
) -> tuple[str, ...]:
    """Validate one bounded path sequence without sorting or deduplicating it."""

    _validate_positive_limit(max_paths, "max_paths")
    _validate_positive_limit(max_path_bytes, "max_path_bytes")
    _validate_positive_limit(max_total_bytes, "max_total_bytes")
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_RELATIVE_PATH)

    result: list[str] = []
    seen: set[str] = set()
    total_bytes = 0
    try:
        iterator = iter(paths)
    except TypeError as exc:
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_RELATIVE_PATH) from exc
    for raw_path in iterator:
        if len(result) >= max_paths:
            raise SourcePathAuthorizationError(SourcePathFailure.FILTER_LIMIT_EXCEEDED)
        value = validate_repository_relative_path(raw_path, max_path_bytes=max_path_bytes)
        if value in seen:
            raise SourcePathAuthorizationError(SourcePathFailure.FILTER_CONFLICT)
        seen.add(value)
        total_bytes += len(value.encode("utf-8"))
        if total_bytes > max_total_bytes:
            raise SourcePathAuthorizationError(SourcePathFailure.FILTER_LIMIT_EXCEEDED)
        result.append(value)
    return tuple(result)


def validate_repository_filters(
    *,
    include_paths: Iterable[str | os.PathLike[str]] = (),
    exclude_paths: Iterable[str | os.PathLike[str]] = (),
    max_paths: int = DEFAULT_MAX_REPOSITORY_FILTER_PATHS,
    max_path_bytes: int = DEFAULT_MAX_REPOSITORY_FILTER_PATH_BYTES,
    max_total_bytes: int = DEFAULT_MAX_REPOSITORY_FILTER_TOTAL_BYTES,
) -> RepositoryPathFilters:
    """Validate include/exclude paths under one combined count and byte budget."""

    includes = validate_repository_relative_paths(
        include_paths,
        max_paths=max_paths,
        max_path_bytes=max_path_bytes,
        max_total_bytes=max_total_bytes,
    )
    excludes = validate_repository_relative_paths(
        exclude_paths,
        max_paths=max_paths,
        max_path_bytes=max_path_bytes,
        max_total_bytes=max_total_bytes,
    )
    if len(includes) + len(excludes) > max_paths:
        raise SourcePathAuthorizationError(SourcePathFailure.FILTER_LIMIT_EXCEEDED)
    combined_bytes = sum(len(value.encode("utf-8")) for value in (*includes, *excludes))
    if combined_bytes > max_total_bytes:
        raise SourcePathAuthorizationError(SourcePathFailure.FILTER_LIMIT_EXCEEDED)
    if set(includes).intersection(excludes):
        raise SourcePathAuthorizationError(SourcePathFailure.FILTER_CONFLICT)
    return RepositoryPathFilters(include_paths=includes, exclude_paths=excludes)


def open_authorized_source_repository(
    repository_path: str | os.PathLike[str],
    *,
    allowed_roots: Iterable[str | os.PathLike[str]],
    policy_version: str = DEFAULT_SOURCE_PATH_POLICY_VERSION,
) -> AuthorizedSourceRepository:
    """Authorize and open one local repository directory beneath an allowed root."""

    canonical_repository = validate_posix_absolute_path(repository_path)
    canonical_roots = _canonical_allowed_roots(allowed_roots)
    _validate_policy_version(policy_version)

    repository = PurePosixPath(canonical_repository)
    candidates: list[tuple[str, tuple[str, ...]]] = []
    for root_value in canonical_roots:
        root = PurePosixPath(root_value)
        try:
            relative = repository.relative_to(root)
        except ValueError:
            continue
        candidates.append((root_value, relative.parts))
    if not candidates:
        raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_OUTSIDE_ROOT)
    _require_descriptor_platform()
    canonical_root, relative_components = max(
        candidates,
        key=lambda candidate: len(PurePosixPath(candidate[0]).parts),
    )

    root_fd = -1
    repository_fd = -1
    handle: AuthorizedSourceRepository | None = None
    try:
        root_fd, root_chain = _open_absolute_directory(canonical_root)
        root_fingerprint = _fingerprint_fd(root_fd)
        repository_fd, repository_chain = _open_relative_directory(
            root_fd,
            relative_components,
        )
        repository_fingerprint = _fingerprint_fd(repository_fd)
        relative_path = PurePosixPath(*relative_components).as_posix()
        if not relative_components:
            relative_path = "."

        root_identity = _build_root_identity(
            canonical_root,
            root_fingerprint,
            policy_version=policy_version,
        )
        descriptor_chain_digest = _build_descriptor_chain_digest(
            root_identity_digest=root_identity.identity_digest,
            relative_path=relative_path,
            repository_chain=repository_chain,
            repository_fingerprint=repository_fingerprint,
        )
        repository_identity = _build_repository_identity(
            canonical_repository,
            relative_path=relative_path,
            root_identity_digest=root_identity.identity_digest,
            descriptor_chain_digest=descriptor_chain_digest,
            fingerprint=repository_fingerprint,
        )
        handle = AuthorizedSourceRepository(
            root_fd=root_fd,
            repository_fd=repository_fd,
            canonical_root=canonical_root,
            canonical_repository=canonical_repository,
            repository_relative_path=relative_path,
            policy_version=policy_version,
            root_fingerprint=root_fingerprint,
            repository_fingerprint=repository_fingerprint,
            root_chain=root_chain,
            repository_chain=repository_chain,
            root_identity=root_identity,
            repository_identity=repository_identity,
            descriptor_chain_digest=descriptor_chain_digest,
        )
        root_fd = -1
        repository_fd = -1
        handle.verify_unchanged()
        return handle
    except SourcePathAuthorizationError:
        if handle is not None:
            handle.close()
        raise
    except (NotImplementedError, OSError, RuntimeError, ValueError) as exc:
        if handle is not None:
            handle.close()
        raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_UNAVAILABLE) from exc
    finally:
        _close_fd(repository_fd)
        _close_fd(root_fd)


def _canonical_allowed_roots(
    allowed_roots: Iterable[str | os.PathLike[str]],
) -> tuple[str, ...]:
    if isinstance(allowed_roots, (str, bytes, os.PathLike)):
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_ABSOLUTE_PATH)
    try:
        roots = tuple(validate_posix_absolute_path(root) for root in allowed_roots)
    except TypeError as exc:
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_ABSOLUTE_PATH) from exc
    if not roots:
        raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_ROOTS_EMPTY)
    return tuple(dict.fromkeys(roots))


def _open_absolute_directory(
    canonical_path: str,
) -> tuple[int, tuple[_DescriptorChainEntry, ...]]:
    current_fd = -1
    try:
        current_fd = os.open("/", _directory_open_flags())
        _fingerprint_fd(current_fd)
        chain: list[_DescriptorChainEntry] = []
        components = () if canonical_path == "/" else canonical_path.split("/")[1:]
        for component in components:
            next_fd, entry = _open_directory_component(current_fd, component)
            _close_fd(current_fd)
            current_fd = next_fd
            chain.append(entry)
        return current_fd, tuple(chain)
    except SourcePathAuthorizationError:
        _close_fd(current_fd)
        raise
    except NotImplementedError as exc:
        _close_fd(current_fd)
        raise SourcePathAuthorizationError(SourcePathFailure.PLATFORM_UNSUPPORTED) from exc
    except (OSError, ValueError) as exc:
        _close_fd(current_fd)
        raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_UNAVAILABLE) from exc


def _open_relative_directory(
    root_fd: int,
    components: tuple[str, ...],
) -> tuple[int, tuple[_DescriptorChainEntry, ...]]:
    current_fd = -1
    try:
        current_fd = os.dup(root_fd)
        os.set_inheritable(current_fd, False)
        _fingerprint_fd(current_fd)
        chain: list[_DescriptorChainEntry] = []
        for component in components:
            next_fd, entry = _open_directory_component(current_fd, component)
            _close_fd(current_fd)
            current_fd = next_fd
            chain.append(entry)
        return current_fd, tuple(chain)
    except SourcePathAuthorizationError:
        _close_fd(current_fd)
        raise
    except NotImplementedError as exc:
        _close_fd(current_fd)
        raise SourcePathAuthorizationError(SourcePathFailure.PLATFORM_UNSUPPORTED) from exc
    except (OSError, ValueError) as exc:
        _close_fd(current_fd)
        raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_UNAVAILABLE) from exc


def _open_directory_component(
    parent_fd: int,
    component: str,
) -> tuple[int, _DescriptorChainEntry]:
    if not component or component in {".", ".."} or "/" in component or "\x00" in component:
        raise SourcePathAuthorizationError(SourcePathFailure.INVALID_ABSOLUTE_PATH)
    next_fd = -1
    try:
        next_fd = os.open(component, _directory_open_flags(), dir_fd=parent_fd)
        fingerprint = _fingerprint_fd(next_fd)
        entry = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        entry_fingerprint = _DirectoryFingerprint.from_stat(entry)
        if entry_fingerprint != fingerprint:
            raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_CHANGED)
        return next_fd, _DescriptorChainEntry(component, fingerprint)
    except SourcePathAuthorizationError:
        _close_fd(next_fd)
        raise
    except NotImplementedError as exc:
        _close_fd(next_fd)
        raise SourcePathAuthorizationError(SourcePathFailure.PLATFORM_UNSUPPORTED) from exc
    except OSError as exc:
        _close_fd(next_fd)
        raise SourcePathAuthorizationError(
            _classify_component_open_failure(parent_fd, component, exc)
        ) from exc


def _classify_component_open_failure(
    parent_fd: int,
    component: str,
    error: OSError,
) -> SourcePathFailure:
    if error.errno == errno.ELOOP:
        return SourcePathFailure.SOURCE_SYMLINK
    if error.errno == errno.ENOTDIR:
        try:
            value = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        except (NotImplementedError, OSError):
            return SourcePathFailure.SOURCE_NOT_DIRECTORY
        if stat.S_ISLNK(value.st_mode):
            return SourcePathFailure.SOURCE_SYMLINK
        return SourcePathFailure.SOURCE_NOT_DIRECTORY
    return SourcePathFailure.SOURCE_UNAVAILABLE


def _fingerprint_fd(file_descriptor: int) -> _DirectoryFingerprint:
    try:
        return _DirectoryFingerprint.from_stat(os.fstat(file_descriptor))
    except SourcePathAuthorizationError:
        raise
    except OSError as exc:
        raise SourcePathAuthorizationError(SourcePathFailure.SOURCE_UNAVAILABLE) from exc


def _build_root_identity(
    canonical_root: str,
    fingerprint: _DirectoryFingerprint,
    *,
    policy_version: str,
) -> SourceDirectoryIdentity:
    platform = _platform_identity()
    payload: dict[str, object] = {
        "canonical_root": canonical_root,
        "directory": fingerprint.canonical_value(),
        "platform": platform,
        "policy_version": policy_version,
        "schema_version": SOURCE_ROOT_IDENTITY_DIGEST_DOMAIN,
    }
    return SourceDirectoryIdentity(
        schema_version=SOURCE_ROOT_IDENTITY_DIGEST_DOMAIN,
        platform=platform,
        canonical_path=canonical_root,
        root_relative_path=None,
        filesystem_device=fingerprint.device,
        inode=fingerprint.inode,
        mode=fingerprint.mode,
        owner_uid=fingerprint.uid,
        owner_gid=fingerprint.gid,
        identity_digest=_domain_separated_digest(
            SOURCE_ROOT_IDENTITY_DIGEST_DOMAIN,
            payload,
        ),
    )


def _build_descriptor_chain_digest(
    *,
    root_identity_digest: str,
    relative_path: str,
    repository_chain: tuple[_DescriptorChainEntry, ...],
    repository_fingerprint: _DirectoryFingerprint,
) -> str:
    return _domain_separated_digest(
        REPOSITORY_DESCRIPTOR_CHAIN_DIGEST_DOMAIN,
        {
            "components": [entry.canonical_value() for entry in repository_chain],
            "repository_directory": repository_fingerprint.canonical_value(),
            "repository_relative_path": relative_path,
            "root_identity_digest": root_identity_digest,
            "schema_version": REPOSITORY_DESCRIPTOR_CHAIN_DIGEST_DOMAIN,
        },
    )


def _build_repository_identity(
    canonical_repository: str,
    *,
    relative_path: str,
    root_identity_digest: str,
    descriptor_chain_digest: str,
    fingerprint: _DirectoryFingerprint,
) -> SourceDirectoryIdentity:
    platform = _platform_identity()
    payload: dict[str, object] = {
        "descriptor_chain_digest": descriptor_chain_digest,
        "directory": fingerprint.canonical_value(),
        "platform": platform,
        "repository_relative_path": relative_path,
        "root_identity_digest": root_identity_digest,
        "schema_version": REPOSITORY_IDENTITY_DIGEST_DOMAIN,
    }
    return SourceDirectoryIdentity(
        schema_version=REPOSITORY_IDENTITY_DIGEST_DOMAIN,
        platform=platform,
        canonical_path=canonical_repository,
        root_relative_path=relative_path,
        filesystem_device=fingerprint.device,
        inode=fingerprint.inode,
        mode=fingerprint.mode,
        owner_uid=fingerprint.uid,
        owner_gid=fingerprint.gid,
        identity_digest=_domain_separated_digest(
            REPOSITORY_IDENTITY_DIGEST_DOMAIN,
            payload,
        ),
    )


def _domain_separated_digest(domain: str, value: dict[str, object]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(domain.encode("ascii"))
    digest.update(b"\x00")
    digest.update(canonical)
    return digest.hexdigest()


def _relative_components(relative_path: str) -> tuple[str, ...]:
    if relative_path == ".":
        return ()
    return tuple(relative_path.split("/"))


def _coerce_path_string(
    path: str | os.PathLike[str],
    failure: SourcePathFailure,
) -> str:
    try:
        value = os.fspath(path)
    except TypeError as exc:
        raise SourcePathAuthorizationError(failure) from exc
    if not isinstance(value, str):
        raise SourcePathAuthorizationError(failure)
    return value


def _contains_unsafe_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _validate_positive_limit(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _validate_policy_version(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("policy_version must be bounded printable ASCII")


def _descriptor_platform_supported() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_CLOEXEC")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "dup")
        and hasattr(os, "fstat")
        and hasattr(os, "set_inheritable")
        and _OPEN_SUPPORTS_DIR_FD
        and _STAT_SUPPORTS_DIR_FD
        and _STAT_SUPPORTS_NOFOLLOW
    )


def _require_descriptor_platform() -> None:
    if not _descriptor_platform_supported():
        raise SourcePathAuthorizationError(SourcePathFailure.PLATFORM_UNSUPPORTED)


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _platform_identity() -> str:
    return f"{os.name}:{sys.platform}"


def _close_fd(file_descriptor: int) -> None:
    if file_descriptor >= 0:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
