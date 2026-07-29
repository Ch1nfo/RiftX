"""Application service for the durable Run event timeline."""

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import RunEventRepository, RunRepository
from riftx.domain import RunEvent


class EventApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        event_repository: RunEventRepository,
    ) -> None:
        self._run_repository = run_repository
        self._event_repository = event_repository

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
        return list(
            await self._event_repository.list_after(
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        )
