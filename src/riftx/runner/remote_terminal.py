"""Central routing and durable state for terminals hosted by remote Runners."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.ports import (
    ExecutionRepository,
    RunEventRepository,
    TerminalRepository,
)
from riftx.application.services.runner_control import RunnerControlService
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    RunnerCommandKind,
    TerminalOwner,
    TerminalSession,
    TerminalStatus,
)
from riftx.domain.base import new_id

from .models import OutputSlice, TerminalLaunchRequest
from .paths import RunnerPaths
from .terminal import TerminalController


class RemoteTerminalSupervisor:
    """Persist terminal state centrally and dispatch operations to a remote node."""

    def __init__(
        self,
        *,
        terminal_repository: TerminalRepository,
        execution_repository: ExecutionRepository,
        event_repository: RunEventRepository,
        control: RunnerControlService,
        paths: RunnerPaths,
    ) -> None:
        self._terminals = terminal_repository
        self._executions = execution_repository
        self._events = event_repository
        self._control = control
        self._paths = paths

    async def start(self, request: TerminalLaunchRequest) -> TerminalSession:
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
            stdout_path=str(terminal_paths.transcript),
            stderr_path=str(terminal_paths.transcript),
        )
        execution, created = await self._executions.create_if_absent(execution)
        if not created:
            return await self.get(session_id)
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
        try:
            await self._control.enqueue(
                request.node_id,
                kind=RunnerCommandKind.TERMINAL_START,
                idempotency_key=f"terminal-start:{session_id}",
                payload={
                    "session_id": session_id,
                    "execution_id": execution.id,
                    "request": request.model_copy(
                        update={
                            "session_id": session_id,
                            "execution_id": execution.id,
                        }
                    ).model_dump(mode="json"),
                },
            )
        except Exception:
            execution.transition_to(ExecutionStatus.LOST)
            await self._executions.save(execution)
            terminal.transition_to(TerminalStatus.LOST)
            await self._terminals.save(terminal)
            raise
        execution.transition_to(ExecutionStatus.RUNNING)
        await self._executions.save(execution)
        terminal.transition_to(TerminalStatus.OPEN)
        await self._terminals.save(terminal)
        await self._events.append(
            request.run_id,
            "terminal.opened",
            {
                "session_id": terminal.id,
                "execution_id": execution.id,
                "node_id": request.node_id,
                "backend": "remote",
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
        if not data:
            return
        if len(data) > 64 * 1024:
            raise ValueError("terminal input exceeds 65536 bytes")
        execution = await self.get_execution(session_id)
        await self._enqueue_operation(
            execution,
            RunnerCommandKind.TERMINAL_WRITE,
            {"session_id": session_id, "data": base64.b64encode(data).decode("ascii")},
        )

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalSession:
        terminal = await self.get(session_id)
        terminal.resize(cols, rows)
        execution = await self.get_execution(session_id)
        await self._enqueue_operation(
            execution,
            RunnerCommandKind.TERMINAL_RESIZE,
            {"session_id": session_id, "cols": cols, "rows": rows},
        )
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
        execution = await self.get_execution(session_id)
        await self._enqueue_operation(
            execution,
            RunnerCommandKind.TERMINAL_INTERRUPT,
            {"session_id": session_id},
        )
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
        if terminal.status is not TerminalStatus.OPEN:
            return terminal
        execution = await self.get_execution(session_id)
        operation_id = f"terminal-close:{session_id}"
        await self._control.enqueue(
            execution.node_id,
            kind=RunnerCommandKind.TERMINAL_CLOSE,
            idempotency_key=operation_id,
            payload={
                "session_id": session_id,
                "execution_id": execution.id,
                "operation_id": operation_id,
            },
        )
        terminal.transition_to(TerminalStatus.CLOSED)
        await self._terminals.save(terminal)
        if execution.status in {ExecutionStatus.STARTING, ExecutionStatus.RUNNING}:
            execution.transition_to(ExecutionStatus.CANCELLED)
            await self._executions.save(execution)
        await self._events.append(
            terminal.run_id,
            "terminal.closed",
            {
                "session_id": terminal.id,
                "execution_id": terminal.execution_id,
                "requested": True,
            },
        )
        return terminal

    async def recover(self) -> list[TerminalSession]:
        return []

    async def close_all(self) -> None:
        return None

    async def _enqueue_operation(
        self,
        execution: Execution,
        kind: RunnerCommandKind,
        payload: dict[str, object],
    ) -> None:
        operation_id = f"{kind.value}:{execution.id}:{new_id()}"
        await self._control.enqueue(
            execution.node_id,
            kind=kind,
            idempotency_key=operation_id,
            payload={
                **payload,
                "execution_id": execution.id,
                "operation_id": operation_id,
            },
        )

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
            )


class NodeTerminalRouter:
    """Route terminal operations by the execution node persisted for the session."""

    def __init__(
        self,
        *,
        local_node_id: str,
        terminal_repository: TerminalRepository,
        execution_repository: ExecutionRepository,
        local: TerminalController,
        remote: TerminalController,
    ) -> None:
        self._local_node_id = local_node_id
        self._terminals = terminal_repository
        self._executions = execution_repository
        self._local = local
        self._remote = remote

    async def start(self, request: TerminalLaunchRequest) -> TerminalSession:
        return await self._for_node(request.node_id).start(request)

    async def get(self, session_id: str) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.get(session_id)

    async def get_execution(self, session_id: str) -> Execution:
        controller = await self._for_session(session_id)
        return await controller.get_execution(session_id)

    async def read(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> OutputSlice:
        controller = await self._for_session(session_id)
        return await controller.read(session_id, cursor=cursor, max_bytes=max_bytes)

    async def write(
        self,
        session_id: str,
        data: bytes,
        *,
        actor: TerminalOwner,
    ) -> None:
        controller = await self._for_session(session_id)
        await controller.write(session_id, data, actor=actor)

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.resize(session_id, cols=cols, rows=rows)

    async def interrupt(self, session_id: str, *, actor: TerminalOwner) -> None:
        controller = await self._for_session(session_id)
        await controller.interrupt(session_id, actor=actor)

    async def take_over(self, session_id: str) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.take_over(session_id)

    async def release(self, session_id: str) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.release(session_id)

    async def close(self, session_id: str) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.close(session_id)

    async def _for_session(self, session_id: str) -> TerminalController:
        terminal = await self._terminals.get(session_id)
        if terminal is None:
            raise EntityNotFoundError("Terminal Session", session_id)
        execution = await self._executions.get(terminal.execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", terminal.execution_id)
        return self._for_node(execution.node_id)

    def _for_node(self, node_id: str) -> TerminalController:
        return self._local if node_id == self._local_node_id else self._remote


def _read_output_slice(path: Path, cursor: int, max_bytes: int) -> OutputSlice:
    if cursor < 0:
        raise ValueError("terminal cursor must not be negative")
    if not path.exists():
        return OutputSlice(data=b"", cursor=cursor, next_cursor=cursor, eof=True)
    size = path.stat().st_size
    if cursor > size:
        raise ValueError(f"terminal cursor {cursor} is beyond transcript size {size}")
    with path.open("rb") as stream:
        stream.seek(cursor)
        data = stream.read(max_bytes)
    next_cursor = cursor + len(data)
    return OutputSlice(data=data, cursor=cursor, next_cursor=next_cursor, eof=next_cursor >= size)
