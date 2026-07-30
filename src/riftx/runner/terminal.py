"""Cross-platform native terminal lifecycle and durable transcript management."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
from riftx.domain.base import new_id, utc_now
from riftx.executors import merge_environment

from .models import OutputSlice, TerminalLaunchRequest
from .paths import RunnerPaths
from .terminal_backend import NativeTerminalBackend, NativeTerminalHandle


class TerminalController(Protocol):
    async def start(self, request: TerminalLaunchRequest) -> TerminalSession: ...

    async def get(self, session_id: str) -> TerminalSession: ...

    async def get_execution(self, session_id: str) -> Execution: ...

    async def read(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> OutputSlice: ...

    async def write(
        self,
        session_id: str,
        data: bytes,
        *,
        actor: TerminalOwner,
    ) -> None: ...

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalSession: ...

    async def interrupt(self, session_id: str, *, actor: TerminalOwner) -> None: ...

    async def take_over(self, session_id: str) -> TerminalSession: ...

    async def release(self, session_id: str) -> TerminalSession: ...

    async def close(self, session_id: str) -> TerminalSession: ...


@dataclass(slots=True)
class _ManagedTerminal:
    handle: NativeTerminalHandle
    monitor_task: asyncio.Task[None] | None = None
    close_requested: bool = False


class TerminalSupervisor:
    """Own native PTY/ConPTY sessions while state and transcripts remain durable."""

    def __init__(
        self,
        *,
        terminal_repository: TerminalRepository,
        execution_repository: ExecutionRepository,
        event_repository: RunEventRepository,
        paths: RunnerPaths,
        termination_grace_seconds: float = 2.0,
        native_backend: NativeTerminalBackend | None = None,
        platform_name: str | None = None,
    ) -> None:
        self._terminals = terminal_repository
        self._executions = execution_repository
        self._events = event_repository
        self._paths = paths
        self._termination_grace_seconds = termination_grace_seconds
        self._native_backend = native_backend
        self._platform_name = platform_name or os.name
        self._managed: dict[str, _ManagedTerminal] = {}

    async def start(self, request: TerminalLaunchRequest) -> TerminalSession:
        backend = self._backend()
        session_id = request.session_id or new_id()
        execution_id = request.execution_id or new_id()
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
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            platform_system=platform.system().lower() or os.name,
            platform_release=platform.release(),
            platform_architecture=platform.machine() or "unknown",
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
            runner_id=request.node_id,
            shell=request.argv[0],
            cwd=str(request.cwd),
            owner=request.owner,
            cols=request.cols,
            rows=request.rows,
        )
        await self._terminals.create(terminal)

        execution.transition_to(ExecutionStatus.STARTING)
        await self._executions.save(execution)
        environment = merge_environment(request.env, mode=request.environment_mode)
        execution.executable_path = _resolve_terminal_executable(request.argv[0], environment)
        try:
            handle = await backend.start(
                request,
                transcript_path=terminal_paths.transcript,
                environment=environment,
            )
        except Exception:
            execution.transition_to(ExecutionStatus.FAILED)
            await self._executions.save(execution)
            terminal.transition_to(TerminalStatus.CLOSED)
            await self._terminals.save(terminal)
            raise

        execution.pid = handle.pid
        execution.process_group_id = handle.pid
        started_at = utc_now()
        execution.process_created_at = started_at
        execution.transition_to(ExecutionStatus.RUNNING, at=started_at)
        await self._executions.save(execution)
        terminal.transition_to(TerminalStatus.OPEN)
        await self._terminals.save(terminal)

        managed = _ManagedTerminal(handle=handle)
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
                "backend": "conpty" if self._platform_name == "nt" else "pty",
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
        await self._require_managed(session_id).handle.write(data)

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalSession:
        terminal = await self.get(session_id)
        terminal.resize(cols, rows)
        await self._require_managed(session_id).handle.resize(cols, rows)
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
        await self._require_managed(session_id).handle.interrupt()
        await self._events.append(
            terminal.run_id,
            "terminal.interrupted",
            {"session_id": terminal.id, "actor": actor.value},
        )

    async def take_over(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        execution = await self.get_execution(session_id)
        cursor = await asyncio.to_thread(_output_size, Path(execution.stdout_path))
        terminal.take_over(cursor=cursor)
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.taken_over",
            {"session_id": terminal.id, "owner": terminal.owner.value},
        )
        return terminal

    async def release(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        execution = await self.get_execution(session_id)
        cursor = await asyncio.to_thread(_output_size, Path(execution.stdout_path))
        terminal.release(cursor=cursor)
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
            await managed.handle.terminate(self._termination_grace_seconds)
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
                {"session_id": terminal.id, "reason": "native_terminal_not_attached"},
            )
        return terminal

    async def recover(self, *, node_id: str | None = None) -> list[TerminalSession]:
        lost: list[TerminalSession] = []
        for terminal in await self._terminals.list_open():
            execution = await self._executions.get(terminal.execution_id)
            if node_id is not None and (execution is None or execution.node_id != node_id):
                continue
            terminal.transition_to(TerminalStatus.LOST)
            await self._terminals.save(terminal)
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

    async def _monitor(self, session_id: str, managed: _ManagedTerminal) -> None:
        exit_code = await managed.handle.wait()
        await managed.handle.close_output()
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

    def _backend(self) -> NativeTerminalBackend:
        if self._native_backend is not None:
            return self._native_backend
        if self._platform_name == "posix":
            from .unix_pty import UnixPTYBackend

            self._native_backend = UnixPTYBackend()
            return self._native_backend
        if self._platform_name == "nt":
            from .conpty import ConPTYBackend

            self._native_backend = ConPTYBackend()
            return self._native_backend
        raise RuntimeError(f"native terminals are unsupported on {self._platform_name!r}")

    def _require_managed(self, session_id: str) -> _ManagedTerminal:
        managed = self._managed.get(session_id)
        if managed is None:
            raise ApplicationConflictError(
                "terminal_not_attached",
                "The native terminal is not attached to this Runner process",
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
        if terminal.owner is not actor:
            raise ApplicationConflictError(
                "terminal_not_owned",
                f"Terminal input belongs to {terminal.owner.value!r}, not {actor.value!r}",
                details={"session_id": terminal.id, "owner": terminal.owner.value},
            )


def _resolve_terminal_executable(
    executable: str,
    environment: dict[str, str],
) -> str | None:
    path = Path(executable)
    if path.is_absolute():
        return str(path.resolve(strict=False))
    return shutil.which(executable, path=environment.get("PATH"))


def _read_output_slice(path: Path, cursor: int, max_bytes: int) -> OutputSlice:
    if cursor < 0:
        raise ValueError("output cursor must not be negative")
    if not path.exists():
        return OutputSlice(data=b"", cursor=cursor, next_cursor=cursor, eof=True)
    size = path.stat().st_size
    if cursor > size:
        raise ValueError(f"terminal cursor {cursor} is beyond transcript size {size}")
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


def _output_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0
