"""Versioned wire contracts for the dedicated Audit Preflight Runner channel.

These models intentionally carry no Run, Audit, Snapshot, plan, or token identity.
They are shared by the Control Plane and outbound Runner so neither side needs to
import the other's API or implementation modules.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .audit_preflight import (
    AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    AuditPreflightEffectOwner,
    AuditPreflightJobStatus,
    AuditPreflightLeaseEnvelope,
    AuditPreflightStrictModel,
    PreflightDigest,
    PreflightId,
    PreflightRequest,
)

AUDIT_PREFLIGHT_DISPATCH_SCHEMA_VERSION: Literal["riftx.audit-preflight-dispatch/v1"] = (
    "riftx.audit-preflight-dispatch/v1"
)
AUDIT_PREFLIGHT_OUTPUT_CONTRACT_SCHEMA_VERSION: Literal[
    "riftx.audit-preflight-output-contract/v1"
] = "riftx.audit-preflight-output-contract/v1"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


_OUTPUT_CONTRACT = {
    "allowed_outcomes": ["failed", "rejected", "succeeded"],
    "exit_receipt_schema": "riftx.audit-preflight-exit-receipt/v1",
    "maximum_result_bytes": 256 * 1024,
    "protocol_capability": AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    "result_schema": "riftx.audit-preflight-result/v1",
    "schema_version": AUDIT_PREFLIGHT_OUTPUT_CONTRACT_SCHEMA_VERSION,
    "stop_receipt_schema": "riftx.audit-preflight-stop-receipt/v1",
}
AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST: PreflightDigest = hashlib.sha256(
    AUDIT_PREFLIGHT_OUTPUT_CONTRACT_SCHEMA_VERSION.encode("ascii")
    + b"\0"
    + _canonical_json(_OUTPUT_CONTRACT)
).hexdigest()


class AuditPreflightDispatchEnvelope(AuditPreflightStrictModel):
    """One exact claim delivered only over the Preflight-specific Runner wire."""

    schema_version: Literal["riftx.audit-preflight-dispatch/v1"] = (
        AUDIT_PREFLIGHT_DISPATCH_SCHEMA_VERSION
    )
    owner_kind: Literal["preflight_job"] = "preflight_job"
    owner: AuditPreflightEffectOwner
    lease: AuditPreflightLeaseEnvelope
    request: PreflightRequest = Field(repr=False)
    capsule_id: PreflightId
    status: Literal[AuditPreflightJobStatus.CLAIMED] = AuditPreflightJobStatus.CLAIMED
    state_version: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def validate_bindings(self) -> AuditPreflightDispatchEnvelope:
        if self.lease.owner != self.owner:
            raise ValueError("Preflight dispatch lease owner does not match owner")
        if self.owner.job_id != self.lease.owner.job_id:
            raise ValueError("Preflight dispatch job binding does not match")
        if self.owner.request_schema_version != self.request.schema_version:
            raise ValueError("Preflight dispatch request schema binding does not match")
        if self.owner.request_digest != self.request.request_digest:
            raise ValueError("Preflight dispatch request digest does not match")
        if self.owner.source_node_id != self.request.source_execution_target.node_id:
            raise ValueError("Preflight dispatch source node binding does not match")
        if self.owner.backend_id != self.request.source_execution_target.source_ingest_backend:
            raise ValueError("Preflight dispatch backend binding does not match")
        if self.lease.expected_state_version != self.state_version:
            raise ValueError("Preflight dispatch state version does not match lease")
        if self.lease.output_contract_digest != AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST:
            raise ValueError("Preflight dispatch output contract is unsupported")
        return self


class AuditPreflightLeaseGrant(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-lease-envelope/v1"] = (
        "riftx.audit-preflight-lease-envelope/v1"
    )
    job_id: PreflightId
    status: AuditPreflightJobStatus
    state_version: int = Field(strict=True, ge=1)
    lease_envelope_digest: PreflightDigest
    lease_expires_at: AwareDatetime
    lease_duration_seconds: float = Field(strict=True, gt=0)


class AuditPreflightStartGrant(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-start-grant/v1"] = (
        "riftx.audit-preflight-start-grant/v1"
    )
    job_id: PreflightId
    capsule_id: PreflightId
    status: Literal[AuditPreflightJobStatus.RUNNING] = AuditPreflightJobStatus.RUNNING
    state_version: int = Field(strict=True, ge=1)
    started_at: AwareDatetime


class AuditPreflightCallbackAck(AuditPreflightStrictModel):
    schema_version: Literal["riftx.audit-preflight-callback-ack/v1"] = (
        "riftx.audit-preflight-callback-ack/v1"
    )
    job_id: PreflightId
    status: AuditPreflightJobStatus
    state_version: int = Field(strict=True, ge=1)
    finished_at: datetime | None = None


__all__ = [
    "AUDIT_PREFLIGHT_DISPATCH_SCHEMA_VERSION",
    "AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST",
    "AUDIT_PREFLIGHT_OUTPUT_CONTRACT_SCHEMA_VERSION",
    "AuditPreflightCallbackAck",
    "AuditPreflightDispatchEnvelope",
    "AuditPreflightLeaseGrant",
    "AuditPreflightStartGrant",
]
