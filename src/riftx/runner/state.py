"""Runner-local execution metadata storage without Control Plane foreign keys."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from riftx.application.errors import EntityNotFoundError
from riftx.domain import Execution, ExecutionStatus, TerminalSession, TerminalStatus

_ACTIVE_STATUSES = {
    ExecutionStatus.QUEUED,
    ExecutionStatus.CREATED,
    ExecutionStatus.STARTING,
    ExecutionStatus.RUNNING,
}


class FileExecutionRepository:
    """Small atomic JSON store used by the standalone Runner daemon."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def create_if_absent(self, execution: Execution) -> tuple[Execution, bool]:
        async with self._lock:
            items = await asyncio.to_thread(self._read)
            for existing in items.values():
                if existing.execution_key == execution.execution_key:
                    return _copy(existing), False
            items[execution.id] = _copy(execution)
            await asyncio.to_thread(self._write, items)
            return _copy(execution), True

    async def get(self, execution_id: str) -> Execution | None:
        async with self._lock:
            execution = (await asyncio.to_thread(self._read)).get(execution_id)
            return _copy(execution) if execution is not None else None

    async def get_by_key(self, execution_key: str) -> Execution | None:
        async with self._lock:
            for execution in (await asyncio.to_thread(self._read)).values():
                if execution.execution_key == execution_key:
                    return _copy(execution)
        return None

    async def save(self, execution: Execution) -> Execution:
        async with self._lock:
            items = await asyncio.to_thread(self._read)
            if execution.id not in items:
                raise EntityNotFoundError("Execution", execution.id)
            items[execution.id] = _copy(execution)
            await asyncio.to_thread(self._write, items)
        return _copy(execution)

    async def list_active(self) -> Sequence[Execution]:
        async with self._lock:
            items = await asyncio.to_thread(self._read)
            return [_copy(item) for item in items.values() if item.status in _ACTIVE_STATUSES]

    async def list(
        self,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Execution]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")
        async with self._lock:
            matches = [
                _copy(item)
                for item in (await asyncio.to_thread(self._read)).values()
                if item.run_id == run_id
            ]
        matches.sort(key=lambda item: (item.started_at is None, item.started_at, item.id))
        return matches[offset : offset + limit]

    def _read(self) -> dict[str, Execution]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Runner execution state is corrupted: {self.path}") from exc
        if not isinstance(raw, list):
            raise RuntimeError(f"Runner execution state has an invalid shape: {self.path}")
        return {item.id: item for item in (Execution.model_validate(value) for value in raw)}

    def _write(self, items: dict[str, Execution]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in items.values()],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        temporary.replace(self.path)


class FileTerminalRepository:
    """Atomic JSON state for native terminals owned by a standalone Runner."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def create(self, terminal: TerminalSession) -> TerminalSession:
        async with self._lock:
            items = await asyncio.to_thread(self._read)
            if terminal.id in items:
                raise RuntimeError(f"terminal session already exists: {terminal.id}")
            items[terminal.id] = _copy_terminal(terminal)
            await asyncio.to_thread(self._write, items)
        return _copy_terminal(terminal)

    async def get(self, session_id: str) -> TerminalSession | None:
        async with self._lock:
            terminal = (await asyncio.to_thread(self._read)).get(session_id)
            return _copy_terminal(terminal) if terminal is not None else None

    async def get_by_execution(self, execution_id: str) -> TerminalSession | None:
        async with self._lock:
            for terminal in (await asyncio.to_thread(self._read)).values():
                if terminal.execution_id == execution_id:
                    return _copy_terminal(terminal)
        return None

    async def save(self, terminal: TerminalSession) -> TerminalSession:
        async with self._lock:
            items = await asyncio.to_thread(self._read)
            if terminal.id not in items:
                raise EntityNotFoundError("TerminalSession", terminal.id)
            items[terminal.id] = _copy_terminal(terminal)
            await asyncio.to_thread(self._write, items)
        return _copy_terminal(terminal)

    async def list_open(self) -> Sequence[TerminalSession]:
        async with self._lock:
            items = await asyncio.to_thread(self._read)
            return [
                _copy_terminal(item)
                for item in items.values()
                if item.status is TerminalStatus.OPEN
            ]

    def _read(self) -> dict[str, TerminalSession]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Runner terminal state is corrupted: {self.path}") from exc
        if not isinstance(raw, list):
            raise RuntimeError(f"Runner terminal state has an invalid shape: {self.path}")
        terminals = (TerminalSession.model_validate(value) for value in raw)
        return {item.id: item for item in terminals}

    def _write(self, items: dict[str, TerminalSession]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in items.values()],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        temporary.replace(self.path)


def _copy(execution: Execution) -> Execution:
    return Execution.model_validate(execution.model_dump())


def _copy_terminal(terminal: TerminalSession) -> TerminalSession:
    return TerminalSession.model_validate(terminal.model_dump())
