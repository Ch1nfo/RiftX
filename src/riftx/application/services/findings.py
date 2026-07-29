"""Application service for reading findings attached to a Run."""

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import FindingRepository, RunRepository
from riftx.domain import Finding, FindingSeverity, FindingStatus


class FindingApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        finding_repository: FindingRepository,
    ) -> None:
        self._run_repository = run_repository
        self._finding_repository = finding_repository

    async def list_findings(
        self,
        run_id: str,
        *,
        severity: FindingSeverity | None = None,
        status: FindingStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Finding]:
        if await self._run_repository.get(run_id) is None:
            raise EntityNotFoundError("Run", run_id)
        return list(
            await self._finding_repository.list(
                run_id,
                severity=severity,
                status=status,
                limit=limit,
                offset=offset,
            )
        )
