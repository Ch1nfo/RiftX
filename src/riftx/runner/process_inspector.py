"""Best-effort process identity checks used during supervisor recovery."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from riftx.domain import Execution, ExecutorType

_CREATION_TIME_TOLERANCE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    created_at: datetime | None
    command: str | None
    process_group_id: int | None = None


class ProcessInspector:
    async def inspect(self, pid: int) -> ProcessIdentity | None:
        if not await asyncio.to_thread(_pid_exists, pid):
            return None
        if os.name != "posix":
            return ProcessIdentity(pid=pid, created_at=None, command=None)
        return await asyncio.to_thread(_read_posix_identity, pid)

    async def matches(self, execution: Execution) -> bool:
        if execution.pid is None:
            return False
        identity = await self.inspect(execution.pid)
        if identity is None:
            return False
        if not _creation_time_matches(execution, identity):
            return False
        if not _process_group_matches(execution, identity):
            return False
        return _command_matches(execution, identity)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_posix_identity(pid: int) -> ProcessIdentity | None:
    try:
        completed = subprocess.run(
            [
                "ps",
                "-ww",
                "-o",
                "lstart=",
                "-o",
                "pgid=",
                "-o",
                "command=",
                "-p",
                str(pid),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout.strip()
    if not raw:
        return None
    created_at, process_group_id, command = _split_posix_identity(raw)
    return ProcessIdentity(
        pid=pid,
        created_at=created_at,
        command=command,
        process_group_id=process_group_id,
    )


def _read_posix_command(pid: int) -> str | None:
    """Compatibility helper retained for existing Runner diagnostics."""

    identity = _read_posix_identity(pid)
    return identity.command if identity is not None else None


def _split_posix_identity(
    raw: str,
) -> tuple[datetime | None, int | None, str | None]:
    parts = raw.split(maxsplit=6)
    if len(parts) < 6:
        return None, None, raw or None
    timestamp = " ".join(parts[:5])
    try:
        created_at = datetime.strptime(timestamp, "%a %b %d %H:%M:%S %Y").astimezone(UTC)
    except ValueError:
        created_at = None
    if len(parts) < 7:
        return created_at, None, parts[5] or None
    try:
        process_group_id = int(parts[5])
    except ValueError:
        return created_at, None, " ".join(parts[5:]) or None
    return created_at, process_group_id, parts[6] or None


def _creation_time_matches(execution: Execution, identity: ProcessIdentity) -> bool:
    expected = execution.process_created_at
    actual = identity.created_at
    if expected is None or actual is None:
        return False
    return abs((expected.astimezone(UTC) - actual.astimezone(UTC)).total_seconds()) <= (
        _CREATION_TIME_TOLERANCE_SECONDS
    )


def _process_group_matches(execution: Execution, identity: ProcessIdentity) -> bool:
    expected = execution.process_group_id
    actual = identity.process_group_id
    if expected is None or actual is None:
        # Missing ownership evidence is unknown, not a match.  This also keeps
        # detached Windows cancellation fail-closed until a reliable native
        # creation/group identity probe is available.
        return False
    return expected == actual


def _command_matches(execution: Execution, identity: ProcessIdentity) -> bool:
    command = identity.command
    if command is None:
        return False
    expected = execution.argv
    if not expected:
        if execution.executable_path is None:
            return False
        direct_match = Path(execution.executable_path).name in command
    else:
        direct_match = _argv_matches(expected, command)
    if direct_match:
        return True
    if execution.executor_type is not ExecutorType.SHELL or not execution.command_text:
        return False
    return _shell_exec_replacement_matches(execution.command_text, command)


def _argv_matches(expected: list[str], command: str) -> bool:
    try:
        actual = shlex.split(command, posix=True)
    except ValueError:
        return False
    return (
        len(actual) == len(expected)
        and Path(actual[0]).name == Path(expected[0]).name
        and actual[1:] == expected[1:]
    )


def _shell_exec_replacement_matches(script: str, command: str) -> bool:
    """Recognize a shell optimizing one simple command into ``exec``.

    This is intentionally narrow: compound shell programs and expansions keep
    using the recorded shell identity. Accepting only a simple argv preserves
    command identity while covering ``zsh -lc 'sleep 120'`` becoming
    ``/bin/sleep 120`` under the same PID and process group.
    """

    try:
        lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        expected = list(lexer)
        actual = shlex.split(command, posix=True)
    except ValueError:
        return False
    if expected and expected[0] == "exec":
        expected = expected[1:]
    if (
        not expected
        or len(actual) != len(expected)
        or any(any(character in token for character in ";&|<>()$`") for token in expected)
    ):
        return False
    return Path(actual[0]).name == Path(expected[0]).name and actual[1:] == expected[1:]
