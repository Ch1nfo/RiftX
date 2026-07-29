"""Runner-local execution metadata storage without Control Plane foreign keys."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from riftx.application.errors import EntityNotFoundError
from riftx.domain import Execution, ExecutionStatus

_ACTIVE_STATUSES = {
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


def _copy(execution: Execution) -> Execution:
    return Execution.model_validate(execution.model_dump())
