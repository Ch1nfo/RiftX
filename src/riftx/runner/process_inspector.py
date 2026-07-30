"""Best-effort process identity checks used during supervisor recovery."""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from riftx.domain import Execution

_CREATION_TIME_TOLERANCE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    created_at: datetime | None
    command: str | None


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
            ["ps", "-ww", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
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
    created_at, command = _split_posix_identity(raw)
    return ProcessIdentity(pid=pid, created_at=created_at, command=command)


def _read_posix_command(pid: int) -> str | None:
    """Compatibility helper retained for existing Runner diagnostics."""

    identity = _read_posix_identity(pid)
    return identity.command if identity is not None else None


def _split_posix_identity(raw: str) -> tuple[datetime | None, str | None]:
    parts = raw.split(maxsplit=5)
    if len(parts) < 6:
        return None, raw or None
    timestamp = " ".join(parts[:5])
    try:
        created_at = datetime.strptime(timestamp, "%a %b %d %H:%M:%S %Y").astimezone(UTC)
    except ValueError:
        created_at = None
    return created_at, parts[5] or None


def _creation_time_matches(execution: Execution, identity: ProcessIdentity) -> bool:
    expected = execution.process_created_at
    actual = identity.created_at
    if expected is None or actual is None:
        return True
    return abs((expected.astimezone(UTC) - actual.astimezone(UTC)).total_seconds()) <= (
        _CREATION_TIME_TOLERANCE_SECONDS
    )


def _command_matches(execution: Execution, identity: ProcessIdentity) -> bool:
    command = identity.command
    if command is None:
        return True
    expected = execution.argv
    if not expected:
        return execution.executable_path is None or Path(execution.executable_path).name in command
    actual_executable = command.split(maxsplit=1)[0]
    if Path(actual_executable).name != Path(expected[0]).name:
        return False
    cursor = len(actual_executable)
    for argument in expected[1:]:
        position = command.find(argument, cursor)
        if position < 0:
            return False
        cursor = position + len(argument)
    return True
