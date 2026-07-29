"""Best-effort process identity checks used during supervisor recovery."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from riftx.domain import Execution


class ProcessInspector:
    async def matches(self, execution: Execution) -> bool:
        if execution.pid is None or not await asyncio.to_thread(_pid_exists, execution.pid):
            return False
        if os.name != "posix" or not execution.argv:
            return True

        command = await asyncio.to_thread(_read_posix_command, execution.pid)
        if command is None:
            return False
        executable_name = Path(execution.argv[0]).name
        return executable_name in command


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_posix_command(pid: int) -> str | None:
    completed = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    if completed.returncode != 0:
        return None
    command = completed.stdout.strip()
    return command or None
