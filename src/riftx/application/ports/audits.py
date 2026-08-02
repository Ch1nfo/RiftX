"""Persistence ports for RiftX Code Audit facts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from riftx.domain import (
    AuditContractRecord,
    AuditLifecycleStatus,
    AuditPhase,
    AuditPhaseRun,
    AuditPhaseRunStatus,
    AuditProject,
    AuditScan,
    AuditScopeKind,
    AuditScopeStatus,
    AuditScopeUnit,
    AuditStartIntent,
    AuditWorkItem,
    AuditWorkStatus,
    SourceSnapshot,
)


@dataclass(frozen=True, slots=True)
class StoredAuditEntity[T]:
    """A validated Audit fact paired with its monotonic persistence CAS token."""

    value: T
    state_version: int

    def __post_init__(self) -> None:
        if self.state_version < 1:
            raise ValueError("Audit persistence state_version must be at least 1")


class AuditProjectRepository(Protocol):
    async def create(
        self,
        project: AuditProject,
    ) -> tuple[StoredAuditEntity[AuditProject], bool]: ...

    async def get(self, project_id: str) -> StoredAuditEntity[AuditProject] | None: ...

    async def get_by_identity(
        self,
        repository_identity_digest: str,
        *,
        engagement_id: str,
    ) -> StoredAuditEntity[AuditProject] | None: ...

    async def list(
        self,
        *,
        engagement_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditProject]]: ...

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditProject],
        replacement: AuditProject,
    ) -> tuple[StoredAuditEntity[AuditProject], bool]: ...


class SnapshotRepository(Protocol):
    async def create(self, snapshot: SourceSnapshot) -> tuple[SourceSnapshot, bool]: ...

    async def get(self, project_id: str, snapshot_id: str) -> SourceSnapshot | None: ...

    async def get_by_digest(
        self,
        project_id: str,
        snapshot_digest: str,
    ) -> SourceSnapshot | None: ...

    async def list(
        self,
        project_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[SourceSnapshot]: ...


class AuditContractRepository(Protocol):
    """Read/seal access; contracts can only be created with their AuditScan."""

    async def get(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
    ) -> StoredAuditEntity[AuditContractRecord] | None: ...

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditContractRecord],
        replacement: AuditContractRecord,
    ) -> tuple[StoredAuditEntity[AuditContractRecord], bool]: ...


class AuditRepository(Protocol):
    async def create(
        self,
        scan: AuditScan,
        contract: AuditContractRecord,
    ) -> tuple[StoredAuditEntity[AuditScan], bool]: ...

    async def get(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
    ) -> StoredAuditEntity[AuditScan] | None: ...

    async def get_contract(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
    ) -> AuditContractRecord | None: ...

    async def list(
        self,
        *,
        project_id: str | None = None,
        lifecycle_status: AuditLifecycleStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditScan]]: ...

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditScan],
        replacement: AuditScan,
    ) -> tuple[StoredAuditEntity[AuditScan], bool]: ...


class AuditStartIntentRepository(Protocol):
    async def create(
        self,
        intent: AuditStartIntent,
    ) -> tuple[StoredAuditEntity[AuditStartIntent], bool]: ...

    async def get(
        self,
        audit_id: str,
        intent_id: str,
    ) -> StoredAuditEntity[AuditStartIntent] | None: ...

    async def get_for_audit(
        self,
        audit_id: str,
    ) -> StoredAuditEntity[AuditStartIntent] | None: ...

    async def list_ready(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> Sequence[StoredAuditEntity[AuditStartIntent]]: ...

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditStartIntent],
        replacement: AuditStartIntent,
    ) -> tuple[StoredAuditEntity[AuditStartIntent], bool]: ...


class AuditPhaseRepository(Protocol):
    async def create(
        self,
        phase_run: AuditPhaseRun,
    ) -> tuple[StoredAuditEntity[AuditPhaseRun], bool]: ...

    async def get(
        self,
        audit_id: str,
        phase_run_id: str,
    ) -> StoredAuditEntity[AuditPhaseRun] | None: ...

    async def list(
        self,
        audit_id: str,
        *,
        phase: AuditPhase | None = None,
        status: AuditPhaseRunStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditPhaseRun]]: ...

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditPhaseRun],
        replacement: AuditPhaseRun,
    ) -> tuple[StoredAuditEntity[AuditPhaseRun], bool]: ...


class AuditScopeRepository(Protocol):
    async def create(
        self,
        scope_unit: AuditScopeUnit,
    ) -> tuple[StoredAuditEntity[AuditScopeUnit], bool]: ...

    async def get(
        self,
        audit_id: str,
        scope_unit_id: str,
    ) -> StoredAuditEntity[AuditScopeUnit] | None: ...

    async def list(
        self,
        audit_id: str,
        *,
        kind: AuditScopeKind | None = None,
        status: AuditScopeStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditScopeUnit]]: ...

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditScopeUnit],
        replacement: AuditScopeUnit,
    ) -> tuple[StoredAuditEntity[AuditScopeUnit], bool]: ...


class AuditWorkRepository(Protocol):
    async def create(
        self,
        work_item: AuditWorkItem,
    ) -> tuple[StoredAuditEntity[AuditWorkItem], bool]: ...

    async def get(
        self,
        audit_id: str,
        work_item_id: str,
    ) -> StoredAuditEntity[AuditWorkItem] | None: ...

    async def list(
        self,
        audit_id: str,
        *,
        phase: AuditPhase | None = None,
        status: AuditWorkStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditWorkItem]]: ...

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditWorkItem],
        replacement: AuditWorkItem,
    ) -> tuple[StoredAuditEntity[AuditWorkItem], bool]: ...
