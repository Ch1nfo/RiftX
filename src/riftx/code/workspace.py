"""Read-only code navigation over one Run workspace or immutable Audit Snapshot."""

from __future__ import annotations

import asyncio
import base64
import errno
import fnmatch
import os
import stat
from collections import deque
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
)
from riftx.application.ports import (
    AuditAggregateReadRepository,
    AuditAuthorizationBinding,
    RunRepository,
    SnapshotRepository,
)
from riftx.audit import (
    LocalSnapshotView,
    LocalSnapshotViewEntry,
    LocalSnapshotViewError,
    SnapshotBlobObjectType,
    SnapshotCASBinding,
    SnapshotStore,
    open_local_snapshot_view,
    parse_snapshot_content_storage_key,
)
from riftx.domain import RunKind

from .models import (
    CodeEntry,
    CodeGrepMatch,
    CodeGrepResult,
    CodeListResult,
    CodeReadManyResult,
    CodeReadResult,
    CodeSymbol,
    CodeSymbolSearchResult,
)
from .symbols import extract_symbols, language_for_path

_MAX_PATH_BYTES = 4096
_MAX_LIST_ENTRIES = 1000
_MAX_READ_BYTES = 64 * 1024
_ARTIFACT_THRESHOLD_BYTES = _MAX_READ_BYTES
_MAX_READ_MANY_FILES = 20
_MAX_READ_MANY_BYTES = 128 * 1024
_MAX_SCAN_ENTRIES = 20_000
_MAX_GREP_MATCHES = 200
_MAX_GREP_FILE_BYTES = 1024 * 1024
_MAX_GREP_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_GREP_LINE_CHARS = 1000
_MAX_SYMBOL_FILE_BYTES = 512 * 1024
_MAX_SYMBOL_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_SYMBOLS_SCANNED = 20_000
_MAX_SYMBOL_RESULTS = 200


class _Source(Protocol):
    kind: Literal["workspace", "audit_snapshot"]
    digest: str | None
    audit_id: str | None

    def list_entries(
        self,
        path: str,
        *,
        recursive: bool,
        max_entries: int,
    ) -> tuple[list[CodeEntry], bool]: ...

    def read_bytes(self, path: str, *, max_bytes: int) -> tuple[bytes, int, str | None]: ...


class _SourceContext(AbstractContextManager[_Source], Protocol):
    def __enter__(self) -> _Source: ...

    def __exit__(self, *args: object) -> None: ...


class CodeArtifactPublisher(Protocol):
    async def publish(
        self,
        run_id: str,
        *,
        audit_id: str | None,
        path: str,
        content: bytes,
        source_digest: str | None,
    ) -> str: ...


class CodeWorkspaceService:
    """Resolve and operate on the exact source tree owned by one Run."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        audits: AuditAggregateReadRepository,
        snapshots: SnapshotRepository,
        snapshot_store: SnapshotStore | None,
        max_snapshot_file_bytes: int,
        artifacts: CodeArtifactPublisher | None = None,
    ) -> None:
        if max_snapshot_file_bytes < 1:
            raise ValueError("max_snapshot_file_bytes must be positive")
        self._runs = runs
        self._audits = audits
        self._snapshots = snapshots
        self._snapshot_store = snapshot_store
        self._max_snapshot_file_bytes = max_snapshot_file_bytes
        self._artifacts = artifacts

    async def list_files(
        self,
        run_id: str,
        *,
        path: str = "",
        recursive: bool = False,
        max_entries: int = 200,
    ) -> CodeListResult:
        path = _relative_path(path, allow_empty=True)
        _bounded(max_entries, maximum=_MAX_LIST_ENTRIES, label="max_entries")
        source = await self._resolve(run_id)

        def operation() -> CodeListResult:
            with source as opened:
                entries, truncated = opened.list_entries(
                    path,
                    recursive=recursive,
                    max_entries=max_entries,
                )
                return CodeListResult(
                    source=opened.kind,
                    source_digest=opened.digest,
                    path=path,
                    entries=entries,
                    truncated=truncated,
                )

        return await asyncio.to_thread(operation)

    async def read_file(
        self,
        run_id: str,
        *,
        path: str,
        offset: int = 0,
        max_bytes: int = _MAX_READ_BYTES,
    ) -> CodeReadResult:
        path = _relative_path(path)
        if offset < 0:
            raise _conflict("code_read_invalid", "offset must not be negative")
        _bounded(max_bytes, maximum=_MAX_READ_BYTES, label="max_bytes")
        source = await self._resolve(run_id)

        def operation() -> tuple[CodeReadResult, bytes, str | None]:
            with source as opened:
                data, size, digest = opened.read_bytes(
                    path,
                    max_bytes=self._max_snapshot_file_bytes,
                )
                if offset > size:
                    raise _conflict("code_read_invalid", "offset is beyond file size")
                preview = data[offset : offset + max_bytes]
                encoding, content = _model_content(preview)
                return (
                    CodeReadResult(
                        source=opened.kind,
                        source_digest=opened.digest,
                        path=path,
                        size=size,
                        offset=offset,
                        next_offset=offset + len(preview),
                        eof=offset + len(preview) >= size,
                        encoding=encoding,
                        content=content,
                        content_digest=digest,
                    ),
                    data,
                    opened.audit_id,
                )

        result, data, audit_id = await asyncio.to_thread(operation)
        return await self._publish_partial(run_id, result, data, audit_id)

    async def read_many_files(
        self,
        run_id: str,
        *,
        paths: Sequence[str],
        max_bytes_per_file: int = 32 * 1024,
        max_total_bytes: int = _MAX_READ_MANY_BYTES,
    ) -> CodeReadManyResult:
        if not paths or len(paths) > _MAX_READ_MANY_FILES:
            raise _conflict(
                "code_read_invalid",
                f"paths must contain between 1 and {_MAX_READ_MANY_FILES} entries",
            )
        normalized = [_relative_path(path) for path in paths]
        if len(normalized) != len(set(normalized)):
            raise _conflict("code_read_invalid", "paths must not contain duplicates")
        _bounded(max_bytes_per_file, maximum=_MAX_READ_BYTES, label="max_bytes_per_file")
        _bounded(max_total_bytes, maximum=_MAX_READ_MANY_BYTES, label="max_total_bytes")
        results: list[CodeReadResult] = []
        total = 0
        for path in normalized:
            remaining = max_total_bytes - total
            if remaining <= 0:
                break
            result = await self.read_file(
                run_id,
                path=path,
                max_bytes=min(max_bytes_per_file, remaining),
            )
            results.append(result)
            total += result.next_offset
        return CodeReadManyResult(
            files=results,
            total_bytes=total,
            truncated=len(results) < len(normalized),
        )

    async def _publish_partial(
        self,
        run_id: str,
        result: CodeReadResult,
        content: bytes,
        audit_id: str | None,
    ) -> CodeReadResult:
        if (
            self._artifacts is None
            or result.offset != 0
            or result.eof
            or result.size <= _ARTIFACT_THRESHOLD_BYTES
        ):
            return result
        artifact_id = await self._artifacts.publish(
            run_id,
            audit_id=audit_id,
            path=result.path,
            content=content,
            source_digest=result.source_digest,
        )
        return result.model_copy(update={"artifact_id": artifact_id})

    async def glob(
        self,
        run_id: str,
        *,
        pattern: str,
        path: str = "",
        max_results: int = 200,
    ) -> CodeListResult:
        pattern = _glob_pattern(pattern)
        path = _relative_path(path, allow_empty=True)
        _bounded(max_results, maximum=_MAX_LIST_ENTRIES, label="max_results")
        source = await self._resolve(run_id)

        def operation() -> CodeListResult:
            with source as opened:
                entries, scan_truncated = opened.list_entries(
                    path,
                    recursive=True,
                    max_entries=_MAX_SCAN_ENTRIES,
                )
                matched = [
                    entry
                    for entry in entries
                    if entry.type == "file"
                    and fnmatch.fnmatchcase(_relative_to(entry.path, path), pattern)
                ]
                return CodeListResult(
                    source=opened.kind,
                    source_digest=opened.digest,
                    path=path,
                    entries=matched[:max_results],
                    truncated=scan_truncated or len(matched) > max_results,
                )

        return await asyncio.to_thread(operation)

    async def symbol_search(
        self,
        run_id: str,
        *,
        query: str,
        path: str = "",
        file_glob: str | None = None,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> CodeSymbolSearchResult:
        if (
            not query.strip()
            or any(character in query for character in "\x00\r\n")
            or len(query.encode("utf-8")) > 1024
        ):
            raise _conflict("code_symbol_query_invalid", "query is empty or too large")
        path = _relative_path(path, allow_empty=True)
        pattern = _glob_pattern(file_glob) if file_glob is not None else None
        _bounded(max_results, maximum=_MAX_SYMBOL_RESULTS, label="max_results")
        source = await self._resolve(run_id)

        def operation() -> CodeSymbolSearchResult:
            matches: list[CodeSymbol] = []
            files_scanned = bytes_scanned = 0
            skipped_binary = skipped_large = skipped_unsupported = parse_errors = 0
            symbols_scanned = 0
            truncated = False
            needle = query if case_sensitive else query.casefold()
            with source as opened:
                entries, scan_truncated = opened.list_entries(
                    path,
                    recursive=True,
                    max_entries=_MAX_SCAN_ENTRIES,
                )
                truncated = scan_truncated
                for entry in entries:
                    if entry.type != "file":
                        continue
                    relative = _relative_to(entry.path, path)
                    if pattern is not None and not fnmatch.fnmatchcase(relative, pattern):
                        continue
                    if language_for_path(entry.path) is None:
                        skipped_unsupported += 1
                        continue
                    if entry.size > _MAX_SYMBOL_FILE_BYTES:
                        skipped_large += 1
                        continue
                    if bytes_scanned + entry.size > _MAX_SYMBOL_TOTAL_BYTES:
                        truncated = True
                        break
                    data, _, _ = opened.read_bytes(
                        entry.path,
                        max_bytes=_MAX_SYMBOL_FILE_BYTES,
                    )
                    if _looks_binary(data):
                        skipped_binary += 1
                        continue
                    files_scanned += 1
                    bytes_scanned += len(data)
                    remaining_symbols = _MAX_SYMBOLS_SCANNED - symbols_scanned
                    if remaining_symbols <= 0:
                        truncated = True
                        break
                    symbols, file_truncated, parse_failed = extract_symbols(
                        entry.path,
                        data.decode("utf-8", errors="replace"),
                        max_symbols=remaining_symbols,
                    )
                    parse_errors += int(parse_failed)
                    symbols_scanned += len(symbols)
                    truncated |= file_truncated
                    for symbol in symbols:
                        haystack = (
                            f"{symbol.name}\n{symbol.qualified_name}"
                            if case_sensitive
                            else f"{symbol.name}\n{symbol.qualified_name}".casefold()
                        )
                        if needle not in haystack:
                            continue
                        matches.append(symbol)
                        if len(matches) >= max_results:
                            truncated = True
                            break
                    if len(matches) >= max_results or symbols_scanned >= _MAX_SYMBOLS_SCANNED:
                        truncated = True
                        break
                return CodeSymbolSearchResult(
                    source=opened.kind,
                    source_digest=opened.digest,
                    query=query,
                    symbols=matches,
                    files_scanned=files_scanned,
                    bytes_scanned=bytes_scanned,
                    skipped_binary_files=skipped_binary,
                    skipped_large_files=skipped_large,
                    skipped_unsupported_files=skipped_unsupported,
                    parse_errors=parse_errors,
                    truncated=truncated,
                )

        return await asyncio.to_thread(operation)

    async def grep(
        self,
        run_id: str,
        *,
        query: str,
        path: str = "",
        file_glob: str | None = None,
        case_sensitive: bool = True,
        max_matches: int = 100,
    ) -> CodeGrepResult:
        if not query or len(query.encode("utf-8")) > 4096:
            raise _conflict("code_grep_invalid", "query is empty or too large")
        path = _relative_path(path, allow_empty=True)
        pattern = _glob_pattern(file_glob) if file_glob is not None else None
        _bounded(max_matches, maximum=_MAX_GREP_MATCHES, label="max_matches")
        source = await self._resolve(run_id)

        def operation() -> CodeGrepResult:
            matches: list[CodeGrepMatch] = []
            files_scanned = bytes_scanned = skipped_binary = skipped_large = 0
            truncated = False
            needle = query if case_sensitive else query.casefold()
            with source as opened:
                entries, scan_truncated = opened.list_entries(
                    path,
                    recursive=True,
                    max_entries=_MAX_SCAN_ENTRIES,
                )
                truncated = scan_truncated
                for entry in entries:
                    if entry.type != "file":
                        continue
                    relative = _relative_to(entry.path, path)
                    if pattern is not None and not fnmatch.fnmatchcase(relative, pattern):
                        continue
                    if entry.size > _MAX_GREP_FILE_BYTES:
                        skipped_large += 1
                        continue
                    if bytes_scanned + entry.size > _MAX_GREP_TOTAL_BYTES:
                        truncated = True
                        break
                    data, _, _ = opened.read_bytes(
                        entry.path,
                        max_bytes=_MAX_GREP_FILE_BYTES,
                    )
                    if _looks_binary(data):
                        skipped_binary += 1
                        continue
                    text = data.decode("utf-8", errors="replace")
                    files_scanned += 1
                    bytes_scanned += len(data)
                    for line_number, line in enumerate(text.splitlines(), start=1):
                        haystack = line if case_sensitive else line.casefold()
                        if needle not in haystack:
                            continue
                        matches.append(
                            CodeGrepMatch(
                                path=entry.path,
                                line_number=line_number,
                                line=line[:_MAX_GREP_LINE_CHARS],
                            )
                        )
                        if len(matches) >= max_matches:
                            truncated = True
                            break
                    if len(matches) >= max_matches:
                        break
                return CodeGrepResult(
                    source=opened.kind,
                    source_digest=opened.digest,
                    query=query,
                    matches=matches,
                    files_scanned=files_scanned,
                    bytes_scanned=bytes_scanned,
                    skipped_binary_files=skipped_binary,
                    skipped_large_files=skipped_large,
                    truncated=truncated,
                )

        return await asyncio.to_thread(operation)

    async def _resolve(self, run_id: str) -> _SourceContext:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        if run.kind is RunKind.GENERAL:
            if not run.workspace_path:
                raise _conflict(
                    "code_workspace_invalid",
                    "Run workspace is not configured",
                )
            return _FilesystemSource(Path(run.workspace_path))
        if run.kind is not RunKind.CODE_AUDIT:
            raise _conflict("code_source_unavailable", "Run kind has no code source")
        if self._snapshot_store is None:
            raise _conflict(
                "code_source_unavailable",
                "Code Audit Snapshot storage is not configured",
            )

        def authorize(binding: AuditAuthorizationBinding) -> None:
            if (
                binding.scan_run_id != run.id
                or binding.run_id != run.id
                or binding.run_kind != RunKind.CODE_AUDIT.value
            ):
                raise _conflict(
                    "code_source_owner_mismatch",
                    "Audit source does not belong to this Run",
                )

        aggregate = await self._audits.get_by_run_authorized(
            run.id,
            authorize=authorize,
        )
        if aggregate is None or aggregate.audit.value.snapshot_id is None:
            raise _conflict(
                "code_source_unavailable",
                "Code Audit source Snapshot is not sealed",
            )
        snapshot = await self._snapshots.get(
            aggregate.project.value.id,
            aggregate.audit.value.snapshot_id,
        )
        if snapshot is None:
            raise _conflict(
                "code_source_unavailable",
                "Code Audit source Snapshot is unavailable",
            )
        return _SnapshotSource(
            store=self._snapshot_store,
            audit_id=aggregate.audit.value.id,
            project_id=snapshot.project_id,
            snapshot_digest=snapshot.snapshot_digest,
            manifest_digest=snapshot.manifest_digest,
            content_storage_key=snapshot.content_storage_key,
            max_file_bytes=self._max_snapshot_file_bytes,
        )


class _FilesystemSource(AbstractContextManager[_Source]):
    kind: Literal["workspace"] = "workspace"
    digest = None
    audit_id = None

    def __init__(self, root: Path) -> None:
        self._root = root
        self._absolute: Path | None = None
        self._fd = -1

    @property
    def absolute_path(self) -> Path:
        if self._absolute is None or self._fd < 0:
            raise RuntimeError("Workspace source is not open")
        return self._absolute

    def duplicate_root_fd(self) -> int:
        if self._fd < 0:
            raise RuntimeError("Workspace source is not open")
        return os.dup(self._fd)

    def verify_path_binding(self) -> None:
        if self._absolute is None or self._fd < 0:
            raise RuntimeError("Workspace source is not open")
        try:
            path_stat = os.stat(self._absolute, follow_symlinks=False)
            descriptor_stat = os.fstat(self._fd)
        except OSError as exc:
            raise _path_error(exc, "Run workspace changed during operation") from None
        if _fingerprint(path_stat) != _fingerprint(descriptor_stat):
            raise _conflict(
                "code_workspace_changed",
                "Run workspace binding changed during operation",
            )

    def __enter__(self) -> Self:
        if not self._root.is_absolute() or ".." in self._root.parts:
            raise _conflict(
                "code_workspace_invalid",
                "Run workspace must be an absolute normalized path",
            )
        absolute = Path(os.path.normpath(os.fspath(self._root)))
        descriptor = -1
        try:
            descriptor = os.open(
                "/",
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            for part in absolute.parts[1:]:
                next_descriptor = _open_at_directory(descriptor, part)
                os.close(descriptor)
                descriptor = next_descriptor
            self._fd = descriptor
            self._absolute = absolute
            descriptor = -1
        except ApplicationConflictError:
            raise
        except OSError as exc:
            raise _path_error(exc, "Run workspace is unavailable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return self

    def __exit__(self, *_: object) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        self._absolute = None

    def list_entries(
        self,
        path: str,
        *,
        recursive: bool,
        max_entries: int,
    ) -> tuple[list[CodeEntry], bool]:
        root_fd = self._open_directory(path)
        try:
            pending: deque[tuple[str, int]] = deque([(path, root_fd)])
            root_fd = -1
            entries: list[CodeEntry] = []
            while pending:
                current_path, current_fd = pending.popleft()
                try:
                    for name in sorted(os.listdir(current_fd)):
                        relative = name if not current_path else f"{current_path}/{name}"
                        try:
                            metadata = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                        except OSError as exc:
                            raise _path_error(exc, "Workspace entry changed during read") from None
                        entry = _filesystem_entry(relative, metadata)
                        entries.append(entry)
                        if len(entries) > max_entries:
                            return entries[:max_entries], True
                        if recursive and entry.type == "directory":
                            child = _open_at_directory(current_fd, name)
                            pending.append((relative, child))
                finally:
                    os.close(current_fd)
            return entries, False
        finally:
            if root_fd >= 0:
                os.close(root_fd)
            for _, descriptor in pending:
                os.close(descriptor)

    def read_bytes(self, path: str, *, max_bytes: int) -> tuple[bytes, int, str | None]:
        parent_fd, name = self._open_parent(path)
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise _conflict(
                    "code_entry_type_unsupported",
                    "Code tools only read regular files",
                )
            if before.st_size > max_bytes:
                raise _conflict(
                    "code_file_too_large",
                    "File exceeds the bounded source read limit",
                )
            content = bytearray()
            while chunk := os.read(descriptor, min(64 * 1024, max_bytes - len(content) + 1)):
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise _conflict(
                        "code_file_too_large",
                        "File exceeds the bounded source read limit",
                    )
            after = os.fstat(descriptor)
            if _fingerprint(before) != _fingerprint(after) or len(content) != before.st_size:
                raise _conflict(
                    "code_source_changed",
                    "Workspace file changed during read",
                )
            return bytes(content), before.st_size, None
        except ApplicationConflictError:
            raise
        except OSError as exc:
            raise _path_error(exc, "Workspace file is unavailable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)

    def _open_directory(self, path: str) -> int:
        descriptor = os.dup(self._fd)
        try:
            for part in _parts(path):
                next_descriptor = _open_at_directory(descriptor, part)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_parent(self, path: str) -> tuple[int, str]:
        parts = _parts(path)
        descriptor = os.dup(self._fd)
        try:
            for part in parts[:-1]:
                next_descriptor = _open_at_directory(descriptor, part)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor, parts[-1]
        except BaseException:
            os.close(descriptor)
            raise


@dataclass(slots=True)
class _SnapshotSource(AbstractContextManager[_Source]):
    store: SnapshotStore
    audit_id: str
    project_id: str
    snapshot_digest: str
    manifest_digest: str
    content_storage_key: str
    max_file_bytes: int
    kind: Literal["audit_snapshot"] = "audit_snapshot"
    _view: LocalSnapshotView | None = None
    _entries: dict[str, LocalSnapshotViewEntry] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @property
    def digest(self) -> str:
        return self.snapshot_digest

    def __enter__(self) -> Self:
        try:
            self._view = open_local_snapshot_view(
                self.store,
                binding=SnapshotCASBinding(
                    project_id=self.project_id,
                    snapshot_digest=self.snapshot_digest,
                    manifest_digest=self.manifest_digest,
                ),
                content_storage_key=self.content_storage_key,
                expected_descriptor_digest=parse_snapshot_content_storage_key(
                    self.content_storage_key
                ),
                max_file_read_bytes=self.max_file_bytes,
                max_total_read_bytes=max(_MAX_GREP_TOTAL_BYTES, self.max_file_bytes),
                max_text_characters=max(_MAX_GREP_TOTAL_BYTES, self.max_file_bytes),
            )
            self._entries = {
                entry.relative_path: entry for entry in self._view.entries()
            }
        except (LocalSnapshotViewError, ValueError) as exc:
            raise _conflict(
                "code_snapshot_integrity",
                "Code Audit source Snapshot failed verification",
            ) from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._view is not None:
            self._view.close()
            self._view = None
        self._entries.clear()

    def list_entries(
        self,
        path: str,
        *,
        recursive: bool,
        max_entries: int,
    ) -> tuple[list[CodeEntry], bool]:
        self._require_view()
        prefix = f"{path}/" if path else ""
        files = {
            relative_path: entry
            for relative_path, entry in self._entries.items()
            if not path or entry.relative_path == path or entry.relative_path.startswith(prefix)
        }
        if path in files:
            entry = files[path]
            return [_snapshot_entry(entry)], False
        directories: set[str] = set()
        for relative in files:
            parts = relative.split("/")
            for index in range(1, len(parts)):
                directories.add("/".join(parts[:index]))
        if path and path not in directories:
            raise _conflict("code_path_missing", "Code path does not exist")
        results: list[CodeEntry] = []
        for directory in sorted(directories):
            if _is_child(directory, path, recursive=recursive):
                results.append(CodeEntry(path=directory, type="directory", size=0))
        for entry in files.values():
            if _is_child(entry.relative_path, path, recursive=recursive):
                results.append(_snapshot_entry(entry))
        results.sort(key=lambda item: (item.path, item.type))
        return results[:max_entries], len(results) > max_entries

    def read_bytes(self, path: str, *, max_bytes: int) -> tuple[bytes, int, str | None]:
        view = self._require_view()
        entry = self._entries.get(path)
        if entry is None:
            raise _conflict("code_path_missing", "Code path does not exist")
        if entry.object_type is not SnapshotBlobObjectType.REGULAR_FILE:
            raise _conflict(
                "code_entry_type_unsupported",
                "Code tools only read regular files",
            )
        try:
            content = view.read_bytes(path, max_bytes=max_bytes)
        except LocalSnapshotViewError as exc:
            raise _conflict(
                "code_snapshot_read_failed",
                "Code Audit source file failed bounded verification",
            ) from exc
        return content, entry.size, entry.content_digest

    def _require_view(self) -> LocalSnapshotView:
        if self._view is None:
            raise RuntimeError("Snapshot source is not open")
        return self._view


def _relative_path(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise _conflict("code_path_invalid", "Code path is invalid")
    if value == "":
        if allow_empty:
            return ""
        raise _conflict("code_path_invalid", "Code path must not be empty")
    if len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise _conflict("code_path_invalid", "Code path exceeds its byte limit")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise _conflict("code_path_invalid", "Code path must be a normalized relative path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute():
        raise _conflict("code_path_invalid", "Code path must be a normalized relative path")
    normalized = candidate.as_posix()
    return normalized


def _glob_pattern(value: str | None) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise _conflict("code_glob_invalid", "Glob pattern is invalid")
    if len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise _conflict("code_glob_invalid", "Glob pattern exceeds its byte limit")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise _conflict("code_glob_invalid", "Glob pattern must be relative")
    candidate = PurePosixPath(value)
    if candidate.is_absolute():
        raise _conflict("code_glob_invalid", "Glob pattern must be relative")
    return value


def _bounded(value: int, *, maximum: int, label: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise _conflict(
            "code_limit_invalid",
            f"{label} must be between 1 and {maximum}",
        )


def _parts(path: str) -> tuple[str, ...]:
    return tuple(PurePosixPath(path).parts)


def _open_at_directory(parent_fd: int, name: str) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise _path_error(exc, "Workspace directory is unavailable") from None


def _filesystem_entry(path: str, metadata: os.stat_result) -> CodeEntry:
    mode = metadata.st_mode
    entry_type: Literal["file", "directory", "symlink", "special"]
    if stat.S_ISREG(mode):
        entry_type = "file"
    elif stat.S_ISDIR(mode):
        entry_type = "directory"
    elif stat.S_ISLNK(mode):
        entry_type = "symlink"
    else:
        entry_type = "special"
    return CodeEntry(path=path, type=entry_type, size=metadata.st_size)


def _snapshot_entry(entry: LocalSnapshotViewEntry) -> CodeEntry:
    return CodeEntry(
        path=entry.relative_path,
        type=(
            "file"
            if entry.object_type is SnapshotBlobObjectType.REGULAR_FILE
            else "symlink"
        ),
        size=entry.size,
        content_digest=entry.content_digest,
    )


def _is_child(candidate: str, parent: str, *, recursive: bool) -> bool:
    if candidate == parent:
        return False
    relative = _relative_to(candidate, parent)
    return recursive or "/" not in relative


def _relative_to(candidate: str, parent: str) -> str:
    if not parent:
        return candidate
    prefix = f"{parent}/"
    if not candidate.startswith(prefix):
        return candidate
    return candidate.removeprefix(prefix)


def _model_content(data: bytes) -> tuple[Literal["utf-8", "utf-8-lossy", "base64"], str]:
    if _looks_binary(data):
        return "base64", base64.b64encode(data).decode("ascii")
    try:
        return "utf-8", data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "utf-8-lossy", data.decode("utf-8", errors="replace")


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _path_error(exc: OSError, message: str) -> ApplicationConflictError:
    code = (
        "code_path_missing"
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}
        else "code_path_unsafe"
        if exc.errno in {errno.ELOOP, errno.EMLINK}
        else "code_path_unavailable"
    )
    return _conflict(code, message)


def _conflict(code: str, message: str) -> ApplicationConflictError:
    return ApplicationConflictError(code, message)


__all__ = ["CodeArtifactPublisher", "CodeWorkspaceService"]
