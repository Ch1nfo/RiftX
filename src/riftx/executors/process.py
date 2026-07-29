"""Direct host process executor."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from riftx.domain import ExecutionStatus

from .models import ProcessExecutionRequest, ProcessResult


class ProcessStartError(RuntimeError):
    """Raised when the operating system rejects process creation."""


@dataclass(slots=True)
class ProcessHandle:
    """A running child process whose stdout and stderr are durable files."""

    process: asyncio.subprocess.Process
    request: ProcessExecutionRequest
    started_at: datetime

    @property
    def pid(self) -> int:
        if self.process.pid is None:
            raise RuntimeError("started process does not have a pid")
        return self.process.pid

    @property
    def process_group_id(self) -> int:
        return self.pid

    async def wait(self, *, termination_grace_seconds: float = 2.0) -> ProcessResult:
        try:
            if self.request.timeout_seconds is None:
                exit_code = await self.process.wait()
            else:
                exit_code = await asyncio.wait_for(
                    self.process.wait(), timeout=self.request.timeout_seconds
                )
        except TimeoutError:
            await self._terminate(termination_grace_seconds)
            return ProcessResult(
                status=ExecutionStatus.FAILED,
                exit_code=self.process.returncode,
                timed_out=True,
            )

        return ProcessResult(status=ExecutionStatus.EXITED, exit_code=exit_code)

    async def cancel(self, *, termination_grace_seconds: float = 2.0) -> ProcessResult:
        await self._terminate(termination_grace_seconds)
        return ProcessResult(
            status=ExecutionStatus.CANCELLED,
            exit_code=self.process.returncode,
        )

    async def _terminate(self, grace_seconds: float) -> None:
        if self.process.returncode is not None:
            return

        _signal_process_group(self.process, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=grace_seconds)
            return
        except TimeoutError:
            pass

        _signal_process_group(self.process, signal.SIGKILL)
        await self.process.wait()


class DirectProcessExecutor:
    """Launch argv directly without a shell."""

    async def start(self, request: ProcessExecutionRequest) -> ProcessHandle:
        request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        request.stderr_path.parent.mkdir(parents=True, exist_ok=True)

        stdout_file = _open_log(request.stdout_path)
        stderr_file = _open_log(request.stderr_path)
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv,
                cwd=request.cwd,
                env=request.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=os.name == "posix",
            )
        except (OSError, ValueError) as exc:
            raise ProcessStartError(
                f"failed to start {request.argv[0]!r} for {request.execution_key!r}: {exc}"
            ) from exc
        finally:
            stdout_file.close()
            stderr_file.close()

        return ProcessHandle(process=process, request=request, started_at=datetime.now(UTC))


def _open_log(path: Path) -> BinaryIO:
    return path.open("ab", buffering=0)


def _signal_process_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        return
