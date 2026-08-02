"""Persistence port for connector submission idempotency."""

from typing import Protocol

from .models import ConnectorSource, ConnectorSubmission


class ConnectorSubmissionRepository(Protocol):
    async def get(
        self, source: ConnectorSource, capture_id: str
    ) -> ConnectorSubmission | None: ...

    async def create(self, item: ConnectorSubmission) -> ConnectorSubmission: ...
