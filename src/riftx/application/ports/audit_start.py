"""Fail-closed Start admission contracts for RiftX Code Audit."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol
from uuid import UUID

from riftx.domain import (
    AuditLifecycleStatus,
    AuditStartIntent,
    AuditStartIntentStatus,
    LocalPrincipal,
    OperatorCapability,
    RunStatus,
)
from riftx.domain.audit_contract_v2 import AuditContractRecordV2
from riftx.domain.audit_preflight_plan import (
    AuditPreflightPlan,
    AuditPreflightPlanStatus,
)

from .audits import AuditAggregate, AuditObjectAuthorizer

AUDIT_START_REVALIDATION_REQUEST_SCHEMA_VERSION = (
    "riftx.audit-start-revalidation-request/v1"
)
AUDIT_START_REVALIDATION_PROOF_SCHEMA_VERSION = (
    "riftx.audit-start-revalidation-proof/v1"
)

_DIGEST_CHARACTERS = frozenset("0123456789abcdef")
_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@+~-"
)
_VERSION_CHARACTERS = _ID_CHARACTERS | frozenset("/")
_MAX_REVALIDATION_LIFETIME = timedelta(minutes=5)


def _require_safe_id(value: str, *, label: str, maximum: int = 128) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(character not in _ID_CHARACTERS for character in value)
    ):
        raise ValueError(f"{label} is invalid")


def _require_digest(value: str, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST_CHARACTERS for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _require_version(value: str, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(character not in _VERSION_CHARACTERS for character in value)
    ):
        raise ValueError(f"{label} is invalid")


def _require_uuid(value: str, *, label: str) -> None:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        parsed = None
    if parsed is None or parsed.int == 0 or str(parsed) != value:
        raise ValueError(f"{label} must be a canonical non-zero UUID")


def _require_aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _canonical_digest(schema_version: str, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(schema_version.encode("ascii") + b"\0" + canonical).hexdigest()


class AuditStartRevalidationDisposition(StrEnum):
    MATCHED = "matched"
    CHANGED = "changed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AuditStartRevalidationRequest:
    """Short-lived, owner-bound request sent to a future same-node source adapter."""

    audit_id: str
    run_id: str
    start_request_id: str
    preflight_plan_id: str
    preflight_plan_digest: str
    contract_digest: str
    security_context_id: str
    security_context_digest: str
    operator_principal_id: str
    authorization_scope_digest: str
    source_node_id: str
    source_root_identity_digest: str
    repository_identity_digest: str
    expected_content_identity_digest: str
    source_ingest_backend_id: str
    source_ingest_image_digest: str
    source_ingest_policy_digest: str
    source_repository_path: str = field(repr=False)
    requested_at: datetime
    expires_at: datetime
    schema_version: str = field(
        default=AUDIT_START_REVALIDATION_REQUEST_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        for label, value in (
            ("audit_id", self.audit_id),
            ("run_id", self.run_id),
            ("preflight_plan_id", self.preflight_plan_id),
            ("operator_principal_id", self.operator_principal_id),
            ("source_node_id", self.source_node_id),
            ("source_ingest_backend_id", self.source_ingest_backend_id),
        ):
            _require_safe_id(value, label=label)
        _require_version(self.security_context_id, label="security_context_id")
        _require_uuid(self.start_request_id, label="start_request_id")
        for label, value in (
            ("preflight_plan_digest", self.preflight_plan_digest),
            ("contract_digest", self.contract_digest),
            ("security_context_digest", self.security_context_digest),
            ("authorization_scope_digest", self.authorization_scope_digest),
            ("source_root_identity_digest", self.source_root_identity_digest),
            ("repository_identity_digest", self.repository_identity_digest),
            ("expected_content_identity_digest", self.expected_content_identity_digest),
            ("source_ingest_image_digest", self.source_ingest_image_digest),
            ("source_ingest_policy_digest", self.source_ingest_policy_digest),
        ):
            _require_digest(value, label=label)
        source_path = (
            PurePosixPath(self.source_repository_path)
            if isinstance(self.source_repository_path, str)
            else None
        )
        if (
            not isinstance(self.source_repository_path, str)
            or not self.source_repository_path.startswith("/")
            or "\x00" in self.source_repository_path
            or len(self.source_repository_path.encode("utf-8")) > 4096
            or source_path is None
            or ".." in source_path.parts
            or str(source_path) != self.source_repository_path
        ):
            raise ValueError("source_repository_path must be a normalized bounded absolute path")
        _require_aware(self.requested_at, label="requested_at")
        _require_aware(self.expires_at, label="expires_at")
        if not self.requested_at < self.expires_at <= (
            self.requested_at + _MAX_REVALIDATION_LIFETIME
        ):
            raise ValueError("Start revalidation request lifetime is invalid")

    @property
    def source_repository_path_digest(self) -> str:
        return _canonical_digest(
            "riftx.audit-start-source-path/v1",
            {"source_repository_path": self.source_repository_path},
        )

    @property
    def request_digest(self) -> str:
        return _canonical_digest(
            self.schema_version,
            {
                "schema_version": self.schema_version,
                "audit_id": self.audit_id,
                "run_id": self.run_id,
                "start_request_id": self.start_request_id,
                "preflight_plan_id": self.preflight_plan_id,
                "preflight_plan_digest": self.preflight_plan_digest,
                "contract_digest": self.contract_digest,
                "security_context_id": self.security_context_id,
                "security_context_digest": self.security_context_digest,
                "operator_principal_id": self.operator_principal_id,
                "authorization_scope_digest": self.authorization_scope_digest,
                "source_node_id": self.source_node_id,
                "source_root_identity_digest": self.source_root_identity_digest,
                "repository_identity_digest": self.repository_identity_digest,
                "expected_content_identity_digest": self.expected_content_identity_digest,
                "source_ingest_backend_id": self.source_ingest_backend_id,
                "source_ingest_image_digest": self.source_ingest_image_digest,
                "source_ingest_policy_digest": self.source_ingest_policy_digest,
                "source_repository_path_digest": self.source_repository_path_digest,
                "requested_at": self.requested_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
            },
        )


@dataclass(frozen=True, slots=True)
class AuditStartRevalidationProof:
    """Bounded proof returned before opening the atomic Start transaction."""

    request_digest: str
    disposition: AuditStartRevalidationDisposition
    reason_code: str
    observed_content_identity_digest: str | None
    proof_digest: str | None
    issued_at: datetime
    expires_at: datetime
    schema_version: str = field(
        default=AUDIT_START_REVALIDATION_PROOF_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        _require_digest(self.request_digest, label="request_digest")
        _require_safe_id(self.reason_code, label="reason_code")
        _require_aware(self.issued_at, label="issued_at")
        _require_aware(self.expires_at, label="expires_at")
        if not self.issued_at < self.expires_at <= self.issued_at + _MAX_REVALIDATION_LIFETIME:
            raise ValueError("Start revalidation proof lifetime is invalid")
        if self.disposition is AuditStartRevalidationDisposition.UNAVAILABLE:
            if self.observed_content_identity_digest is not None or self.proof_digest is not None:
                raise ValueError("unavailable Start revalidation cannot carry observed proof")
            return
        if self.observed_content_identity_digest is None or self.proof_digest is None:
            raise ValueError("Start revalidation result requires content and proof digests")
        _require_digest(
            self.observed_content_identity_digest,
            label="observed_content_identity_digest",
        )
        _require_digest(self.proof_digest, label="proof_digest")

    def accepts(self, request: AuditStartRevalidationRequest, *, at: datetime) -> bool:
        _require_aware(at, label="acceptance time")
        if self.disposition is not AuditStartRevalidationDisposition.MATCHED:
            return False
        if not self.issued_at <= at < self.expires_at or at >= request.expires_at:
            return False
        assert self.observed_content_identity_digest is not None
        return hmac.compare_digest(
            self.request_digest,
            request.request_digest,
        ) and hmac.compare_digest(
            self.observed_content_identity_digest,
            request.expected_content_identity_digest,
        )


class AuditAuthorizedAggregateReader(Protocol):
    async def get_authorized(
        self,
        audit_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
        capability: OperatorCapability = OperatorCapability.READ,
    ) -> AuditAggregate: ...


class AuditStartRevalidationPort(Protocol):
    async def revalidate(
        self,
        request: AuditStartRevalidationRequest,
    ) -> AuditStartRevalidationProof: ...


@dataclass(frozen=True, slots=True)
class AuditStartAdmissionRequest:
    """Future atomic consume/queue/Intent request; AUD-201 never constructs it."""

    aggregate: AuditAggregate
    plan: AuditPreflightPlan
    start_request_id: str
    reviewed_contract_digest: str
    expected_audit_state_version: int
    expected_plan_state_version: int
    revalidation_request: AuditStartRevalidationRequest
    revalidation_proof: AuditStartRevalidationProof
    intent: AuditStartIntent
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.start_request_id, label="start_request_id")
        _require_digest(self.reviewed_contract_digest, label="reviewed_contract_digest")
        _require_aware(self.occurred_at, label="occurred_at")
        scan = self.aggregate.audit.value
        contract = self.aggregate.contract.value
        contract_v2 = (
            contract.contract() if isinstance(contract, AuditContractRecordV2) else None
        )
        source_binding = contract_v2.source_binding if contract_v2 is not None else None
        contract_is_start_ready = (
            contract_v2 is not None and contract_v2.start_eligible
        )
        if (
            self.expected_audit_state_version != self.aggregate.audit.state_version
            or self.expected_plan_state_version != self.plan.state_version
            or scan.lifecycle_status is not AuditLifecycleStatus.DRAFT
            or self.aggregate.run.status is not RunStatus.CREATED
            or self.plan.status is not AuditPreflightPlanStatus.RESERVED
            or self.plan.reserved_audit_id != scan.id
            or not self.plan.created_at <= self.occurred_at < self.plan.expires_at
            or contract.contract_digest != self.reviewed_contract_digest
            or not contract_is_start_ready
            or contract.preflight_plan_id != self.plan.plan_id
            or not hmac.compare_digest(
                contract.preflight_plan_digest,
                self.plan.plan_digest,
            )
            or contract_v2 is None
            or contract_v2.preflight_plan_id != self.plan.plan_id
            or not hmac.compare_digest(
                contract_v2.preflight_plan_digest,
                self.plan.plan_digest,
            )
            or contract_v2.operator_principal_id != self.plan.operator_principal_id
            or source_binding is None
            or self.revalidation_request.audit_id != scan.id
            or self.revalidation_request.run_id != self.aggregate.run.id
            or self.revalidation_request.start_request_id != self.start_request_id
            or self.revalidation_request.preflight_plan_id != self.plan.plan_id
            or self.revalidation_request.preflight_plan_digest != self.plan.plan_digest
            or self.revalidation_request.contract_digest != contract.contract_digest
            or self.revalidation_request.security_context_id
            != self.plan.security_context_id
            or not hmac.compare_digest(
                self.revalidation_request.security_context_digest,
                self.plan.security_context_digest,
            )
            or self.revalidation_request.operator_principal_id
            != self.plan.operator_principal_id
            or not hmac.compare_digest(
                self.revalidation_request.authorization_scope_digest,
                self.plan.authorization_scope_digest,
            )
            or self.revalidation_request.source_node_id != self.plan.source_node_id
            or not hmac.compare_digest(
                self.revalidation_request.source_root_identity_digest,
                self.plan.source_root_identity_digest,
            )
            or not hmac.compare_digest(
                self.revalidation_request.repository_identity_digest,
                self.plan.repository_identity_digest,
            )
            or not hmac.compare_digest(
                self.revalidation_request.expected_content_identity_digest,
                self.plan.content_identity_digest,
            )
            or self.revalidation_request.source_ingest_backend_id
            != self.plan.backend_id
            or not hmac.compare_digest(
                self.revalidation_request.source_ingest_image_digest,
                self.plan.image_digest,
            )
            or not hmac.compare_digest(
                self.revalidation_request.source_ingest_policy_digest,
                self.plan.policy_digest,
            )
            or self.revalidation_request.source_repository_path
            != self.plan.target.repository_path
            or self.revalidation_request.security_context_id
            != contract_v2.security_context_bundle_id
            or not hmac.compare_digest(
                self.revalidation_request.security_context_digest,
                contract_v2.security_context_bundle_digest,
            )
            or self.revalidation_request.operator_principal_id
            != contract_v2.operator_principal_id
            or not hmac.compare_digest(
                self.revalidation_request.authorization_scope_digest,
                contract_v2.security_context_binding.authorization_scope_digest,
            )
            or self.revalidation_request.source_node_id != source_binding.source_node_id
            or not hmac.compare_digest(
                self.revalidation_request.source_root_identity_digest,
                source_binding.source_root_identity_digest,
            )
            or not hmac.compare_digest(
                self.revalidation_request.repository_identity_digest,
                source_binding.repository_identity_digest,
            )
            or not hmac.compare_digest(
                self.revalidation_request.expected_content_identity_digest,
                source_binding.content_identity_digest,
            )
            or self.revalidation_request.source_ingest_backend_id
            != source_binding.source_ingest_backend_id
            or not hmac.compare_digest(
                self.revalidation_request.source_ingest_image_digest,
                source_binding.source_ingest_image_digest,
            )
            or not hmac.compare_digest(
                self.revalidation_request.source_ingest_policy_digest,
                source_binding.source_ingest_policy_digest,
            )
            or self.revalidation_request.source_repository_path
            != contract_v2.source_target.repository_path
            or not self.revalidation_request.requested_at
            <= self.revalidation_proof.issued_at
            or self.revalidation_proof.expires_at
            > self.revalidation_request.expires_at
            or not self.revalidation_proof.accepts(
                self.revalidation_request,
                at=self.occurred_at,
            )
            or self.intent.audit_id != scan.id
            or self.intent.run_id != self.aggregate.run.id
            or self.intent.start_request_id != self.start_request_id
            or self.intent.contract_digest != contract.contract_digest
            or self.intent.status is not AuditStartIntentStatus.PENDING
            or self.intent.created_at != self.occurred_at
            or self.intent.updated_at != self.occurred_at
        ):
            raise ValueError("Start admission request has inconsistent authoritative bindings")


@dataclass(frozen=True, slots=True)
class AuditStartAdmissionProjection:
    aggregate: AuditAggregate
    plan: AuditPreflightPlan
    intent: AuditStartIntent
    created: bool

    def __post_init__(self) -> None:
        scan = self.aggregate.audit.value
        contract = self.aggregate.contract.value
        contract_v2 = (
            contract.contract() if isinstance(contract, AuditContractRecordV2) else None
        )
        if (
            scan.lifecycle_status is not AuditLifecycleStatus.QUEUED
            or self.aggregate.run.status is not RunStatus.PREPARING
            or self.plan.status is not AuditPreflightPlanStatus.CONSUMED
            or self.plan.consumed_audit_id != scan.id
            or self.plan.consumed_start_request_id != self.intent.start_request_id
            or self.intent.audit_id != scan.id
            or self.intent.run_id != self.aggregate.run.id
            or self.intent.status is not AuditStartIntentStatus.PENDING
            or self.intent.contract_digest != contract.contract_digest
            or contract_v2 is None
            or not contract_v2.start_eligible
            or contract.preflight_plan_id != self.plan.plan_id
            or contract_v2.preflight_plan_id != self.plan.plan_id
            or not hmac.compare_digest(
                contract.preflight_plan_digest,
                self.plan.plan_digest,
            )
            or not hmac.compare_digest(
                contract_v2.preflight_plan_digest,
                self.plan.plan_digest,
            )
        ):
            raise ValueError("Start admission projection is not atomically converged")

    @property
    def replayed(self) -> bool:
        return not self.created


class AuditStartAdmissionUnitOfWork(Protocol):
    async def admit(
        self,
        request: AuditStartAdmissionRequest,
    ) -> AuditStartAdmissionProjection: ...


__all__ = [
    "AUDIT_START_REVALIDATION_PROOF_SCHEMA_VERSION",
    "AUDIT_START_REVALIDATION_REQUEST_SCHEMA_VERSION",
    "AuditAuthorizedAggregateReader",
    "AuditStartAdmissionProjection",
    "AuditStartAdmissionRequest",
    "AuditStartAdmissionUnitOfWork",
    "AuditStartRevalidationDisposition",
    "AuditStartRevalidationPort",
    "AuditStartRevalidationProof",
    "AuditStartRevalidationRequest",
]
