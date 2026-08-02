"""Remote Runner command handler for native PTY and ConPTY sessions."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import ExecutionRepository, TerminalRepository
from riftx.domain import (
    Execution,
    ExecutionStatus,
    RunEvent,
    RunnerCommandKind,
    RunnerPrincipal,
    TerminalSession,
    TerminalStatus,
)
from riftx.domain.base import new_id

from ._durable_file import atomic_write_json, locked_file
from .control_client import OutputOffsetMismatch, RunnerControlClientError
from .models import TerminalLaunchRequest
from .protocols import EffectGuard
from .supervisor import ProcessTerminationError
from .terminal import TerminalSupervisor
from .terminal_identity import require_terminal_start_replay_matches

logger = logging.getLogger(__name__)

_FINAL_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
    ExecutionStatus.LOST,
}
_PHYSICAL_STOP_PROOF_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
}


class TerminalControlClient(Protocol):
    @property
    def principal(self) -> RunnerPrincipal | None: ...

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        *,
        pid: int | None = None,
        process_group_id: int | None = None,
        exit_code: int | None = None,
        physical_stop_confirmed: bool = False,
    ) -> None: ...

    async def report_output(
        self,
        execution_id: str,
        *,
        stream: str,
        offset: int,
        data: bytes,
    ) -> int: ...


class NullRunEventRepository:
    """Runner-local terminal events are authoritative only in the Control Plane."""

    def __init__(self) -> None:
        self._sequence = 0

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str | None = None,
    ) -> RunEvent:
        self._sequence += 1
        return RunEvent(
            id=event_id or new_id(),
            run_id=run_id,
            sequence=self._sequence,
            event_type=event_type,
            payload=payload or {},
        )

    async def append_terminal_projection_if_current(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str,
        session_id: str,
        expected_terminal_status: TerminalStatus,
        expected_execution_status: ExecutionStatus,
    ) -> RunEvent | None:
        # Runner-local instances do not own the Control Plane projection.  The
        # method exists only to preserve the repository protocol for call sites
        # that intentionally discard these events.
        del session_id, expected_terminal_status, expected_execution_status
        return await self.append(
            run_id,
            event_type,
            payload,
            event_id=event_id,
        )

    async def list_after(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Sequence[RunEvent]:
        return []


class OperationJournal:
    """Persist one-way Runner operation facts across command re-leases."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def contains(self, operation_id: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._contains_locked, operation_id)

    async def add(self, operation_id: str) -> None:
        await self.claim(operation_id)

    async def claim(self, operation_id: str) -> bool:
        """Atomically persist an operation id and report whether this caller won."""

        async with self._lock:
            return await asyncio.to_thread(self._claim_locked, operation_id)

    def _contains_locked(self, operation_id: str) -> bool:
        with locked_file(self.path):
            return operation_id in self._read()

    def _claim_locked(self, operation_id: str) -> bool:
        # The read/merge/replace transaction must happen under the OS lock.
        # Per-instance asyncio locks cannot protect two Runner processes (or
        # two independently constructed journal objects) sharing state_path.
        with locked_file(self.path):
            items = self._read()
            if operation_id in items:
                return False
            items.add(operation_id)
            self._write(items)
            return True

    def _read(self) -> set[str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"terminal operation journal is corrupted: {self.path}") from exc
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise RuntimeError(f"terminal operation journal has an invalid shape: {self.path}")
        return set(raw)

    def _write(self, items: set[str]) -> None:
        atomic_write_json(self.path, sorted(items))


class RemoteTerminalManager:
    """Execute durable terminal commands and stream transcripts back to the server."""

    def __init__(
        self,
        *,
        node_id: str,
        supervisor: TerminalSupervisor,
        terminals: TerminalRepository,
        executions: ExecutionRepository,
        client: TerminalControlClient,
        operation_journal: OperationJournal,
        output_poll_seconds: float = 0.1,
    ) -> None:
        self._node_id = node_id
        self._supervisor = supervisor
        self._terminals = terminals
        self._executions = executions
        self._client = client
        self._journal = operation_journal
        self._output_poll_seconds = output_poll_seconds
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._command_lock = asyncio.Lock()
        self._recovered = False
        self._closed = False

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        effect_guard: EffectGuard | None = None,
        on_admitted: Callable[[], None] | None = None,
    ) -> object:
        if kind is RunnerCommandKind.TERMINAL_START:
            return await self._start(
                payload,
                effect_guard=effect_guard,
                on_admitted=on_admitted,
            )
        if kind not in {
            RunnerCommandKind.TERMINAL_WRITE,
            RunnerCommandKind.TERMINAL_RESIZE,
            RunnerCommandKind.TERMINAL_INTERRUPT,
            RunnerCommandKind.TERMINAL_CLOSE,
        }:
            raise ValueError(f"unsupported terminal command: {kind.value}")

        operation_id = _required_string(payload, "operation_id")
        async with self._command_lock:
            if await self._journal.contains(operation_id):
                return {"operation_id": operation_id, "duplicate": True}
            result = await self._apply_operation(kind, payload)
            await self._journal.add(operation_id)
            return {**result, "operation_id": operation_id, "duplicate": False}

    async def resume_active(self) -> None:
        if self._recovered:
            return
        self._recovered = True
        owner = self._client_principal()
        for terminal in await self._supervisor.recover(owner=owner):
            execution = await self._require_execution(terminal.execution_id)
            self._require_execution_owner(execution, owner)
            await self._report_with_retry(execution)

    async def cancel_execution(self, execution_id: str) -> Execution:
        """Stop a PTY/ConPTY through its native supervisor and return observed state."""
        owner = self._client_principal()
        before = await self._require_execution(execution_id)
        self._require_execution_owner(before, owner)
        execution = await self._supervisor.close_execution(execution_id)
        self._require_execution_owner(execution, owner)
        terminal = await self._terminals.get_by_execution(execution_id)
        if (
            execution.status not in _PHYSICAL_STOP_PROOF_STATUSES
            or execution.physical_stop_confirmed_at is None
        ):
            raise ProcessTerminationError(
                f"Terminal execution {execution_id!r} did not provide affirmative "
                "physical-stop confirmation"
            )
        if terminal is not None and terminal.status is not TerminalStatus.CLOSED:
            terminal.transition_to(TerminalStatus.CLOSED)
            await self._terminals.save(terminal)
        return execution

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._monitors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        owner = self._client_principal()
        terminal_stops = [
            asyncio.create_task(
                self._close_terminal_on_shutdown(terminal, owner),
                name=f"riftx-runner-close-terminal-{terminal.id}",
            )
            for terminal in await self._terminals.list_active()
        ]
        results = await asyncio.gather(*terminal_stops, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            for error in errors[1:]:
                logger.error("Additional terminal shutdown failure: %r", error)
            raise errors[0]

    async def _close_terminal_on_shutdown(
        self,
        terminal: TerminalSession,
        owner: RunnerPrincipal,
    ) -> None:
        execution = await self._require_execution(terminal.execution_id)
        if execution.owner != owner:
            logger.error(
                "Refusing to close terminal %s owned by another Runner principal",
                terminal.id,
            )
            return
        await self._supervisor.close(terminal.id)
        execution = await self._require_execution(terminal.execution_id)
        self._require_execution_owner(execution, owner)
        try:
            await self._forward_once(execution.id, 0)
            await self._report_execution(execution)
        except RunnerControlClientError:
            logger.warning(
                "Unable to report terminal %s during Runner shutdown",
                terminal.id,
            )

    async def _start(
        self,
        payload: dict[str, object],
        *,
        effect_guard: EffectGuard | None,
        on_admitted: Callable[[], None] | None,
    ) -> dict[str, object]:
        session_id = _required_string(payload, "session_id")
        execution_id = _required_string(payload, "execution_id")
        raw_request = payload.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("terminal_start command is missing request")
        request = TerminalLaunchRequest.model_validate(raw_request)
        owner = self._client_principal()
        if request.runner_principal != owner:
            raise ValueError("terminal_start request owner does not match this Runner")
        if request.session_id not in {None, session_id}:
            raise ValueError("terminal_start session IDs do not match")
        if request.execution_id not in {None, execution_id}:
            raise ValueError("terminal_start execution IDs do not match")
        if request.node_id != self._node_id:
            raise ValueError("terminal_start command targets a different Runner node")
        request = request.model_copy(
            update={"session_id": session_id, "execution_id": execution_id}
        )

        existing = await self._terminals.get(session_id)
        if existing is not None:
            if existing.execution_id != execution_id:
                raise ValueError("terminal_start conflicts with an existing session")
            execution = await self._require_execution(execution_id)
            require_terminal_start_replay_matches(existing, execution, request)
            if existing.status is TerminalStatus.CREATED and execution.status in {
                ExecutionStatus.CREATED,
                ExecutionStatus.STARTING,
            }:
                # Do not turn a crash between durable admission phases into a
                # successful duplicate.  The supervisor can safely converge
                # CREATED, while STARTING remains fail-closed and is never
                # replayed without an attached native handle.
                try:
                    existing = await self._supervisor.start(
                        request,
                        effect_guard=effect_guard,
                    )
                except Exception:
                    if on_admitted is not None:
                        on_admitted()
                    execution = await self._executions.get(execution_id)
                    if execution is not None and execution.status in _FINAL_STATUSES:
                        await self._report_execution(execution)
                    raise
                if on_admitted is not None:
                    on_admitted()
                execution = await self._require_execution(existing.execution_id)
                await self._report_execution(execution)
                if execution.status not in _FINAL_STATUSES:
                    self._start_monitor(execution.id)
                return {
                    "session_id": existing.id,
                    "execution_id": execution.id,
                    "status": execution.status.value,
                    "duplicate": True,
                }
            # A foreign payload must be rejected before invoking any callback,
            # reporting durable state, or starting a monitor. The complete
            # replay identity above includes the authenticated Runner owner.
            if effect_guard is not None:
                await effect_guard()
            if on_admitted is not None:
                on_admitted()
            await self._report_execution(execution)
            if execution.status not in _FINAL_STATUSES:
                self._start_monitor(execution_id)
            return {
                "session_id": session_id,
                "execution_id": execution_id,
                "status": execution.status.value,
                "duplicate": True,
            }

        try:
            terminal = await self._supervisor.start(
                request,
                effect_guard=effect_guard,
            )
        except Exception:
            # Local admission has resolved (including guarded pre/post-spawn
            # cleanup). Release the daemon's execution lock before any status
            # upload so Control Plane I/O cannot delay a safety stop.
            if on_admitted is not None:
                on_admitted()
            execution = await self._executions.get(execution_id)
            if execution is not None and execution.status in _FINAL_STATUSES:
                await self._report_execution(execution)
            raise
        if on_admitted is not None:
            on_admitted()
        execution = await self._require_execution(terminal.execution_id)
        self._require_execution_owner(execution, owner)
        await self._report_execution(execution)
        self._start_monitor(execution.id)
        return {
            "session_id": terminal.id,
            "execution_id": execution.id,
            "status": execution.status.value,
            "duplicate": False,
        }

    async def _apply_operation(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
    ) -> dict[str, object]:
        terminal = await self._require_terminal(payload)
        execution = await self._require_execution(terminal.execution_id)
        self._require_execution_owner(execution, self._client_principal())
        if kind is RunnerCommandKind.TERMINAL_WRITE:
            encoded = _required_string(payload, "data")
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("terminal_write data is not valid base64") from exc
            if len(data) > 64 * 1024:
                raise ValueError("terminal input exceeds 65536 bytes")
            await self._supervisor.write(terminal.id, data, actor=terminal.owner)
            return {"session_id": terminal.id, "bytes_written": len(data)}
        if kind is RunnerCommandKind.TERMINAL_RESIZE:
            cols = _required_positive_int(payload, "cols")
            rows = _required_positive_int(payload, "rows")
            resized = await self._supervisor.resize(terminal.id, cols=cols, rows=rows)
            return {"session_id": terminal.id, "cols": resized.cols, "rows": resized.rows}
        if kind is RunnerCommandKind.TERMINAL_INTERRUPT:
            await self._supervisor.interrupt(terminal.id, actor=terminal.owner)
            return {"session_id": terminal.id, "interrupted": True}
        closed = await self._supervisor.close(terminal.id)
        execution = await self._require_execution(closed.execution_id)
        await self._forward_once(execution.id, 0)
        await self._report_execution(execution)
        return {"session_id": terminal.id, "status": closed.status.value}

    async def _require_terminal(self, payload: dict[str, object]) -> TerminalSession:
        session_id = _required_string(payload, "session_id")
        execution_id = _required_string(payload, "execution_id")
        terminal = await self._terminals.get(session_id)
        if terminal is None:
            raise EntityNotFoundError("Terminal Session", session_id)
        if terminal.execution_id != execution_id:
            raise ValueError("terminal command execution does not match the session")
        return terminal

    async def _require_execution(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution

    def _start_monitor(self, execution_id: str) -> None:
        current = self._monitors.get(execution_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._monitor(execution_id),
            name=f"riftx-remote-terminal-monitor-{execution_id}",
        )
        self._monitors[execution_id] = task
        task.add_done_callback(lambda _: self._monitors.pop(execution_id, None))

    async def _monitor(self, execution_id: str) -> None:
        cursor = 0
        while not self._closed:
            try:
                cursor = await self._forward_once(execution_id, cursor)
                execution = await self._require_execution(execution_id)
                if execution.status in _FINAL_STATUSES:
                    cursor = await self._forward_once(execution_id, cursor)
                    await self._report_with_retry(execution)
                    return
            except RunnerControlClientError:
                logger.warning("Terminal output forwarding failed for %s; retrying", execution_id)
            await asyncio.sleep(self._output_poll_seconds)

    async def _forward_once(self, execution_id: str, cursor: int) -> int:
        execution = await self._require_execution(execution_id)
        self._require_execution_owner(execution, self._client_principal())
        terminal = await self._terminals.get_by_execution(execution_id)
        if terminal is None:
            return cursor
        output = await self._supervisor.read(terminal.id, cursor=cursor, max_bytes=256 * 1024)
        if not output.data:
            return cursor
        try:
            return await self._client.report_output(
                execution_id,
                stream="stdout",
                offset=output.cursor,
                data=output.data,
            )
        except OutputOffsetMismatch as exc:
            return exc.expected_offset

    async def _report_with_retry(self, execution: Execution) -> None:
        while not self._closed:
            try:
                await self._report_execution(execution)
                return
            except RunnerControlClientError:
                await asyncio.sleep(self._output_poll_seconds)

    async def _report_execution(self, execution: Execution) -> None:
        self._require_execution_owner(execution, self._client_principal())
        await self._client.report_status(
            execution.id,
            execution.status,
            pid=execution.pid if execution.status is ExecutionStatus.RUNNING else None,
            process_group_id=(
                execution.process_group_id if execution.status is ExecutionStatus.RUNNING else None
            ),
            exit_code=execution.exit_code if execution.status in _FINAL_STATUSES else None,
            physical_stop_confirmed=(execution.physical_stop_confirmed_at is not None),
        )

    def _client_principal(self) -> RunnerPrincipal:
        principal = self._client.principal
        if principal is None:
            raise RuntimeError("Runner terminal client has no authenticated principal")
        return principal

    @staticmethod
    def _require_execution_owner(
        execution: Execution,
        owner: RunnerPrincipal,
    ) -> None:
        if execution.owner != owner:
            raise RuntimeError(
                f"Terminal execution {execution.id!r} belongs to another Runner principal"
            )


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"terminal command is missing {key}")
    return value


def _required_positive_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"terminal command {key} must be a positive integer")
    return value
