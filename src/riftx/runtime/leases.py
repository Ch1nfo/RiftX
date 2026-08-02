"""Database-backed Run Lease lifecycle."""

from __future__ import annotations

from datetime import timedelta

from riftx.domain.base import utc_now
from riftx.persistence.runtime_repositories import SQLAlchemyRunLeaseRepository
from riftx.runtime.types import RunLease


class DatabaseRunLease:
    def __init__(self, repository: SQLAlchemyRunLeaseRepository, lease: RunLease) -> None:
        self._repository = repository
        self.lease = lease
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        await self._repository.release(
            self.lease.run_id,
            owner_id=self.lease.owner_id,
            expected_version=self.lease.version,
        )
        self._released = True


class DatabaseRunLeaseManager:
    def __init__(
        self,
        repository: SQLAlchemyRunLeaseRepository,
        *,
        ttl_seconds: float = 60,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._repository = repository
        self._ttl_seconds = ttl_seconds

    async def acquire(self, run_id: str, owner_id: str) -> DatabaseRunLease:
        now = utc_now()
        lease = RunLease(
            run_id=run_id,
            owner_id=owner_id,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        await self._repository.acquire(lease)
        return DatabaseRunLease(self._repository, lease)
