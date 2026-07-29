"""Unix PTY lifecycle, durable transcript, and single-writer ownership."""

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
from dataclasses import dataclass
from pathlib import Path

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.ports import (
    ExecutionRepository,
    RunEventRepository,
    TerminalRepository,
)
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    TerminalOwner,
    TerminalSession,
    TerminalStatus,
)
from riftx.domain.base import new_id
from riftx.executors import merge_environment

from .models import OutputSlice, TerminalLaunchRequest
from .paths import RunnerPaths


@dataclass(slots=True)
class _ManagedTerminal:
    process: asyncio.subprocess.Process
    master_fd: int
    transcript_path: Path
    reader_task: asyncio.Task[None]
    monitor_task: asyncio.Task[None] | None = None
    write_lock: asyncio.Lock | None = None
    close_requested: bool = False


class TerminalSupervisor:
    """Own native PTYs while durable state and transcripts live in repositories/files."""

    def __init__(
        self,
        *,
        terminal_repository: TerminalRepository,
        execution_repository: ExecutionRepository,
        event_repository: RunEventRepository,
        paths: RunnerPaths,
        termination_grace_seconds: float = 2.0,
    ) -> None:
        self._terminals = terminal_repository
        self._executions = execution_repository
        self._events = event_repository
        self._paths = paths
        self._termination_grace_seconds = termination_grace_seconds
        self._managed: dict[str, _ManagedTerminal] = {}

    async def start(self, request: TerminalLaunchRequest) -> TerminalSession:
        if os.name != "posix":
            raise RuntimeError("native PTY sessions are currently supported only on Unix")

        session_id = new_id()
        execution_id = new_id()
        self._paths.ensure_run_layout(request.run_id)
        terminal_paths = self._paths.terminal(request.run_id, session_id)
        terminal_paths.directory.mkdir(parents=True, exist_ok=True)
        terminal_paths.transcript.touch(exist_ok=True)

        execution = Execution(
            id=execution_id,
            execution_key=f"terminal:{session_id}",
            run_id=request.run_id,
            node_id=request.node_id,
            executor_type=ExecutorType.PTY,
            argv=request.argv,
            cwd=str(request.cwd),
            env_diff=request.env,
            stdout_path=str(terminal_paths.transcript),
            stderr_path=str(terminal_paths.transcript),
        )
        execution, created = await self._executions.create_if_absent(execution)
        if not created:
            raise ApplicationConflictError(
                "terminal_execution_exists",
                f"Terminal execution {execution.execution_key!r} already exists",
            )
        terminal = TerminalSession(
            id=session_id,
            run_id=request.run_id,
            execution_id=execution.id,
            owner=request.owner,
            cols=request.cols,
            rows=request.rows,
        )
        await self._terminals.create(terminal)

        execution.transition_to(ExecutionStatus.STARTING)
        await self._executions.save(execution)
        master_fd, slave_fd = pty.openpty()
        try:
            _set_window_size(master_fd, request.cols, request.rows)
            environment = merge_environment(request.env, mode=request.environment_mode)
            _validate_target(request.argv[0], request.cwd, environment)
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
        except (OSError, ValueError):
            os.close(master_fd)
            execution.transition_to(ExecutionStatus.FAILED)
            await self._executions.save(execution)
            terminal.transition_to(TerminalStatus.CLOSED)
            await self._terminals.save(terminal)
            raise
        finally:
            os.close(slave_fd)

        execution.pid = process.pid
        execution.process_group_id = process.pid
        execution.transition_to(ExecutionStatus.RUNNING)
        await self._executions.save(execution)
        terminal.transition_to(TerminalStatus.OPEN)
        await self._terminals.save(terminal)

        reader_task = asyncio.create_task(
            self._pump_output(master_fd, terminal_paths.transcript),
            name=f"riftx-terminal-reader-{session_id}",
        )
        managed = _ManagedTerminal(
            process=process,
            master_fd=master_fd,
            transcript_path=terminal_paths.transcript,
            reader_task=reader_task,
            write_lock=asyncio.Lock(),
        )
        self._managed[session_id] = managed
        managed.monitor_task = asyncio.create_task(
            self._monitor(session_id, managed),
            name=f"riftx-terminal-monitor-{session_id}",
        )
        await self._events.append(
            request.run_id,
            "terminal.opened",
            {
                "session_id": session_id,
                "execution_id": execution.id,
                "argv": request.argv,
                "cwd": str(request.cwd),
                "owner": terminal.owner.value,
                "cols": terminal.cols,
                "rows": terminal.rows,
            },
        )
        return terminal

    async def get(self, session_id: str) -> TerminalSession:
        terminal = await self._terminals.get(session_id)
        if terminal is None:
            raise EntityNotFoundError("Terminal Session", session_id)
        return terminal

    async def get_execution(self, session_id: str) -> Execution:
        terminal = await self.get(session_id)
        execution = await self._executions.get(terminal.execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", terminal.execution_id)
        return execution

    async def read(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> OutputSlice:
        if max_bytes < 1 or max_bytes > 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 1048576")
        execution = await self.get_execution(session_id)
        return await asyncio.to_thread(
            _read_output_slice,
            Path(execution.stdout_path),
            cursor,
            max_bytes,
        )

    async def write(
        self,
        session_id: str,
        data: bytes,
        *,
        actor: TerminalOwner,
    ) -> None:
        terminal = await self.get(session_id)
        self._require_writer(terminal, actor)
        managed = self._require_managed(session_id)
        if not data:
            return
        lock = managed.write_lock
        if lock is None:
            raise RuntimeError("terminal write lock was not initialized")
        async with lock:
            await asyncio.to_thread(os.write, managed.master_fd, data)

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalSession:
        terminal = await self.get(session_id)
        terminal.resize(cols, rows)
        managed = self._require_managed(session_id)
        await asyncio.to_thread(_set_window_size, managed.master_fd, cols, rows)
        if managed.process.returncode is None:
            _signal_process_group(managed.process.pid, signal.SIGWINCH)
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.resized",
            {"session_id": terminal.id, "cols": cols, "rows": rows},
        )
        return terminal

    async def interrupt(self, session_id: str, *, actor: TerminalOwner) -> None:
        terminal = await self.get(session_id)
        self._require_writer(terminal, actor)
        managed = self._require_managed(session_id)
        if managed.process.returncode is None:
            _signal_process_group(managed.process.pid, signal.SIGINT)
        await self._events.append(
            terminal.run_id,
            "terminal.interrupted",
            {"session_id": terminal.id, "actor": actor.value},
        )

    async def take_over(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        terminal.take_over()
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.taken_over",
            {"session_id": terminal.id, "owner": terminal.owner.value},
        )
        return terminal

    async def release(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        terminal.release()
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.released",
            {"session_id": terminal.id, "owner": terminal.owner.value},
        )
        return terminal

    async def close(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        managed = self._managed.get(session_id)
        if managed is not None:
            managed.close_requested = True
            await _terminate_process(managed.process, self._termination_grace_seconds)
            if managed.monitor_task is not None:
                await asyncio.shield(managed.monitor_task)
            return await self.get(session_id)
        if terminal.status is TerminalStatus.OPEN:
            terminal.transition_to(TerminalStatus.LOST)
            await self._terminals.save(terminal)
            execution = await self._executions.get(terminal.execution_id)
            if execution is not None and execution.status in {
                ExecutionStatus.STARTING,
                ExecutionStatus.RUNNING,
            }:
                execution.transition_to(ExecutionStatus.LOST)
                await self._executions.save(execution)
            await self._events.append(
                terminal.run_id,
                "terminal.lost",
                {"session_id": terminal.id, "reason": "native_pty_not_attached"},
            )
        return terminal

    async def recover(self) -> list[TerminalSession]:
        lost: list[TerminalSession] = []
        for terminal in await self._terminals.list_open():
            terminal.transition_to(TerminalStatus.LOST)
            await self._terminals.save(terminal)
            execution = await self._executions.get(terminal.execution_id)
            if execution is not None and execution.status in {
                ExecutionStatus.STARTING,
                ExecutionStatus.RUNNING,
            }:
                execution.transition_to(ExecutionStatus.LOST)
                await self._executions.save(execution)
            await self._events.append(
                terminal.run_id,
                "terminal.lost",
                {"session_id": terminal.id, "reason": "runner_restarted"},
            )
            lost.append(terminal)
        return lost

    async def close_all(self) -> None:
        await asyncio.gather(
            *(self.close(session_id) for session_id in list(self._managed)),
            return_exceptions=True,
        )

    async def _pump_output(self, master_fd: int, transcript_path: Path) -> None:
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

    async def _monitor(self, session_id: str, managed: _ManagedTerminal) -> None:
        exit_code = await managed.process.wait()
        try:
            await asyncio.wait_for(asyncio.shield(managed.reader_task), timeout=1.0)
        except TimeoutError:
            os.close(managed.master_fd)
            await asyncio.gather(managed.reader_task, return_exceptions=True)
        else:
            os.close(managed.master_fd)

        terminal = await self.get(session_id)
        execution = await self._executions.get(terminal.execution_id)
        if execution is not None and execution.status in {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
        }:
            execution.transition_to(
                ExecutionStatus.CANCELLED if managed.close_requested else ExecutionStatus.EXITED,
                exit_code=exit_code,
            )
            await self._executions.save(execution)
        if terminal.status is TerminalStatus.OPEN:
            terminal.transition_to(TerminalStatus.CLOSED)
            await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.closed",
            {
                "session_id": terminal.id,
                "execution_id": terminal.execution_id,
                "exit_code": exit_code,
                "requested": managed.close_requested,
            },
        )
        self._managed.pop(session_id, None)

    def _require_managed(self, session_id: str) -> _ManagedTerminal:
        managed = self._managed.get(session_id)
        if managed is None:
            raise ApplicationConflictError(
                "terminal_not_attached",
                "The native PTY is not attached to this Runner process",
                details={"session_id": session_id},
            )
        return managed

    @staticmethod
    def _require_writer(terminal: TerminalSession, actor: TerminalOwner) -> None:
        if terminal.status is not TerminalStatus.OPEN:
            raise ApplicationConflictError(
                "terminal_not_open",
                f"Terminal {terminal.id!r} is {terminal.status.value}",
            )
        if terminal.owner is not TerminalOwner.SHARED and terminal.owner is not actor:
            raise ApplicationConflictError(
                "terminal_not_owned",
                f"Terminal input belongs to {terminal.owner.value!r}, not {actor.value!r}",
                details={"session_id": terminal.id, "owner": terminal.owner.value},
            )


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


def _read_output_slice(path: Path, cursor: int, max_bytes: int) -> OutputSlice:
    if cursor < 0:
        raise ValueError("output cursor must not be negative")
    if not path.exists():
        return OutputSlice(data=b"", cursor=cursor, next_cursor=cursor, eof=True)
    size = path.stat().st_size
    if cursor > size:
        raise ValueError(f"output cursor {cursor} is beyond file size {size}")
    with path.open("rb") as stream:
        stream.seek(cursor)
        data = stream.read(max_bytes)
    next_cursor = cursor + len(data)
    return OutputSlice(
        data=data,
        cursor=cursor,
        next_cursor=next_cursor,
        eof=next_cursor >= size,
    )


def _signal_process_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return


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
