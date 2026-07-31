"""Execution runner contracts shared by local and remote supervisors."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from riftx.domain import Execution

from .models import ExecutionLaunchRequest, ExecutionOutput

EffectGuard = Callable[[], Awaitable[None]]


class ExecutionRunner(Protocol):
    """Common process execution interface consumed by Skills and Activities."""

    async def start(
        self,
        request: ExecutionLaunchRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> Execution: ...

    async def get(self, execution_id: str) -> Execution: ...

    async def wait(self, execution_id: str) -> Execution: ...

    async def cancel(self, execution_id: str) -> Execution: ...

    async def read_output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ExecutionOutput: ...
