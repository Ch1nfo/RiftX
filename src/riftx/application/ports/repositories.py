"""Repository interfaces consumed by application services."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from riftx.domain import (
    Engagement,
    Execution,
    Run,
    RunEvent,
    RunStatus,
)


class EngagementRepository(Protocol):
    async def create(self, engagement: Engagement) -> Engagement: ...

    async def get(self, engagement_id: str) -> Engagement | None: ...


class RunRepository(Protocol):
    async def create(self, run: Run) -> Run: ...

    async def get(self, run_id: str) -> Run | None: ...

    async def list(
        self,
        *,
        status: RunStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Run]: ...

    async def update_status(self, run_id: str, target: RunStatus) -> Run: ...


class RunEventRepository(Protocol):
    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> RunEvent: ...

    async def list_after(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Sequence[RunEvent]: ...


class ExecutionRepository(Protocol):
    async def create_if_absent(self, execution: Execution) -> tuple[Execution, bool]: ...

    async def get(self, execution_id: str) -> Execution | None: ...

    async def get_by_key(self, execution_key: str) -> Execution | None: ...

    async def save(self, execution: Execution) -> Execution: ...

    async def list_active(self) -> Sequence[Execution]: ...
