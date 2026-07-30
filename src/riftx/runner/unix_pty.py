"""Unix pseudo-terminal backend."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import shutil
import signal
import struct
import sys
import termios
from pathlib import Path

from .models import TerminalLaunchRequest


class UnixPTYHandle:
    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process,
        master_fd: int,
        transcript_path: Path,
    ) -> None:
        self.process = process
        self.master_fd = master_fd
        self._write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(
            _pump_output(master_fd, transcript_path),
            name=f"riftx-unix-pty-reader-{process.pid}",
        )

    @property
    def pid(self) -> int:
        if self.process.pid is None:
            raise RuntimeError("PTY child does not have a pid")
        return self.process.pid

    async def write(self, data: bytes) -> None:
        if not data:
            return
        async with self._write_lock:
            await asyncio.to_thread(os.write, self.master_fd, data)

    async def resize(self, cols: int, rows: int) -> None:
        await asyncio.to_thread(_set_window_size, self.master_fd, cols, rows)
        if self.process.returncode is None:
            _signal_process_group(self.pid, signal.SIGWINCH)

    async def interrupt(self) -> None:
        if self.process.returncode is None:
            _signal_process_group(self.pid, signal.SIGINT)

    async def terminate(self, grace_seconds: float) -> None:
        await _terminate_process(self.process, grace_seconds)

    async def wait(self) -> int:
        return await self.process.wait()

    async def close_output(self) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self._reader_task), timeout=1.0)
        except TimeoutError:
            _safe_close(self.master_fd)
            await asyncio.gather(self._reader_task, return_exceptions=True)
        else:
            _safe_close(self.master_fd)


class UnixPTYBackend:
    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        transcript_path: Path,
        environment: dict[str, str],
    ) -> UnixPTYHandle:
        _validate_target(request.argv[0], request.cwd, environment)
        master_fd, slave_fd = pty.openpty()
        try:
            _set_window_size(master_fd, request.cols, request.rows)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("_pty_child.py")),
                *request.argv,
                cwd=request.cwd,
                env=environment,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
            )
        except Exception:
            _safe_close(master_fd)
            raise
        finally:
            _safe_close(slave_fd)
        return UnixPTYHandle(
            process=process,
            master_fd=master_fd,
            transcript_path=transcript_path,
        )


async def _pump_output(master_fd: int, transcript_path: Path) -> None:
    with transcript_path.open("ab", buffering=0) as transcript:
        while True:
            try:
                data = await asyncio.to_thread(os.read, master_fd, 64 * 1024)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    return
                raise
            if not data:
                return
            transcript.write(data)


def _validate_target(executable: str, cwd: Path, environment: dict[str, str]) -> None:
    if os.path.dirname(executable):
        path = Path(executable)
        if not path.is_absolute():
            path = cwd / path
        if not path.exists():
            raise FileNotFoundError(executable)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise PermissionError(executable)
        return
    if shutil.which(executable, path=environment.get("PATH")) is None:
        raise FileNotFoundError(executable)


def _set_window_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _terminate_process(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        return
    _signal_process_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    _signal_process_group(process.pid, signal.SIGKILL)
    await process.wait()


def _signal_process_group(process_group_id: int, sig: signal.Signals) -> None:
    try:
        os.killpg(process_group_id, sig)
    except ProcessLookupError:
        return


def _safe_close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError as exc:
        if exc.errno != errno.EBADF:
            raise
