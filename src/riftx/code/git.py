"""Side-effect-free, bounded Git navigation for one General Run workspace."""

from __future__ import annotations

import asyncio
import hashlib
import os
import selectors
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.ports import RunRepository
from riftx.domain import RunKind

from .models import (
    GitCommitSummary,
    GitDiffResult,
    GitLogResult,
    GitStatusEntry,
    GitStatusResult,
)
from .workspace import _FilesystemSource, _relative_path

_GIT_PATH = "/usr/local/bin:/usr/bin:/bin"
_MAX_ADMIN_ENTRIES = 200_000
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_STATUS_ENTRIES = 1000
_MAX_STATUS_BYTES = 512 * 1024
_MAX_DIFF_BYTES = 64 * 1024
_MAX_LOG_ENTRIES = 100
_MAX_LOG_BYTES = 256 * 1024
_COMMAND_TIMEOUT_SECONDS = 10.0

_DANGEROUS_CONFIG_PREFIXES = (
    "credential.",
    "filter.",
    "http.",
    "https.",
    "include.",
    "includeif.",
    "remote.",
    "submodule.",
    "url.",
)
_DANGEROUS_CONFIG_KEYS = {
    "core.askpass",
    "core.attributesfile",
    "core.excludesfile",
    "core.fsmonitor",
    "core.gitproxy",
    "core.hookspath",
    "core.sshcommand",
    "core.worktree",
    "diff.external",
    "extensions.worktreeconfig",
    "interactive.difffilter",
    "protocol.allow",
}


@dataclass(frozen=True, slots=True)
class _GitOutput:
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    stderr_truncated: bool = False


class GitWorkspaceService:
    """Expose selected Git reads without entering the durable execution path."""

    def __init__(self, runs: RunRepository) -> None:
        self._runs = runs

    async def status(self, run_id: str, *, max_entries: int = 200) -> GitStatusResult:
        _bounded(max_entries, maximum=_MAX_STATUS_ENTRIES, label="max_entries")
        root = await self._workspace(run_id)

        def operation() -> GitStatusResult:
            with _FilesystemSource(root) as source:
                repository = _SafeGitRepository(source)
                output = repository.run(
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--branch",
                    "--untracked-files=all",
                    "--ignore-submodules=all",
                    maximum_bytes=_MAX_STATUS_BYTES,
                    allow_truncation=True,
                )
                branch, entries = _parse_status(output.stdout, truncated=output.truncated)
                return GitStatusResult(
                    branch=branch,
                    entries=entries[:max_entries],
                    truncated=output.truncated or len(entries) > max_entries,
                )

        return await asyncio.to_thread(operation)

    async def diff(
        self,
        run_id: str,
        *,
        path: str | None = None,
        staged: bool = False,
        context_lines: int = 3,
        max_bytes: int = _MAX_DIFF_BYTES,
    ) -> GitDiffResult:
        normalized = _relative_path(path) if path is not None else None
        if type(context_lines) is not int or not 0 <= context_lines <= 20:
            raise _conflict("code_git_limit_invalid", "context_lines must be between 0 and 20")
        _bounded(max_bytes, maximum=_MAX_DIFF_BYTES, label="max_bytes")
        root = await self._workspace(run_id)

        def operation() -> GitDiffResult:
            arguments = [
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                f"--unified={context_lines}",
            ]
            if staged:
                arguments.append("--cached")
            arguments.append("--")
            if normalized is not None:
                arguments.append(normalized)
            with _FilesystemSource(root) as source:
                repository = _SafeGitRepository(source)
                output = repository.run(
                    "diff",
                    *arguments,
                    maximum_bytes=max_bytes,
                    allow_truncation=True,
                )
                return GitDiffResult(
                    staged=staged,
                    path=normalized,
                    content=output.stdout.decode("utf-8", errors="replace"),
                    bytes_returned=len(output.stdout),
                    truncated=output.truncated,
                )

        return await asyncio.to_thread(operation)

    async def log(
        self,
        run_id: str,
        *,
        path: str | None = None,
        max_entries: int = 20,
    ) -> GitLogResult:
        normalized = _relative_path(path) if path is not None else None
        _bounded(max_entries, maximum=_MAX_LOG_ENTRIES, label="max_entries")
        root = await self._workspace(run_id)

        def operation() -> GitLogResult:
            arguments = [
                "--no-show-signature",
                "--no-decorate",
                f"--max-count={max_entries + 1}",
                "--format=%H%x00%P%x00%aI%x00%an%x00%s%x00",
            ]
            if normalized is not None:
                arguments.extend(("--", normalized))
            with _FilesystemSource(root) as source:
                repository = _SafeGitRepository(source)
                if not repository.has_head():
                    return GitLogResult(path=normalized, commits=[])
                output = repository.run(
                    "log",
                    *arguments,
                    maximum_bytes=_MAX_LOG_BYTES,
                    allow_truncation=True,
                )
                commits = _parse_log(output.stdout)
                return GitLogResult(
                    path=normalized,
                    commits=commits[:max_entries],
                    truncated=output.truncated or len(commits) > max_entries,
                )

        return await asyncio.to_thread(operation)

    async def _workspace(self, run_id: str) -> Path:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        if run.kind is not RunKind.GENERAL or not run.workspace_path:
            raise _conflict(
                "code_git_unavailable",
                "Git tools require a General Run workspace",
            )
        return Path(run.workspace_path)


class _SafeGitRepository:
    def __init__(self, source: _FilesystemSource) -> None:
        self._source = source
        executable = shutil.which("git", path=_GIT_PATH)
        if executable is None or not os.path.isabs(executable):
            raise _conflict("code_git_unavailable", "Git executable is unavailable")
        try:
            executable_stat = os.stat(executable, follow_symlinks=True)
        except OSError as exc:
            raise _conflict("code_git_unavailable", "Git executable is unavailable") from exc
        if not stat.S_ISREG(executable_stat.st_mode):
            raise _conflict("code_git_unavailable", "Git executable is unavailable")
        self._executable = executable
        self._root = source.absolute_path
        self._git_dir = self._root / ".git"
        self._environment = {
            "GIT_ASKPASS": "/bin/false",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_EXTERNAL_DIFF": "/bin/false",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_SSH_COMMAND": "/bin/false",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": _GIT_PATH,
            "SSH_ASKPASS": "/bin/false",
        }
        self._base = (
            self._executable,
            "--no-pager",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "diff.external=",
            "-c",
            "interactive.diffFilter=",
            "-c",
            "log.showSignature=false",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            f"--git-dir={self._git_dir}",
            f"--work-tree={self._root}",
        )
        self._admin_digest = self._validate_admin()
        self._validate_config()

    def run(
        self,
        subcommand: str,
        *arguments: str,
        maximum_bytes: int,
        allow_truncation: bool,
    ) -> _GitOutput:
        if subcommand not in {"status", "diff", "log"}:
            raise RuntimeError(f"Unsupported native Git command: {subcommand!r}")
        if any(
            not argument or "\x00" in argument or "\n" in argument or "\r" in argument
            for argument in arguments
        ):
            raise _conflict("code_git_argument_invalid", "Git argument is invalid")
        output = self._run_raw(
            (*self._base, subcommand, *arguments),
            maximum_bytes=maximum_bytes,
            allow_truncation=allow_truncation,
        )
        self._source.verify_path_binding()
        if self._validate_admin() != self._admin_digest:
            raise _conflict(
                "code_git_changed",
                "Git administrative state changed during read",
            )
        return output

    def has_head(self) -> bool:
        output = self._run_raw(
            (*self._base, "rev-parse", "--verify", "--quiet", "HEAD"),
            maximum_bytes=1024,
            allow_truncation=False,
            accepted_returncodes={0, 1},
        )
        self._source.verify_path_binding()
        if self._validate_admin() != self._admin_digest:
            raise _conflict(
                "code_git_changed",
                "Git administrative state changed during read",
            )
        return bool(output.stdout.strip())

    def _validate_config(self) -> None:
        output = self._run_raw(
            (*self._base, "config", "--local", "--no-includes", "--null", "--list"),
            maximum_bytes=_MAX_CONFIG_BYTES,
            allow_truncation=False,
        ).stdout
        for entry in output.split(b"\x00"):
            if not entry:
                continue
            key, separator, _value = entry.partition(b"\n")
            if not separator:
                key, separator, _value = entry.partition(b"=")
            try:
                normalized = key.decode("utf-8", errors="strict").lower()
            except UnicodeDecodeError as exc:
                raise _conflict("code_git_config_unsafe", "Git config is invalid") from exc
            if (
                normalized in _DANGEROUS_CONFIG_KEYS
                or normalized.startswith(_DANGEROUS_CONFIG_PREFIXES)
                or normalized.startswith("diff.")
                and normalized.endswith((".command", ".textconv"))
            ):
                raise _conflict(
                    "code_git_config_unsafe",
                    "Git repository config contains executable or external behavior",
                )

    def _validate_admin(self) -> str:
        root_fd = self._source.duplicate_root_fd()
        git_fd = -1
        descriptors: list[tuple[str, int]] = []
        digest = hashlib.sha256()
        count = 0
        try:
            try:
                git_fd = os.open(
                    ".git",
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise _conflict(
                    "code_git_unavailable",
                    "Workspace is not a supported Git repository",
                ) from exc
            descriptors.append(("", git_fd))
            git_fd = -1
            while descriptors:
                prefix, descriptor = descriptors.pop()
                try:
                    for name in sorted(os.listdir(descriptor)):
                        relative = f"{prefix}/{name}" if prefix else name
                        try:
                            metadata = os.stat(
                                name,
                                dir_fd=descriptor,
                                follow_symlinks=False,
                            )
                        except OSError as exc:
                            raise _conflict(
                                "code_git_admin_unsafe",
                                "Git administrative state is unreadable",
                            ) from exc
                        count += 1
                        if count > _MAX_ADMIN_ENTRIES:
                            raise _conflict(
                                "code_git_limit_exceeded",
                                "Git administrative state exceeds its entry limit",
                            )
                        mode = metadata.st_mode
                        if stat.S_ISLNK(mode) or not (
                            stat.S_ISDIR(mode) or stat.S_ISREG(mode)
                        ):
                            raise _conflict(
                                "code_git_admin_unsafe",
                                "Git administrative state contains a link or special file",
                            )
                        if relative in {"objects/info/alternates", "info/grafts"}:
                            raise _conflict(
                                "code_git_admin_unsafe",
                                "Git external object or graft sources are not allowed",
                            )
                        digest.update(relative.encode("utf-8", errors="surrogateescape"))
                        digest.update(b"\x00")
                        digest.update(
                            repr(
                                (
                                    metadata.st_dev,
                                    metadata.st_ino,
                                    metadata.st_mode,
                                    metadata.st_size,
                                    metadata.st_mtime_ns,
                                    metadata.st_ctime_ns,
                                )
                            ).encode("ascii")
                        )
                        digest.update(b"\x00")
                        if stat.S_ISDIR(mode):
                            try:
                                child = os.open(
                                    name,
                                    os.O_RDONLY
                                    | os.O_CLOEXEC
                                    | os.O_DIRECTORY
                                    | os.O_NOFOLLOW,
                                    dir_fd=descriptor,
                                )
                            except OSError as exc:
                                raise _conflict(
                                    "code_git_admin_unsafe",
                                    "Git administrative directory changed during validation",
                                ) from exc
                            descriptors.append((relative, child))
                finally:
                    os.close(descriptor)
            return digest.hexdigest()
        finally:
            if git_fd >= 0:
                os.close(git_fd)
            for _, descriptor in descriptors:
                os.close(descriptor)
            os.close(root_fd)

    def _run_raw(
        self,
        argv: tuple[str, ...],
        *,
        maximum_bytes: int,
        allow_truncation: bool,
        accepted_returncodes: set[int] | None = None,
    ) -> _GitOutput:
        try:
            process = subprocess.Popen(
                argv,
                cwd="/",
                env=self._environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise _conflict("code_git_spawn_failed", "Git command could not start") from exc
        output = _communicate_bounded(
            process,
            timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
            maximum_bytes=maximum_bytes,
        )
        if output.stderr_truncated:
            raise _conflict("code_git_limit_exceeded", "Git error output exceeded its byte limit")
        if output.truncated and not allow_truncation:
            raise _conflict("code_git_limit_exceeded", "Git output exceeded its byte limit")
        allowed = accepted_returncodes or {0}
        if not output.truncated and process.returncode not in allowed:
            raise _conflict("code_git_command_failed", "Git command was rejected")
        return output


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    maximum_bytes: int,
) -> _GitOutput:
    if process.stdout is None or process.stderr is None:
        _terminate(process)
        raise _conflict("code_git_pipe_unavailable", "Git output pipe is unavailable")
    selector = selectors.DefaultSelector()
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    total = 0
    truncated = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                raise _conflict("code_git_timeout", "Git command exceeded its timeout")
            events = selector.select(min(remaining, 0.25))
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
            for key, _ in events:
                stream = cast(object, key.fileobj)
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)  # type: ignore[attr-defined]
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                allowed = max(0, maximum_bytes - total)
                streams[stream].extend(chunk[:allowed])  # type: ignore[index]
                total += min(len(chunk), allowed)
                if len(chunk) > allowed:
                    truncated = True
                    stderr_truncated = stream is process.stderr
                    _terminate(process)
                    selector.close()
                    return _GitOutput(
                        stdout=bytes(streams[process.stdout]),
                        stderr=bytes(streams[process.stderr]),
                        truncated=True,
                        stderr_truncated=stderr_truncated,
                    )
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            _terminate(process)
            raise _conflict("code_git_timeout", "Git command exceeded its timeout") from exc
        return _GitOutput(
            stdout=bytes(streams[process.stdout]),
            stderr=bytes(streams[process.stderr]),
            truncated=truncated,
        )
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _parse_status(
    raw: bytes,
    *,
    truncated: bool,
) -> tuple[str | None, list[GitStatusEntry]]:
    records = raw.split(b"\x00")
    if truncated and records:
        records.pop()
    branch: str | None = None
    entries: list[GitStatusEntry] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith(b"## "):
            branch = record[3:].decode("utf-8", errors="replace")
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise _conflict("code_git_output_invalid", "Git status output is invalid")
        status = record[:2].decode("ascii", errors="strict")
        path = os.fsdecode(record[3:])
        original: str | None = None
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            if index >= len(records):
                if truncated:
                    break
                raise _conflict("code_git_output_invalid", "Git rename output is incomplete")
            original = os.fsdecode(records[index])
            index += 1
        entries.append(
            GitStatusEntry(
                path=path,
                index_status=status[0],
                worktree_status=status[1],
                original_path=original,
            )
        )
    return branch, entries


def _parse_log(raw: bytes) -> list[GitCommitSummary]:
    fields = raw.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    commits: list[GitCommitSummary] = []
    for offset in range(0, len(fields) - 4, 5):
        commit, parents, authored_at, author, subject = fields[offset : offset + 5]
        commits.append(
            GitCommitSummary(
                commit=commit.decode("ascii", errors="strict"),
                parents=parents.decode("ascii", errors="strict").split(),
                authored_at=authored_at.decode("ascii", errors="strict"),
                author=author.decode("utf-8", errors="replace"),
                subject=subject.decode("utf-8", errors="replace"),
            )
        )
    return commits


def _bounded(value: int, *, maximum: int, label: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise _conflict(
            "code_git_limit_invalid",
            f"{label} must be between 1 and {maximum}",
        )


def _conflict(code: str, message: str) -> ApplicationConflictError:
    return ApplicationConflictError(code, message)


__all__ = ["GitWorkspaceService"]
