from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

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
    handle = await DirectProcessExecutor().start(
        request_for(tmp_path, "sleep", "--seconds", "30", timeout_seconds=0.05)
    )
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
    handle = await DirectProcessExecutor().start(
        request_for(
            tmp_path,
            "child",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
        )
    )
    await asyncio.to_thread(wait_for_file, heartbeat)

    result = await handle.cancel(termination_grace_seconds=0.2)
    size_after_cancel = heartbeat.stat().st_size
    await asyncio.sleep(0.2)

    assert result.status is ExecutionStatus.CANCELLED
    assert handle.process.returncode is not None
    assert heartbeat.stat().st_size == size_after_cancel
