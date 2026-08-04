"""Ports for durable Code Audit Preflight authorization plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from riftx.domain.audit_preflight_plan import (
    AuditPreflightPlan,
    AuditPreflightPlanStatus,
    AuditPreflightTokenVerifier,
)


@dataclass(frozen=True, slots=True)
class AuditPreflightPlanOwnerBinding:
    """Bounded Plan ownership facts safe to load before restricted Plan JSON."""

    plan_id: str
    preflight_job_id: str
    operator_principal_id: str
    authorization_scope_digest: str
    plan_digest: str
    status: AuditPreflightPlanStatus
    state_version: int
    expires_at: datetime
    reserved_audit_id: str | None
    reserved_client_request_id: str | None
    consumed_audit_id: str | None


@dataclass(frozen=True, slots=True)
class AuditPreflightPlanTokenBinding:
    """Bounded verifier/owner projection for an opaque token-hash lookup.

    The projection deliberately excludes canonical Plan JSON, repository paths,
    target and Scope bodies, and every non-verifier secret.  It contains only
    the facts required to authenticate a token, authorize its owner, and choose
    between a new admission and an exact replay path.
    """

    plan_id: str
    preflight_job_id: str
    operator_principal_id: str
    authorization_scope_digest: str
    plan_digest: str
    token_verifier: AuditPreflightTokenVerifier
    status: AuditPreflightPlanStatus
    state_version: int
    expires_at: datetime
    reserved_audit_id: str | None
    reserved_client_request_id: str | None
    consumed_audit_id: str | None
    consumed_start_request_id: str | None


class AuditPreflightPlanRepository(Protocol):
    async def create(
        self,
        plan: AuditPreflightPlan,
    ) -> tuple[AuditPreflightPlan, bool]: ...

    async def get_owner_binding(
        self,
        plan_id: str,
    ) -> AuditPreflightPlanOwnerBinding | None: ...

    async def get_owner_binding_for_job(
        self,
        preflight_job_id: str,
    ) -> AuditPreflightPlanOwnerBinding | None: ...

    async def get_token_binding(
        self,
        token_hash: str,
    ) -> AuditPreflightPlanTokenBinding | None: ...

    async def get(self, plan_id: str) -> AuditPreflightPlan | None: ...

    async def compare_and_set(
        self,
        *,
        previous: AuditPreflightPlan,
        updated: AuditPreflightPlan,
    ) -> AuditPreflightPlan: ...


__all__ = [
    "AuditPreflightPlanOwnerBinding",
    "AuditPreflightPlanRepository",
    "AuditPreflightPlanTokenBinding",
]
