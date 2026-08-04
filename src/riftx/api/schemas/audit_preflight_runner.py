"""Strict request/response contracts for the dedicated Preflight Runner wire."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from riftx.domain.audit_preflight import (
    AuditPreflightEffectOwner,
    AuditPreflightExitReceipt,
    AuditPreflightJobStatus,
    AuditPreflightLeaseEnvelope,
    AuditPreflightResult,
    AuditPreflightStopReceipt,
)
from riftx.domain.audit_preflight_wire import AuditPreflightDispatchEnvelope

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

type _Id = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=128, pattern=_ID_PATTERN),
]
type _Digest = Annotated[
    str,
    Field(strict=True, min_length=64, max_length=64, pattern=_DIGEST_PATTERN),
]


class _AuditPreflightRunnerRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        validate_default=True,
    )


class AuditPreflightRunnerPollResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dispatch: AuditPreflightDispatchEnvelope | None = None


class AuditPreflightCallbackIdentity(_AuditPreflightRunnerRequest):
    owner_kind: Literal["preflight_job"] = "preflight_job"
    owner: AuditPreflightEffectOwner
    lease: AuditPreflightLeaseEnvelope
    state_version: int = Field(strict=True, ge=1)
    capsule_id: _Id

    @field_validator("owner", mode="before")
    @classmethod
    def parse_owner(cls, value: object) -> AuditPreflightEffectOwner:
        if isinstance(value, AuditPreflightEffectOwner):
            return value
        return AuditPreflightEffectOwner.model_validate_json(
            json.dumps(value, sort_keys=True, separators=(",", ":"))
        )

    @field_validator("lease", mode="before")
    @classmethod
    def parse_lease(cls, value: object) -> AuditPreflightLeaseEnvelope:
        if isinstance(value, AuditPreflightLeaseEnvelope):
            return value
        return AuditPreflightLeaseEnvelope.model_validate_json(
            json.dumps(value, sort_keys=True, separators=(",", ":"))
        )

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.lease.owner != self.owner:
            raise ValueError("Audit Preflight callback lease owner does not match")
        return self


class RenewAuditPreflightLeaseRequest(AuditPreflightCallbackIdentity):
    schema_version: Literal["riftx.audit-preflight-renew-request/v1"] = (
        "riftx.audit-preflight-renew-request/v1"
    )


class StartAuditPreflightRequest(AuditPreflightCallbackIdentity):
    schema_version: Literal["riftx.audit-preflight-start-request/v1"] = (
        "riftx.audit-preflight-start-request/v1"
    )
    capsule_prepare_proof_digest: _Digest


class FinishAuditPreflightRequest(AuditPreflightCallbackIdentity):
    schema_version: Literal["riftx.audit-preflight-finish-request/v1"] = (
        "riftx.audit-preflight-finish-request/v1"
    )
    status: AuditPreflightJobStatus
    result: AuditPreflightResult | None = None
    safe_error_code: _Id | None = None
    exit_receipt: AuditPreflightExitReceipt

    @field_validator("result", mode="before")
    @classmethod
    def parse_result(cls, value: object) -> AuditPreflightResult | None:
        if value is None or isinstance(value, AuditPreflightResult):
            return value
        return AuditPreflightResult.model_validate_json(
            json.dumps(value, sort_keys=True, separators=(",", ":"))
        )

    @field_validator("exit_receipt", mode="before")
    @classmethod
    def parse_exit_receipt(cls, value: object) -> AuditPreflightExitReceipt:
        if isinstance(value, AuditPreflightExitReceipt):
            return value
        return AuditPreflightExitReceipt.model_validate_json(
            json.dumps(value, sort_keys=True, separators=(",", ":"))
        )

    @model_validator(mode="after")
    def validate_finish_contract(self) -> Self:
        if self.status not in {
            AuditPreflightJobStatus.SUCCEEDED,
            AuditPreflightJobStatus.REJECTED,
            AuditPreflightJobStatus.FAILED,
        }:
            raise ValueError("Audit Preflight finish status is invalid")
        if self.exit_receipt.job_id != self.owner.job_id:
            raise ValueError("Audit Preflight exit receipt job does not match owner")
        return self


class StopAuditPreflightRequest(AuditPreflightCallbackIdentity):
    schema_version: Literal["riftx.audit-preflight-stop-request/v1"] = (
        "riftx.audit-preflight-stop-request/v1"
    )
    status: AuditPreflightJobStatus
    safe_error_code: _Id | None = None
    stop_receipt: AuditPreflightStopReceipt

    @field_validator("stop_receipt", mode="before")
    @classmethod
    def parse_stop_receipt(cls, value: object) -> AuditPreflightStopReceipt:
        if isinstance(value, AuditPreflightStopReceipt):
            return value
        return AuditPreflightStopReceipt.model_validate_json(
            json.dumps(value, sort_keys=True, separators=(",", ":"))
        )

    @model_validator(mode="after")
    def validate_stop_contract(self) -> Self:
        if self.status not in {
            AuditPreflightJobStatus.CANCELLED,
            AuditPreflightJobStatus.FAILED,
        }:
            raise ValueError("Audit Preflight stop status is invalid")
        if self.stop_receipt.job_id != self.owner.job_id:
            raise ValueError("Audit Preflight stop receipt job does not match owner")
        return self


__all__ = [
    "AuditPreflightRunnerPollResponse",
    "FinishAuditPreflightRequest",
    "RenewAuditPreflightLeaseRequest",
    "StartAuditPreflightRequest",
    "StopAuditPreflightRequest",
]
