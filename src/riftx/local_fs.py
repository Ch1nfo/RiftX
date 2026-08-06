"""Small POSIX filesystem primitives for owner-only local state."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class OwnerDirectoryError(RuntimeError):
    """Raised when owner-only directory creation or rollback is unsafe."""


@dataclass(frozen=True, slots=True)
class _CreatedDirectory:
    parent_descriptor: int
    child_descriptor: int
    name: str
    path: Path


class OwnerDirectoryBatch:
    """Hold verified directory identities until a repair batch commits."""

    def __init__(self) -> None:
        _require_secure_primitives()
        self._created: list[_CreatedDirectory] = []
        self._finished = False

    def ensure(self, paths: Iterable[Path]) -> tuple[Path, ...]:
        if self._finished:
            raise OwnerDirectoryError("Owner directory batch is already finished")
        created_targets: list[Path] = []
        seen: set[Path] = set()
        for raw_path in paths:
            path = Path(os.path.abspath(os.fspath(raw_path)))
            if path in seen:
                continue
            seen.add(path)
            if _create_owner_directory(path, self._created):
                created_targets.append(path)
        return tuple(created_targets)

    def commit(self) -> None:
        if self._finished:
            return
        _close_created_directories(self._created)
        self._finished = True

    def rollback(self) -> None:
        if self._finished:
            return
        failures = _rollback_created_directories(self._created)
        self._finished = True
        if failures:
            failed = ", ".join(str(path) for path in failures)
            raise OwnerDirectoryError(f"Owner directory rollback was incomplete for: {failed}")

    def __enter__(self) -> OwnerDirectoryBatch:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> Literal[False]:
        if exc is None:
            self.commit()
            return False
        self.rollback()
        return False


def ensure_owner_directories(paths: Iterable[Path]) -> tuple[Path, ...]:
    with OwnerDirectoryBatch() as batch:
        return batch.ensure(paths)


def _require_secure_primitives() -> None:
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "fchmod")
        or os.mkdir not in os.supports_dir_fd
        or os.open not in os.supports_dir_fd
    ):
        raise OwnerDirectoryError(
            "Owner-only directory repair requires secure POSIX filesystem primitives."
        )


def _create_owner_directory(path: Path, created: list[_CreatedDirectory]) -> bool:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(os.sep, flags)
    final_created = False
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            was_created = False
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    was_created = True
                except FileExistsError:
                    pass
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except OSError as error:
                    _raise_directory_open_error(error, descriptor, component, path)
            except OSError as error:
                _raise_directory_open_error(error, descriptor, component, path)

            try:
                metadata = os.fstat(next_descriptor)
                if was_created:
                    os.fchmod(next_descriptor, 0o700)
                    metadata = os.fstat(next_descriptor)
                _validate_directory_metadata(metadata, path=path, created=was_created)
                if was_created:
                    os.fsync(next_descriptor)
                    os.fsync(descriptor)
                    created.append(
                        _CreatedDirectory(
                            parent_descriptor=os.dup(descriptor),
                            child_descriptor=os.dup(next_descriptor),
                            name=component,
                            path=Path(os.sep, *path.parts[1 : index + 2]),
                        )
                    )
                if index == len(components) - 1:
                    final_created = was_created
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
    finally:
        os.close(descriptor)
    return final_created


def _raise_directory_open_error(
    error: OSError,
    parent_descriptor: int,
    component: str,
    path: Path,
) -> None:
    try:
        metadata = os.stat(component, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        metadata = None
    if error.errno == errno.ELOOP or (
        metadata is not None and stat.S_ISLNK(metadata.st_mode)
    ):
        raise OwnerDirectoryError(
            f"Owner directory creation refuses symbolic links in path: {path}"
        ) from error
    if error.errno == errno.ENOTDIR:
        raise OwnerDirectoryError(f"Path component is not a directory: {path}") from error
    raise error


def _validate_directory_metadata(
    metadata: os.stat_result,
    *,
    path: Path,
    created: bool,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise OwnerDirectoryError(f"Path is not a directory: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if created:
        if metadata.st_uid != os.geteuid() or mode != 0o700:
            raise OwnerDirectoryError(
                f"Could not establish owner-only permissions for: {path}"
            )
        return
    trusted_sticky_root = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if metadata.st_uid not in {0, os.geteuid()} or (
        mode & 0o022 and not trusted_sticky_root
    ):
        raise OwnerDirectoryError(f"Path has an unsafe writable ancestor: {path}")


def _rollback_created_directories(
    created: list[_CreatedDirectory],
) -> tuple[Path, ...]:
    failures: list[Path] = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for item in reversed(created):
        opened: int | None = None
        try:
            opened = os.open(item.name, flags, dir_fd=item.parent_descriptor)
            current = os.fstat(opened)
            expected = os.fstat(item.child_descriptor)
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise OwnerDirectoryError("Created directory identity changed before rollback")
            os.close(opened)
            opened = None
            os.rmdir(item.name, dir_fd=item.parent_descriptor)
            os.fsync(item.parent_descriptor)
        except Exception:
            failures.append(item.path)
        finally:
            if opened is not None:
                os.close(opened)
    _close_created_directories(created)
    return tuple(failures)


def _close_created_directories(created: list[_CreatedDirectory]) -> None:
    for item in created:
        os.close(item.parent_descriptor)
        os.close(item.child_descriptor)
    created.clear()


__all__ = [
    "OwnerDirectoryBatch",
    "OwnerDirectoryError",
    "ensure_owner_directories",
]
