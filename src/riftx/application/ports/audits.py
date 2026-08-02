"""Persistence ports for RiftX Code Audit facts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from riftx.domain import (
    AuditClientRequest,
    AuditContractRecord,
    AuditLifecycleStatus,
    AuditMode,
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
    Engagement,
    Run,
    RunEvent,
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


@dataclass(frozen=True, slots=True)
class AuditAggregate:
    """One structurally validated Audit read captured in a single DB session."""

    audit: StoredAuditEntity[AuditScan]
    contract: StoredAuditEntity[AuditContractRecord]
    project: StoredAuditEntity[AuditProject]
    run: Run
    engagement: Engagement
    client_request: AuditClientRequest

    def __post_init__(self) -> None:
        scan = self.audit.value
        contract = self.contract.value
        project = self.project.value
        if (
            scan.run_id != self.run.id
            or scan.project_id != project.id
            or scan.contract_id != contract.contract_id
            or contract.audit_id != scan.id
            or contract.contract_digest != scan.contract_digest
            or project.engagement_id != self.engagement.id
            or self.run.engagement_id != self.engagement.id
            or self.run.node_id != scan.selected_node_id
            or self.run.model_profile != scan.model_profile
            or self.client_request.audit_id != scan.id
            or self.client_request.run_id != self.run.id
            or self.client_request.project_id != project.id
            or self.client_request.engagement_id != self.engagement.id
            or self.client_request.contract_id != contract.contract_id
            or self.client_request.contract_digest != contract.contract_digest
            or self.client_request.temporal_workflow_id != scan.temporal_workflow_id
        ):
            raise ValueError("Audit aggregate ownership binding is inconsistent")


@dataclass(frozen=True, slots=True)
class AuditDraftCreationEnvelope:
    """Fully materialized draft facts written by one aggregate transaction."""

    engagement: Engagement
    project: AuditProject
    run: Run
    run_created_event: RunEvent
    audit: AuditScan
    contract: AuditContractRecord
    audit_created_event: RunEvent
    client_request: AuditClientRequest

    def __post_init__(self) -> None:
        if (
            self.project.engagement_id != self.engagement.id
            or self.run.engagement_id != self.engagement.id
            or self.audit.project_id != self.project.id
            or self.audit.run_id != self.run.id
            or self.audit.contract_id != self.contract.contract_id
            or self.contract.audit_id != self.audit.id
            or self.client_request.audit_id != self.audit.id
            or self.client_request.run_id != self.run.id
            or self.client_request.project_id != self.project.id
            or self.client_request.engagement_id != self.engagement.id
            or self.client_request.contract_id != self.contract.contract_id
            or self.client_request.contract_digest != self.contract.contract_digest
            or self.client_request.temporal_workflow_id != self.audit.temporal_workflow_id
        ):
            raise ValueError("Audit draft creation envelope has inconsistent ownership")
        if (
            self.run_created_event.run_id != self.run.id
            or self.run_created_event.sequence != 1
            or self.run_created_event.event_type != "run.created"
            or self.audit_created_event.run_id != self.run.id
            or self.audit_created_event.sequence != 2
            or self.audit_created_event.event_type != "audit.created"
        ):
            raise ValueError("Audit draft creation events have an invalid binding or order")


class AuditDraftAggregateFactory(Protocol):
    """Pure application factory invoked after the UoW resolves the real Project."""

    @property
    def client_request_id(self) -> str: ...

    @property
    def request_digest(self) -> str: ...

    @property
    def repository_identity_digest(self) -> str: ...

    @property
    def requested_engagement_id(self) -> str | None: ...

    @property
    def authorization_reference(self) -> str: ...

    @property
    def workspace_root(self) -> str: ...

    @property
    def source_repository_path(self) -> str: ...

    def build_engagement(self) -> Engagement: ...

    def build_project(self, engagement: Engagement) -> AuditProject: ...

    def build(
        self,
        project: AuditProject,
        engagement: Engagement,
    ) -> AuditDraftCreationEnvelope: ...


class AuditCreationUnitOfWork(Protocol):
    async def create_draft(
        self,
        factory: AuditDraftAggregateFactory,
    ) -> tuple[AuditAggregate, bool]: ...


class AuditAggregateReadRepository(Protocol):
    async def get(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
        engagement_id: str | None = None,
    ) -> AuditAggregate | None: ...

    async def list(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        engagement_id: str | None = None,
        lifecycle_status: AuditLifecycleStatus | None = None,
        mode: AuditMode | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditAggregate]: ...


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
