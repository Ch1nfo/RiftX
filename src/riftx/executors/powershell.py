"""PowerShell 7 and Windows PowerShell process execution."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .models import ProcessExecutionRequest, ShellExecutionRequest, ShellKind
from .process import DirectProcessExecutor, ProcessHandle, ProcessStartError


class PowerShellEdition(StrEnum):
    CORE = "core"
    WINDOWS = "windows"


@dataclass(frozen=True, slots=True)
class PowerShellExecutable:
    path: Path
    edition: PowerShellEdition


class PowerShellNotFoundError(ProcessStartError):
    """Raised when neither PowerShell 7 nor Windows PowerShell can be resolved."""


class PowerShellResolver:
    """Resolve PowerShell 7 first, then the Windows PowerShell compatibility host."""

    def __init__(self, *, which=shutil.which, windows: bool | None = None) -> None:
        self._which = which
        self._windows = os.name == "nt" if windows is None else windows

    def resolve(self, explicit_path: Path | None = None) -> PowerShellExecutable:
        if explicit_path is not None:
            resolved = self._resolve_path(explicit_path)
            if resolved is None:
                raise PowerShellNotFoundError(
                    f"PowerShell executable was not found: {explicit_path}"
                )
            return PowerShellExecutable(
                path=resolved,
                edition=_edition_for_path(resolved),
            )

        candidates = ("pwsh.exe", "pwsh", "powershell.exe") if self._windows else ("pwsh",)
        for candidate in candidates:
            resolved = self._which(candidate)
            if resolved:
                path = Path(resolved)
                return PowerShellExecutable(path=path, edition=_edition_for_path(path))
        raise PowerShellNotFoundError(
            "PowerShell was not found; install PowerShell 7 (pwsh) or provide shell_path"
        )

    def _resolve_path(self, path: Path) -> Path | None:
        if path.is_absolute():
            return path if path.is_file() else None
        resolved = self._which(str(path))
        return Path(resolved) if resolved else None


class PowerShellExecutor:
    """Launch a PowerShell script through argv without implicit shell parsing."""

    def __init__(
        self,
        process_executor: DirectProcessExecutor | None = None,
        *,
        resolver: PowerShellResolver | None = None,
    ) -> None:
        self._process_executor = process_executor or DirectProcessExecutor()
        self._resolver = resolver or PowerShellResolver()

    async def start(self, request: ShellExecutionRequest) -> ProcessHandle:
        if request.shell is not ShellKind.POWERSHELL:
            raise ValueError("PowerShellExecutor requires shell='powershell'")
        executable = self._resolver.resolve(request.shell_path)
        return await self._process_executor.start(
            ProcessExecutionRequest(
                execution_key=request.execution_key,
                argv=build_powershell_argv(executable.path, request.script),
                cwd=request.cwd,
                env=request.env,
                timeout_seconds=request.timeout_seconds,
                stdout_path=request.stdout_path,
                stderr_path=request.stderr_path,
            )
        )


def build_powershell_argv(path: Path, script: str) -> list[str]:
    """Build the design-specified explicit PowerShell invocation."""

    if not script:
        raise ValueError("PowerShell script must not be empty")
    return [str(path), "-NoLogo", "-NoProfile", "-Command", script]


def _edition_for_path(path: Path) -> PowerShellEdition:
    name = path.name.casefold()
    return PowerShellEdition.WINDOWS if name == "powershell.exe" else PowerShellEdition.CORE
