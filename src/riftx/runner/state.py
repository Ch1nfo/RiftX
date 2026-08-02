"""Runner-local execution metadata storage without Control Plane foreign keys."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Sequence
from pathlib import Path

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import ExecutionAdmissionIdentity
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    TerminalSession,
    TerminalStatus,
)

from ._durable_file import atomic_write_json, locked_file

_ACTIVE_STATUSES = {
    ExecutionStatus.QUEUED,
    ExecutionStatus.CREATED,
    ExecutionStatus.STARTING,
    ExecutionStatus.RUNNING,
}
_ACTIVE_TERMINAL_STATUSES = {
    TerminalStatus.CREATED,
    TerminalStatus.OPEN,
}


class FileExecutionRepository:
    """Crash-durable JSON store used by the standalone Runner daemon."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def create_if_absent(self, execution: Execution) -> tuple[Execution, bool]:
        async with self._lock:
            return await asyncio.to_thread(self._create_if_absent, _copy(execution))

    async def get(self, execution_id: str) -> Execution | None:
        async with self._lock:
            execution = await asyncio.to_thread(self._get, execution_id)
            return _copy(execution) if execution is not None else None

    async def get_by_key(self, execution_key: str) -> Execution | None:
        async with self._lock:
            execution = await asyncio.to_thread(self._get_by_key, execution_key)
            return _copy(execution) if execution is not None else None

    async def find_admission(
        self,
        identity: ExecutionAdmissionIdentity,
    ) -> Execution | None:
        async with self._lock:
            execution = await asyncio.to_thread(self._find_admission, identity)
            return _copy(execution) if execution is not None else None

    async def save(self, execution: Execution) -> Execution:
        async with self._lock:
            saved = await asyncio.to_thread(self._save, _copy(execution))
        return _copy(saved)

    async def save_if_status(
        self,
        execution: Execution,
        *,
        expected: Collection[ExecutionStatus],
    ) -> tuple[Execution, bool]:
        expected_statuses = set(expected)
        if not expected_statuses:
            raise ValueError("expected execution statuses cannot be empty")
        async with self._lock:
            saved, updated = await asyncio.to_thread(
                self._save_if_status,
                _copy(execution),
                expected_statuses,
            )
        return _copy(saved), updated

    async def list_active(self) -> Sequence[Execution]:
        async with self._lock:
            items = await asyncio.to_thread(self._list_active)
            return [_copy(item) for item in items]

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
            matches = await asyncio.to_thread(self._list_for_run, run_id)
        matches.sort(key=lambda item: (item.started_at is None, item.started_at, item.id))
        return [_copy(item) for item in matches[offset : offset + limit]]

    def _create_if_absent(self, execution: Execution) -> tuple[Execution, bool]:
        with locked_file(self.path):
            items = self._read()
            existing_id = items.get(execution.id)
            if existing_id is not None:
                if existing_id.execution_key != execution.execution_key:
                    raise RuntimeError(
                        f"execution id {execution.id!r} is already bound to key "
                        f"{existing_id.execution_key!r}"
                    )
                _validate_execution_duplicate(existing_id, execution)
                return _copy(existing_id), False
            for existing in items.values():
                if existing.execution_key == execution.execution_key:
                    _validate_execution_duplicate(existing, execution)
                    return _copy(existing), False
            items[execution.id] = execution
            self._write(items)
            return _copy(execution), True

    def _get(self, execution_id: str) -> Execution | None:
        with locked_file(self.path):
            return self._read().get(execution_id)

    def _get_by_key(self, execution_key: str) -> Execution | None:
        with locked_file(self.path):
            for execution in self._read().values():
                if execution.execution_key == execution_key:
                    return execution
        return None

    def _find_admission(
        self,
        identity: ExecutionAdmissionIdentity,
    ) -> Execution | None:
        with locked_file(self.path):
            for execution in self._read().values():
                if identity.matches(execution):
                    return execution
        return None

    def _save(self, execution: Execution) -> Execution:
        with locked_file(self.path):
            items = self._read()
            current = items.get(execution.id)
            if current is None:
                raise EntityNotFoundError("Execution", execution.id)
            stale = _validate_execution_update(current, execution)
            if stale:
                raise RuntimeError(
                    f"stale execution update would clear a bound physical identity "
                    f"for {current.id!r}"
                )
            if not _execution_status_update_is_monotonic(current.status, execution.status):
                return current
            items[execution.id] = execution
            self._write(items)
            return execution

    def _save_if_status(
        self,
        execution: Execution,
        expected: set[ExecutionStatus],
    ) -> tuple[Execution, bool]:
        with locked_file(self.path):
            items = self._read()
            current = items.get(execution.id)
            if current is None:
                raise EntityNotFoundError("Execution", execution.id)
            stale = _validate_execution_update(current, execution)
            if (
                current.status not in expected
                or stale
                or not _execution_status_update_is_monotonic(current.status, execution.status)
            ):
                return current, False
            items[execution.id] = execution
            self._write(items)
            return execution, True

    def _list_active(self) -> list[Execution]:
        with locked_file(self.path):
            return [item for item in self._read().values() if item.status in _ACTIVE_STATUSES]

    def _list_for_run(self, run_id: str) -> list[Execution]:
        with locked_file(self.path):
            return [item for item in self._read().values() if item.run_id == run_id]

    def _read(self) -> dict[str, Execution]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Runner execution state is corrupted: {self.path}") from exc
        if not isinstance(raw, list):
            raise RuntimeError(f"Runner execution state has an invalid shape: {self.path}")
        restored: list[Execution] = []
        for value in raw:
            if isinstance(value, dict) and "created_at" not in value:
                # Runner state written before RX-LN-01 has no trustworthy
                # admission timestamp. Do not invoke the new-row default and
                # manufacture a different chronology on every restart.
                value = {**value, "created_at": None}
            restored.append(Execution.model_validate(value))
        return {item.id: item for item in restored}

    def _write(self, items: dict[str, Execution]) -> None:
        atomic_write_json(
            self.path,
            [item.model_dump(mode="json") for item in items.values()],
        )


class FileTerminalRepository:
    """Crash-durable JSON state for native terminals owned by a Runner."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def create(self, terminal: TerminalSession) -> TerminalSession:
        async with self._lock:
            created = await asyncio.to_thread(self._create, _copy_terminal(terminal))
        return _copy_terminal(created)

    async def get(self, session_id: str) -> TerminalSession | None:
        async with self._lock:
            terminal = await asyncio.to_thread(self._get, session_id)
            return _copy_terminal(terminal) if terminal is not None else None

    async def get_by_execution(self, execution_id: str) -> TerminalSession | None:
        async with self._lock:
            terminal = await asyncio.to_thread(self._get_by_execution, execution_id)
            return _copy_terminal(terminal) if terminal is not None else None

    async def save(self, terminal: TerminalSession) -> TerminalSession:
        async with self._lock:
            saved = await asyncio.to_thread(self._save, _copy_terminal(terminal))
        return _copy_terminal(saved)

    async def save_if_status(
        self,
        terminal: TerminalSession,
        *,
        expected: Collection[TerminalStatus],
    ) -> tuple[TerminalSession, bool]:
        expected_statuses = set(expected)
        if not expected_statuses:
            raise ValueError("expected terminal statuses cannot be empty")
        async with self._lock:
            saved, updated = await asyncio.to_thread(
                self._save_if_status,
                _copy_terminal(terminal),
                expected_statuses,
            )
        return _copy_terminal(saved), updated

    async def list_open(self) -> Sequence[TerminalSession]:
        async with self._lock:
            items = await asyncio.to_thread(self._list_with_statuses, {TerminalStatus.OPEN})
            return [_copy_terminal(item) for item in items]

    async def list_active(self) -> Sequence[TerminalSession]:
        """Include pre-open rows whose matching execution may already exist."""

        async with self._lock:
            items = await asyncio.to_thread(
                self._list_with_statuses,
                _ACTIVE_TERMINAL_STATUSES,
            )
            return [_copy_terminal(item) for item in items]

    def _create(self, terminal: TerminalSession) -> TerminalSession:
        with locked_file(self.path):
            items = self._read()
            if terminal.id in items:
                raise RuntimeError(f"terminal session already exists: {terminal.id}")
            items[terminal.id] = terminal
            self._write(items)
            return terminal

    def _get(self, session_id: str) -> TerminalSession | None:
        with locked_file(self.path):
            return self._read().get(session_id)

    def _get_by_execution(self, execution_id: str) -> TerminalSession | None:
        with locked_file(self.path):
            for terminal in self._read().values():
                if terminal.execution_id == execution_id:
                    return terminal
        return None

    def _save(self, terminal: TerminalSession) -> TerminalSession:
        with locked_file(self.path):
            items = self._read()
            current = items.get(terminal.id)
            if current is None:
                raise EntityNotFoundError("TerminalSession", terminal.id)
            if not _terminal_status_update_is_monotonic(current.status, terminal.status):
                return current
            items[terminal.id] = terminal
            self._write(items)
            return terminal

    def _save_if_status(
        self,
        terminal: TerminalSession,
        expected: set[TerminalStatus],
    ) -> tuple[TerminalSession, bool]:
        with locked_file(self.path):
            items = self._read()
            current = items.get(terminal.id)
            if current is None:
                raise EntityNotFoundError("TerminalSession", terminal.id)
            if current.status not in expected or not _terminal_status_update_is_monotonic(
                current.status,
                terminal.status,
            ):
                return current, False
            items[terminal.id] = terminal
            self._write(items)
            return terminal, True

    def _list_with_statuses(self, statuses: Collection[TerminalStatus]) -> list[TerminalSession]:
        with locked_file(self.path):
            return [item for item in self._read().values() if item.status in statuses]

    def _read(self) -> dict[str, TerminalSession]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Runner terminal state is corrupted: {self.path}") from exc
        if not isinstance(raw, list):
            raise RuntimeError(f"Runner terminal state has an invalid shape: {self.path}")
        terminals = (TerminalSession.model_validate(value) for value in raw)
        return {item.id: item for item in terminals}

    def _write(self, items: dict[str, TerminalSession]) -> None:
        atomic_write_json(
            self.path,
            [item.model_dump(mode="json") for item in items.values()],
        )


def _copy(execution: Execution) -> Execution:
    return Execution.model_validate(execution.model_dump())


def _validate_execution_update(current: Execution, incoming: Execution) -> bool:
    """Validate immutable bindings and report a stale first-write-wins snapshot."""

    if current.created_at != incoming.created_at:
        raise RuntimeError(
            f"execution creation time is immutable for {current.id!r}: "
            f"{current.created_at!r} != {incoming.created_at!r}"
        )
    if current.execution_key != incoming.execution_key:
        raise RuntimeError(
            f"execution key is immutable for {current.id!r}: "
            f"{current.execution_key!r} != {incoming.execution_key!r}"
        )
    for field_name in _EXECUTION_STRICT_ADMISSION_FIELDS:
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if proposed != persisted:
            raise RuntimeError(
                f"execution admission field {field_name!r} is immutable for "
                f"{current.id!r}: {persisted!r} != {proposed!r}"
            )
    if current.owner is not None and incoming.owner != current.owner:
        raise RuntimeError(
            f"execution owner is immutable for {current.id!r}: "
            f"{current.owner.model_dump(mode='json')!r} != "
            f"{incoming.owner.model_dump(mode='json') if incoming.owner is not None else None!r}"
        )
    stale = False
    for field_name in (
        "pid",
        "process_group_id",
        "containment_id",
        "process_created_at",
        "executable_path",
        "tool_id",
        "tool_version",
        "platform_system",
        "platform_release",
        "platform_architecture",
    ):
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if persisted in {None, ""}:
            continue
        if proposed in {None, ""}:
            stale = True
        elif proposed != persisted:
            raise RuntimeError(
                f"execution physical identity field {field_name!r} is immutable "
                f"for {current.id!r}: {persisted!r} != {proposed!r}"
            )
    if current.argv:
        if not incoming.argv:
            stale = True
        elif incoming.argv != current.argv:
            raise RuntimeError(
                f"execution resolved argv is immutable for {current.id!r}: "
                f"{current.argv!r} != {incoming.argv!r}"
            )
    for field_name in ("started_at", "physical_stop_confirmed_at"):
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if persisted is not None and proposed != persisted:
            # Concurrent timestamps are first-write-wins. Returning a CAS miss
            # forces the caller to re-read rather than clearing or replacing
            # durable evidence with a stale snapshot.
            stale = True
    return stale


def _validate_execution_duplicate(current: Execution, incoming: Execution) -> None:
    for field_name in _EXECUTION_DUPLICATE_ADMISSION_FIELDS:
        if (
            field_name == "launch_fingerprint"
            and current.launch_fingerprint is None
            and incoming.launch_fingerprint is not None
        ):
            continue
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if proposed != persisted:
            raise RuntimeError(
                f"execution key {current.execution_key!r} is already bound to "
                f"admission field {field_name!r}={persisted!r}, not {proposed!r}"
            )
    if incoming.owner != current.owner:
        raise RuntimeError(
            f"execution key {current.execution_key!r} is already bound to owner "
            f"{current.owner.model_dump(mode='json') if current.owner is not None else None!r}"
        )
    shell_resolved_argv_replay = (
        current.executor_type is ExecutorType.SHELL and bool(current.argv) and not incoming.argv
    )
    if incoming.argv != current.argv and not shell_resolved_argv_replay:
        raise RuntimeError(
            f"execution key {current.execution_key!r} is already bound to resolved argv "
            f"{current.argv!r}, not {incoming.argv!r}"
        )
    for field_name in ("tool_id", "tool_version"):
        persisted = getattr(current, field_name)
        proposed = getattr(incoming, field_name)
        if proposed != persisted:
            raise RuntimeError(
                f"execution key {current.execution_key!r} is already bound to "
                f"{field_name} {persisted!r}, not {proposed!r}"
            )


_EXECUTION_STRICT_ADMISSION_FIELDS = (
    "launch_fingerprint",
    "run_id",
    "session_id",
    "tool_call_id",
    "attempt_group",
    "node_id",
    "executor_type",
    "command_text",
    "cwd",
    "env_diff",
    "stdout_path",
    "stderr_path",
)
_EXECUTION_DUPLICATE_ADMISSION_FIELDS = tuple(
    field_name
    for field_name in _EXECUTION_STRICT_ADMISSION_FIELDS
    if field_name not in {"stdout_path", "stderr_path"}
)

_EXECUTION_STATUS_TRANSITIONS = {
    ExecutionStatus.QUEUED: frozenset(
        {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.CREATED: frozenset(
        {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.STARTING: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.FAILED: frozenset({ExecutionStatus.CANCELLED}),
    ExecutionStatus.LOST: frozenset({ExecutionStatus.CANCELLED}),
    ExecutionStatus.COMPLETED: frozenset(),
    ExecutionStatus.EXITED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
    ExecutionStatus.HARD_TIMEOUT: frozenset(),
}


def _execution_status_update_is_monotonic(
    current: ExecutionStatus,
    incoming: ExecutionStatus,
) -> bool:
    return incoming is current or incoming in _EXECUTION_STATUS_TRANSITIONS[current]


def _copy_terminal(terminal: TerminalSession) -> TerminalSession:
    return TerminalSession.model_validate(terminal.model_dump())


_TERMINAL_STATUS_TRANSITIONS = {
    TerminalStatus.CREATED: frozenset(
        {TerminalStatus.OPEN, TerminalStatus.CLOSED, TerminalStatus.LOST}
    ),
    TerminalStatus.OPEN: frozenset({TerminalStatus.CLOSED, TerminalStatus.LOST}),
    TerminalStatus.LOST: frozenset({TerminalStatus.CLOSED}),
    TerminalStatus.CLOSED: frozenset(),
}


def _terminal_status_update_is_monotonic(
    current: TerminalStatus,
    incoming: TerminalStatus,
) -> bool:
    return incoming is current or incoming in _TERMINAL_STATUS_TRANSITIONS[current]
