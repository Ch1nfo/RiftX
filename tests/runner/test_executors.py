from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import riftx.executors.process as process_module
from riftx.domain import ExecutionStatus
from riftx.executors import (
    DirectProcessExecutor,
    ProcessExecutionRequest,
    ProcessStartError,
    ShellExecutionRequest,
    ShellExecutor,
    ShellKind,
    merge_environment,
)
from riftx.executors.process import (
    ProcessGroupTerminationError,
    ProcessHandle,
    ProcessTreeTerminationError,
    UnverifiedProcessTreeTerminationError,
    _terminate_posix_process_group,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_process.py"


def request_for(
    tmp_path: Path,
    *args: str,
    timeout_seconds: float | None = None,
    env: dict[str, str] | None = None,
) -> ProcessExecutionRequest:
    return ProcessExecutionRequest(
        execution_key="test-execution",
        argv=[sys.executable, str(FIXTURE), *args],
        cwd=tmp_path,
        env=env or merge_environment(),
        timeout_seconds=timeout_seconds,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )


def wait_for_file(path: Path, *, deadline_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {path}")


def wait_for_process_exit(pid: int, *, deadline_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for process {pid}")


def kill_process_group(process_group_id: int) -> None:
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return


def windows_test_handle(tmp_path: Path, process: Any) -> ProcessHandle:
    return ProcessHandle(
        process=process,
        request=request_for(tmp_path, "success"),
        started_at=datetime.now(UTC),
    )


async def test_direct_process_captures_unicode_stdout_stderr_and_environment(
    tmp_path: Path,
) -> None:
    executor = DirectProcessExecutor()
    environment = merge_environment({"RIFTX_TEST_VALUE": "layered"})
    handle = await executor.start(request_for(tmp_path, "success", env=environment))

    result = await handle.wait()

    assert result.status is ExecutionStatus.EXITED
    assert result.exit_code == 0
    assert (tmp_path / "stdout.log").read_text() == ("stdout: 你好 RiftX\nenv: layered\n")
    assert (tmp_path / "stderr.log").read_text() == "stderr: diagnostic\n"


async def test_direct_process_preserves_nonzero_exit_code(tmp_path: Path) -> None:
    handle = await DirectProcessExecutor().start(request_for(tmp_path, "failure"))
    result = await handle.wait()

    assert result.status is ExecutionStatus.EXITED
    assert result.exit_code == 23
    assert "intentional failure" in (tmp_path / "stderr.log").read_text()


async def test_direct_process_enforces_timeout(tmp_path: Path) -> None:
    handle = await DirectProcessExecutor(autodetect_containment=False).start(
        request_for(tmp_path, "sleep", "--seconds", "30", timeout_seconds=0.05)
    )
    if os.name == "posix":
        with pytest.raises(UnverifiedProcessTreeTerminationError, match="cannot be proven"):
            await handle.wait(termination_grace_seconds=0.1)
    else:
        result = await handle.wait(termination_grace_seconds=0.1)
        assert result.status is ExecutionStatus.FAILED
        assert result.timed_out is True
    assert handle.process.returncode is not None


async def test_shell_executor_supports_pipeline(tmp_path: Path) -> None:
    request = ShellExecutionRequest(
        execution_key="shell-pipeline",
        script="printf 'alpha\\nbeta\\n' | grep beta",
        shell=ShellKind.BASH,
        cwd=tmp_path,
        env=merge_environment(),
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    handle = await ShellExecutor().start(request)
    result = await handle.wait()

    assert result.exit_code == 0
    assert (tmp_path / "stdout.log").read_text() == "beta\n"


async def test_start_error_keeps_durable_context(tmp_path: Path) -> None:
    request = ProcessExecutionRequest(
        execution_key="missing-binary",
        argv=[str(tmp_path / "does-not-exist")],
        cwd=tmp_path,
        env=merge_environment(),
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    with pytest.raises(ProcessStartError, match="missing-binary"):
        await DirectProcessExecutor().start(request)


@pytest.mark.skipif(os.name != "posix", reason="process group semantics are POSIX-specific")
async def test_cancel_terminates_child_process_group(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    handle = await DirectProcessExecutor(autodetect_containment=False).start(
        request_for(
            tmp_path,
            "stubborn-child",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
        )
    )
    try:
        await asyncio.to_thread(wait_for_file, heartbeat)
        await asyncio.to_thread(wait_for_file, tmp_path / "stdout.log")
        child_pid = int((tmp_path / "stdout.log").read_text().strip().splitlines()[0])

        with pytest.raises(UnverifiedProcessTreeTerminationError, match="cannot be proven"):
            await handle.cancel(termination_grace_seconds=0.2)
        size_after_cancel = heartbeat.stat().st_size
        await asyncio.to_thread(wait_for_process_exit, handle.pid)
        await asyncio.to_thread(wait_for_process_exit, child_pid)
        await asyncio.sleep(0.2)

        assert handle.process.returncode is not None
        assert heartbeat.stat().st_size == size_after_cancel
    finally:
        kill_process_group(handle.process_group_id)


@pytest.mark.skipif(os.name != "posix", reason="process group semantics are POSIX-specific")
async def test_cancel_propagates_signal_failure_without_waiting_for_live_leader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = await DirectProcessExecutor().start(request_for(tmp_path, "sleep", "--seconds", "30"))

    async def fail_group_termination(*_: object, **__: object) -> None:
        raise PermissionError("killpg denied")

    monkeypatch.setattr(
        "riftx.executors.process._terminate_posix_process_group",
        fail_group_termination,
    )
    try:
        with pytest.raises(PermissionError, match="killpg denied"):
            await asyncio.wait_for(
                handle.cancel(termination_grace_seconds=0.1),
                timeout=0.5,
            )
        assert handle.process.returncode is None
    finally:
        kill_process_group(handle.process_group_id)
        await handle.process.wait()


@pytest.mark.skipif(os.name != "posix", reason="process group semantics are POSIX-specific")
async def test_group_termination_refuses_the_runner_process_group() -> None:
    with pytest.raises(ProcessGroupTerminationError, match="unsafe process group"):
        await _terminate_posix_process_group(
            os.getpgrp(),
            grace_seconds=0.01,
        )


async def test_windows_cancel_runs_tree_kill_even_when_leader_already_exited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(
        pid=41001,
        returncode=17,
        wait=AsyncMock(return_value=17),
    )
    handle = windows_test_handle(tmp_path, process)
    kill_tree = AsyncMock()
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(process_module, "_kill_windows_process_tree", kill_tree)

    with pytest.raises(
        UnverifiedProcessTreeTerminationError,
        match="cannot be proven without a kernel Job Object",
    ):
        await handle.cancel(termination_grace_seconds=0.2)

    kill_tree.assert_awaited_once_with(41001, timeout_seconds=0.5)
    process.wait.assert_awaited_once_with()


async def test_windows_cancel_propagates_taskkill_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(
        pid=41002,
        returncode=None,
        wait=AsyncMock(),
    )
    handle = windows_test_handle(tmp_path, process)
    taskkill = SimpleNamespace(
        wait=AsyncMock(return_value=5),
        kill=Mock(),
    )
    create_subprocess = AsyncMock(return_value=taskkill)
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_module.asyncio,
        "create_subprocess_exec",
        create_subprocess,
    )

    with pytest.raises(ProcessTreeTerminationError, match="exit status 5"):
        await handle.cancel(termination_grace_seconds=0.1)

    process.wait.assert_not_awaited()
    taskkill.kill.assert_not_called()
    assert create_subprocess.await_args.args == (
        "taskkill.exe",
        "/PID",
        "41002",
        "/T",
        "/F",
    )


async def test_windows_cancel_fails_when_leader_exit_confirmation_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def wait_forever() -> int:
        await asyncio.Event().wait()
        return 0

    process = SimpleNamespace(
        pid=41003,
        returncode=None,
        wait=AsyncMock(side_effect=wait_forever),
    )
    handle = windows_test_handle(tmp_path, process)
    kill_tree = AsyncMock()
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(process_module, "_kill_windows_process_tree", kill_tree)
    monkeypatch.setattr(process_module, "_MIN_WINDOWS_TREE_CONFIRMATION_SECONDS", 0.01)

    with pytest.raises(ProcessTreeTerminationError, match="could not be confirmed"):
        await asyncio.wait_for(
            handle.cancel(termination_grace_seconds=0.001),
            timeout=0.2,
        )

    kill_tree.assert_awaited_once_with(41003, timeout_seconds=0.01)


async def test_windows_cancel_still_fails_closed_after_leader_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=41004, returncode=None)

    async def confirm_exit() -> int:
        process.returncode = -9
        return -9

    process.wait = AsyncMock(side_effect=confirm_exit)
    handle = windows_test_handle(tmp_path, process)
    kill_tree = AsyncMock()
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(process_module, "_kill_windows_process_tree", kill_tree)

    with pytest.raises(
        UnverifiedProcessTreeTerminationError,
        match="cannot be proven without a kernel Job Object",
    ):
        await handle.cancel(termination_grace_seconds=0.1)

    kill_tree.assert_awaited_once_with(41004, timeout_seconds=0.5)
    process.wait.assert_awaited_once_with()
