"""Explicit shell executor built on the direct process executor."""

from __future__ import annotations

import os
from pathlib import Path

from .models import ProcessExecutionRequest, ShellExecutionRequest, ShellKind
from .process import DirectProcessExecutor, ProcessHandle

_DEFAULT_SHELL_PATHS: dict[ShellKind, Path] = {
    ShellKind.BASH: Path("/bin/bash"),
    ShellKind.ZSH: Path("/bin/zsh"),
    ShellKind.POWERSHELL: Path("pwsh.exe"),
    ShellKind.CMD: Path("cmd.exe"),
}


class ShellExecutor:
    def __init__(self, process_executor: DirectProcessExecutor | None = None) -> None:
        self._process_executor = process_executor or DirectProcessExecutor()

    async def start(self, request: ShellExecutionRequest) -> ProcessHandle:
        shell_path = request.shell_path or _DEFAULT_SHELL_PATHS[request.shell]
        argv = build_shell_argv(request.shell, shell_path, request.script)
        return await self._process_executor.start(
            ProcessExecutionRequest(
                execution_key=request.execution_key,
                argv=argv,
                cwd=request.cwd,
                env=request.env,
                timeout_seconds=request.timeout_seconds,
                stdout_path=request.stdout_path,
                stderr_path=request.stderr_path,
            )
        )


def build_shell_argv(shell: ShellKind, shell_path: Path, script: str) -> list[str]:
    if shell in {ShellKind.BASH, ShellKind.ZSH}:
        if os.name == "nt" and not shell_path.is_absolute():
            raise ValueError("Unix shells require an explicit path on Windows")
        return [str(shell_path), "-lc", script]
    if shell is ShellKind.POWERSHELL:
        return [str(shell_path), "-NoLogo", "-NoProfile", "-Command", script]
    if shell is ShellKind.CMD:
        return [str(shell_path), "/d", "/s", "/c", script]
    raise ValueError(f"unsupported shell: {shell}")
