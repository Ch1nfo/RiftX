"""Ports for durable, non-Run-scoped Code Audit Preflight jobs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from riftx.domain.audit_preflight import (
    AuditPreflightExitReceipt,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightResult,
    AuditPreflightStopReceipt,
    PreflightRequest,
)


@dataclass(frozen=True, slots=True)
class AuditPreflightOwnerBinding:
    """Bounded owner columns safe to load before restricted request/result data."""

    job_id: str
    operator_principal_id: str
    authorization_scope_digest: str
    request_schema_version: str
    request_digest: str
    source_node_id: str
    source_root_identity_digest: str
    backend_id: str
    image_digest: str
    policy_digest: str
    status: AuditPreflightJobStatus
    state_version: int
    effect_owner_digest: str


@dataclass(frozen=True, slots=True)
class AuditPreflightReconciliationCandidate:
    """Bounded mutable facts safe for expiry reconciliation.

    This projection deliberately excludes the restricted request payload and
    every source path.  ``state_version`` remains the sole CAS token; the
    remaining fields are only the facts needed to prove an expiry transition
    and return an idempotent bounded result.
    """

    job_id: str
    status: AuditPreflightJobStatus
    state_version: int
    effect_owner_digest: str
    expires_at: datetime
    lease_expires_at: datetime | None
    updated_at: datetime
    never_created_proof_digest: str | None
    stop_receipt_digest: str | None


@dataclass(frozen=True, slots=True)
class AuditPreflightDispatch:
    """Restricted dispatch material returned only after Runner owner admission."""

    job: AuditPreflightJob
    request: PreflightRequest


class AuditPreflightRepository(Protocol):
    async def create(
        self,
        job: AuditPreflightJob,
    ) -> tuple[AuditPreflightJob, bool]: ...

    async def get_idempotency_binding(
        self,
        *,
        operator_principal_id: str,
        client_request_id: str,
    ) -> AuditPreflightOwnerBinding | None: ...

    async def get_owner_binding(
        self,
        job_id: str,
    ) -> AuditPreflightOwnerBinding | None: ...

    async def get(self, job_id: str) -> AuditPreflightJob | None: ...

    async def get_reconciliation_candidate(
        self,
        job_id: str,
    ) -> AuditPreflightReconciliationCandidate | None: ...

    async def get_replayable_claim(
        self,
        *,
        node_id: str,
        runner_instance_id: str,
        runner_epoch: int,
        now: datetime,
    ) -> AuditPreflightDispatch | None: ...

    async def claim_next(
        self,
        *,
        node_id: str,
        runner_instance_id: str,
        runner_epoch: int,
        now: datetime,
        lease_expires_at: datetime,
        output_contract_digest: str,
    ) -> AuditPreflightDispatch | None: ...

    async def compare_and_set(
        self,
        *,
        previous: AuditPreflightJob,
        updated: AuditPreflightJob,
        result: AuditPreflightResult | None = None,
        exit_receipt: AuditPreflightExitReceipt | None = None,
        stop_receipt: AuditPreflightStopReceipt | None = None,
    ) -> AuditPreflightJob: ...

    async def compare_and_set_reconciliation(
        self,
        *,
        previous: AuditPreflightReconciliationCandidate,
        status: AuditPreflightJobStatus,
        observed_at: datetime,
        never_created_proof_digest: str | None = None,
    ) -> AuditPreflightReconciliationCandidate: ...

    async def list_reconciliation_candidates(
        self,
        *,
        observed_at: datetime,
        limit: int,
    ) -> Sequence[AuditPreflightReconciliationCandidate]: ...


__all__ = [
    "AuditPreflightDispatch",
    "AuditPreflightOwnerBinding",
    "AuditPreflightReconciliationCandidate",
    "AuditPreflightRepository",
]
