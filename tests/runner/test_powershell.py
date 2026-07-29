from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from riftx.executors import (
    PowerShellEdition,
    PowerShellExecutor,
    PowerShellNotFoundError,
    PowerShellResolver,
    ProcessHandle,
    ShellExecutionRequest,
    ShellExecutor,
    ShellKind,
    build_powershell_argv,
)
from riftx.executors.process import _process_group_options


class CapturingProcessExecutor:
    def __init__(self) -> None:
        self.requests = []

    async def start(self, request):
        self.requests.append(request)
        return cast(ProcessHandle, object())


class StaticResolver:
    def __init__(self, path: Path) -> None:
        self.path = path

    def resolve(self, explicit_path: Path | None = None):
        from riftx.executors import PowerShellExecutable

        return PowerShellExecutable(
            path=explicit_path or self.path,
            edition=PowerShellEdition.CORE,
        )


def test_powershell_resolver_prefers_pwsh_and_falls_back_to_windows_powershell() -> None:
    available = {
        "pwsh.exe": "C:/Program Files/PowerShell/7/pwsh.exe",
        "powershell.exe": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    }
    resolver = PowerShellResolver(which=available.get, windows=True)
    executable = resolver.resolve()
    assert executable.path.name == "pwsh.exe"
    assert executable.edition is PowerShellEdition.CORE

    fallback = PowerShellResolver(
        which={"powershell.exe": available["powershell.exe"]}.get,
        windows=True,
    ).resolve()
    assert fallback.path.name == "powershell.exe"
    assert fallback.edition is PowerShellEdition.WINDOWS


def test_powershell_resolver_rejects_missing_explicit_binary() -> None:
    resolver = PowerShellResolver(which=lambda _: None)
    with pytest.raises(PowerShellNotFoundError, match="was not found"):
        resolver.resolve(Path("missing-pwsh.exe"))


def test_powershell_argv_matches_explicit_design_contract() -> None:
    assert build_powershell_argv(Path("pwsh.exe"), "Write-Output ok") == [
        "pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-Command",
        "Write-Output ok",
    ]


@pytest.mark.asyncio
async def test_shell_executor_delegates_powershell_without_implicit_shell(
    tmp_path: Path,
) -> None:
    process = CapturingProcessExecutor()
    powershell = PowerShellExecutor(
        process,  # type: ignore[arg-type]
        resolver=StaticResolver(Path("C:/PowerShell/pwsh.exe")),  # type: ignore[arg-type]
    )
    executor = ShellExecutor(
        process,  # type: ignore[arg-type]
        powershell_executor=powershell,
    )
    request = ShellExecutionRequest(
        execution_key="powershell-1",
        script="Write-Output 'hello'",
        shell=ShellKind.POWERSHELL,
        cwd=tmp_path,
        env={"PATH": "test"},
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )
    await executor.start(request)

    launched = process.requests[0]
    assert launched.argv == [
        "C:/PowerShell/pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-Command",
        "Write-Output 'hello'",
    ]
    assert launched.env == {"PATH": "test"}


def test_windows_processes_use_a_new_process_group(monkeypatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    assert _process_group_options() == {"creationflags": 512}


@pytest.mark.asyncio
async def test_installed_powershell_can_execute_and_capture_utf8(tmp_path: Path) -> None:
    path = shutil.which("pwsh") or shutil.which("pwsh.exe")
    if path is None:
        pytest.skip("PowerShell is not installed on this test host")

    from riftx.executors import DirectProcessExecutor

    executor = PowerShellExecutor(DirectProcessExecutor())
    handle = await executor.start(
        ShellExecutionRequest(
            execution_key="powershell-integration",
            script="Write-Output 'RiftX-测试'",
            shell=ShellKind.POWERSHELL,
            cwd=tmp_path,
            env=dict(os.environ),
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
        )
    )
    result = await handle.wait()
    assert result.exit_code == 0
    assert "RiftX-测试" in (tmp_path / "stdout.log").read_text(encoding="utf-8")
