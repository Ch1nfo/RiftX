"""Durable Run-event audit sink for Runtime Hook decisions."""

from typing import Protocol

from .models import HookAuditRecord


class RunEventWriter(Protocol):
    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> object: ...


class RunEventHookAuditSink:
    def __init__(self, events: RunEventWriter) -> None:
        self._events = events

    async def record(self, audit: HookAuditRecord) -> None:
        await self._events.append(
            audit.run_id,
            "runtime.hook_evaluated",
            audit.model_dump(mode="json"),
        )
