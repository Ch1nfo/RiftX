"""Application service for the durable Run event timeline."""

from collections.abc import Collection
from typing import Protocol

from riftx.application.errors import EntityNotFoundError
from riftx.application.event_projection import (
    redact_sensitive_event,
    target_http_artifact_candidates,
)
from riftx.application.ports import RunEventRepository, RunRepository
from riftx.domain import RunEvent


class _TargetHttpArtifactAssociationReader(Protocol):
    async def target_http_sensitive_ids(
        self,
        artifact_ids: Collection[str],
    ) -> frozenset[str]: ...


class EventApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        event_repository: RunEventRepository,
        artifact_associations: _TargetHttpArtifactAssociationReader,
    ) -> None:
        self._run_repository = run_repository
        self._event_repository = event_repository
        self._artifact_associations = artifact_associations

    async def require_run(self, run_id: str) -> None:
        if await self._run_repository.get(run_id) is None:
            raise EntityNotFoundError("Run", run_id)

    async def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        require_run: bool = True,
    ) -> list[RunEvent]:
        if require_run:
            await self.require_run(run_id)
        events = list(
            await self._event_repository.list_after(
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        )
        artifact_ids = target_http_artifact_candidates(events)
        sensitive_artifact_ids = await self._artifact_associations.target_http_sensitive_ids(
            artifact_ids
        )
        return [
            redact_sensitive_event(
                event,
                sensitive_artifact_ids=sensitive_artifact_ids,
            )
            for event in events
        ]
