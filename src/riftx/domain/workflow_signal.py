"""Durable, owner-bound Workflow signal intent contracts.

The models in this module are intentionally independent from Temporal and
SQLAlchemy.  A signal intent is a durable business fact, not an in-process
retry closure.  Owner kind, Workflow protocol, Workflow identity, signal
kind, source event identity, and canonical payload are all immutable once the
intent is created.

Runner stop acknowledgements are deliberately absent from the source/signal
vocabulary.  They are physical-stop receipts and must never be represented as
ordinary Workflow completion intents.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from .base import DomainModel, new_id, utc_now
from .enums import RunKind

WORKFLOW_SIGNAL_INTENT_SCHEMA_VERSION: Literal[
    "riftx.workflow-signal-intent/v1"
] = "riftx.workflow-signal-intent/v1"
GENERAL_RUN_WORKFLOW_PROTOCOL_V1: Literal[
    "riftx.general-run-workflow/v1"
] = "riftx.general-run-workflow/v1"
CODE_AUDIT_WORKFLOW_PROTOCOL_V1: Literal[
    "riftx.code-audit-workflow/v1"
] = "riftx.code-audit-workflow/v1"

_MAX_PAYLOAD_BYTES = 64 * 1024
_GENERAL_WORKFLOW_PREFIX = "riftx-code-audit-"


class WorkflowSignalOwnerKind(StrEnum):
    GENERAL_RUN = "general_run"
    CODE_AUDIT = "code_audit"


class WorkflowSignalKind(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    APPROVE = "approve"
    REJECT = "reject"
    EXECUTION_COMPLETED = "execution_completed"
    SAFETY_RECONCILE = "safety_reconcile"


class WorkflowSignalSourceKind(StrEnum):
    CONTROL_INTENT = "control_intent"
    APPROVAL_DECISION = "approval_decision"
    EXECUTION_TERMINAL = "execution_terminal"
    SAFETY_RECONCILIATION = "safety_reconciliation"


class WorkflowSignalDeliveryState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    OBSERVED_DELIVERED = "observed_delivered"
    RETRYABLE = "retryable"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SUPERSEDED = "superseded"


class WorkflowSignalReceiptKind(StrEnum):
    DELIVERED = "delivered"
    OBSERVED_DELIVERED = "observed_delivered"


_SIGNALS_BY_SOURCE: dict[WorkflowSignalSourceKind, frozenset[WorkflowSignalKind]] = {
    WorkflowSignalSourceKind.CONTROL_INTENT: frozenset(
        {
            WorkflowSignalKind.PAUSE,
            WorkflowSignalKind.RESUME,
            WorkflowSignalKind.CANCEL,
        }
    ),
    WorkflowSignalSourceKind.APPROVAL_DECISION: frozenset(
        {WorkflowSignalKind.APPROVE, WorkflowSignalKind.REJECT}
    ),
    WorkflowSignalSourceKind.EXECUTION_TERMINAL: frozenset(
        {WorkflowSignalKind.EXECUTION_COMPLETED}
    ),
    WorkflowSignalSourceKind.SAFETY_RECONCILIATION: frozenset(
        {WorkflowSignalKind.SAFETY_RECONCILE}
    ),
}


class WorkflowSignalIntent(DomainModel):
    """One immutable signal identity plus mutable lease/delivery projection."""

    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    schema_version: Literal["riftx.workflow-signal-intent/v1"] = (
        WORKFLOW_SIGNAL_INTENT_SCHEMA_VERSION
    )
    owner_kind: WorkflowSignalOwnerKind
    owner_identity: str = Field(default="", min_length=0, max_length=255)
    run_id: str = Field(min_length=1, max_length=64)
    run_kind: RunKind
    audit_id: str | None = Field(default=None, min_length=1, max_length=128)
    workflow_protocol_version: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=255)
    signal_kind: WorkflowSignalKind
    source_event_kind: WorkflowSignalSourceKind
    source_event_id: str = Field(min_length=1, max_length=128)
    source_state_version: int = Field(ge=1)
    identity_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    payload_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    delivery_state: WorkflowSignalDeliveryState = WorkflowSignalDeliveryState.PENDING
    lease_owner: str | None = Field(default=None, min_length=1, max_length=255)
    lease_expires_at: AwareDatetime | None = None
    attempt: int = Field(default=0, ge=0)
    next_attempt_at: AwareDatetime | None = Field(default_factory=utc_now)
    delivery_receipt_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    last_error_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9._-]{0,127}$",
    )
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    delivered_at: AwareDatetime | None = None
    state_version: int = Field(default=1, ge=1)

    @classmethod
    def general_run(
        cls,
        *,
        run_id: str,
        workflow_id: str,
        signal_kind: WorkflowSignalKind,
        source_event_kind: WorkflowSignalSourceKind,
        source_event_id: str,
        source_state_version: int,
        payload: dict[str, JsonValue],
        intent_id: str | None = None,
        created_at: AwareDatetime | None = None,
    ) -> WorkflowSignalIntent:
        now = created_at or utc_now()
        return cls(
            id=intent_id or new_id(),
            owner_kind=WorkflowSignalOwnerKind.GENERAL_RUN,
            run_id=run_id,
            run_kind=RunKind.GENERAL,
            workflow_protocol_version=GENERAL_RUN_WORKFLOW_PROTOCOL_V1,
            workflow_id=workflow_id,
            signal_kind=signal_kind,
            source_event_kind=source_event_kind,
            source_event_id=source_event_id,
            source_state_version=source_state_version,
            payload=payload,
            created_at=now,
            updated_at=now,
            next_attempt_at=now,
        )

    @classmethod
    def code_audit(
        cls,
        *,
        audit_id: str,
        run_id: str,
        workflow_id: str,
        signal_kind: WorkflowSignalKind,
        source_event_kind: WorkflowSignalSourceKind,
        source_event_id: str,
        source_state_version: int,
        payload: dict[str, JsonValue],
        intent_id: str | None = None,
        created_at: AwareDatetime | None = None,
    ) -> WorkflowSignalIntent:
        now = created_at or utc_now()
        return cls(
            id=intent_id or new_id(),
            owner_kind=WorkflowSignalOwnerKind.CODE_AUDIT,
            run_id=run_id,
            run_kind=RunKind.CODE_AUDIT,
            audit_id=audit_id,
            workflow_protocol_version=CODE_AUDIT_WORKFLOW_PROTOCOL_V1,
            workflow_id=workflow_id,
            signal_kind=signal_kind,
            source_event_kind=source_event_kind,
            source_event_id=source_event_id,
            source_state_version=source_state_version,
            payload=payload,
            created_at=now,
            updated_at=now,
            next_attempt_at=now,
        )

    @model_validator(mode="after")
    def validate_contract(self) -> WorkflowSignalIntent:
        expected_owner = workflow_signal_owner_identity(
            self.owner_kind,
            run_id=self.run_id,
            audit_id=self.audit_id,
        )
        if self.owner_identity:
            if not hmac.compare_digest(self.owner_identity, expected_owner):
                raise ValueError("Workflow signal owner identity does not match")
        else:
            object.__setattr__(self, "owner_identity", expected_owner)

        if self.owner_kind is WorkflowSignalOwnerKind.GENERAL_RUN:
            if (
                self.run_kind is not RunKind.GENERAL
                or self.audit_id is not None
                or self.workflow_protocol_version != GENERAL_RUN_WORKFLOW_PROTOCOL_V1
                or self.workflow_id.startswith(_GENERAL_WORKFLOW_PREFIX)
            ):
                raise ValueError("General Workflow signal owner binding is invalid")
        elif (
            self.run_kind is not RunKind.CODE_AUDIT
            or self.audit_id is None
            or self.workflow_protocol_version != CODE_AUDIT_WORKFLOW_PROTOCOL_V1
            or self.workflow_id != f"riftx-code-audit-{self.audit_id}"
        ):
            raise ValueError("Code Audit Workflow signal owner binding is invalid")

        if self.signal_kind not in _SIGNALS_BY_SOURCE[self.source_event_kind]:
            raise ValueError("Workflow signal kind does not match its source event kind")

        expected_identity = workflow_signal_identity_digest(self)
        if self.identity_digest:
            if not hmac.compare_digest(self.identity_digest, expected_identity):
                raise ValueError("Workflow signal identity digest does not match")
        else:
            object.__setattr__(self, "identity_digest", expected_identity)

        expected_payload = workflow_signal_payload_digest(self.payload)
        if self.payload_digest:
            if not hmac.compare_digest(self.payload_digest, expected_payload):
                raise ValueError("Workflow signal payload digest does not match")
        else:
            object.__setattr__(self, "payload_digest", expected_payload)

        has_lease_owner = self.lease_owner is not None
        has_lease_expiry = self.lease_expires_at is not None
        if has_lease_owner != has_lease_expiry:
            raise ValueError("Workflow signal lease fields must be present together")
        if self.delivery_state is WorkflowSignalDeliveryState.CLAIMED:
            if not has_lease_owner or self.attempt < 1:
                raise ValueError("Claimed Workflow signal requires an active lease and attempt")
        elif self.delivery_state is WorkflowSignalDeliveryState.OUTCOME_UNKNOWN:
            if self.attempt < 1 or self.next_attempt_at is None:
                raise ValueError("Unknown Workflow signal outcome requires reconciliation")
        elif self.delivery_state is WorkflowSignalDeliveryState.SUPERSEDED:
            if (
                has_lease_owner
                or has_lease_expiry
                or self.attempt < 1
                or self.next_attempt_at is not None
                or self.last_error_code is None
            ):
                raise ValueError("Superseded Workflow signal requires a terminal reason")
        elif has_lease_owner:
            raise ValueError("Only claimed or outcome-unknown signals may carry a lease")

        delivered = self.delivery_state in {
            WorkflowSignalDeliveryState.DELIVERED,
            WorkflowSignalDeliveryState.OBSERVED_DELIVERED,
        }
        if delivered:
            if (
                self.delivery_receipt_digest is None
                or self.delivered_at is None
                or self.next_attempt_at is not None
                or self.last_error_code is not None
                or self.attempt < 1
            ):
                raise ValueError("Delivered Workflow signal has an incomplete receipt")
        elif self.delivery_receipt_digest is not None or self.delivered_at is not None:
            raise ValueError("Undelivered Workflow signal cannot carry a delivery receipt")

        if self.delivery_state in {
            WorkflowSignalDeliveryState.PENDING,
            WorkflowSignalDeliveryState.RETRYABLE,
        } and self.next_attempt_at is None:
            raise ValueError("Pending Workflow signal requires a next attempt time")
        if self.delivery_state is WorkflowSignalDeliveryState.RETRYABLE and self.attempt < 1:
            raise ValueError("Retryable Workflow signal requires a prior attempt")
        if self.delivery_state is WorkflowSignalDeliveryState.PENDING and self.attempt != 0:
            raise ValueError("New pending Workflow signal cannot carry an attempt")
        if self.delivery_state is WorkflowSignalDeliveryState.CLAIMED and (
            self.next_attempt_at is not None
        ):
            raise ValueError("Claimed Workflow signal cannot remain scheduled")

        if self.updated_at < self.created_at:
            raise ValueError("Workflow signal updated_at precedes created_at")
        if self.delivered_at is not None and self.delivered_at < self.created_at:
            raise ValueError("Workflow signal delivered_at precedes created_at")
        if self.lease_expires_at is not None and self.lease_expires_at <= self.updated_at:
            raise ValueError("Workflow signal lease must expire after updated_at")
        return self


def canonical_workflow_signal_payload(payload: dict[str, JsonValue]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Workflow signal payload must be canonical JSON") from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ValueError("Workflow signal payload exceeds the 64 KiB durable limit")
    return encoded


def workflow_signal_payload_digest(payload: dict[str, JsonValue]) -> str:
    return hashlib.sha256(
        b"riftx.workflow-signal-payload/v1\0"
        + canonical_workflow_signal_payload(payload)
    ).hexdigest()


def workflow_signal_owner_identity(
    owner_kind: WorkflowSignalOwnerKind,
    *,
    run_id: str,
    audit_id: str | None,
) -> str:
    if owner_kind is WorkflowSignalOwnerKind.GENERAL_RUN:
        if audit_id is not None:
            raise ValueError("General Workflow signal cannot carry audit_id")
        return f"general_run:{run_id}"
    if audit_id is None:
        raise ValueError("Code Audit Workflow signal requires audit_id")
    return f"code_audit:{audit_id}"


def workflow_signal_identity_digest(intent: WorkflowSignalIntent) -> str:
    identity = {
        "schema_version": intent.schema_version,
        "owner_kind": intent.owner_kind.value,
        "owner_identity": intent.owner_identity,
        "run_id": intent.run_id,
        "run_kind": intent.run_kind.value,
        "audit_id": intent.audit_id,
        "workflow_protocol_version": intent.workflow_protocol_version,
        "workflow_id": intent.workflow_id,
        "signal_kind": intent.signal_kind.value,
        "source_event_kind": intent.source_event_kind.value,
        "source_event_id": intent.source_event_id,
        "source_state_version": intent.source_state_version,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(b"riftx.workflow-signal-identity/v1\0" + canonical).hexdigest()


def workflow_signal_delivery_receipt_digest(
    intent: WorkflowSignalIntent,
    *,
    receipt_kind: WorkflowSignalReceiptKind,
    dispatcher_id: str,
    observed_at: AwareDatetime,
    transport_receipt: str,
) -> str:
    if not dispatcher_id or not transport_receipt:
        raise ValueError("Workflow signal receipt identity must be non-empty")
    payload = {
        "schema_version": "riftx.workflow-signal-delivery-receipt/v1",
        "intent_id": intent.id,
        "identity_digest": intent.identity_digest,
        "payload_digest": intent.payload_digest,
        "receipt_kind": receipt_kind.value,
        "dispatcher_id": dispatcher_id,
        "observed_at": observed_at.isoformat(),
        "transport_receipt": transport_receipt,
        "attempt": intent.attempt,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(
        b"riftx.workflow-signal-delivery-receipt/v1\0" + canonical
    ).hexdigest()


__all__ = [
    "CODE_AUDIT_WORKFLOW_PROTOCOL_V1",
    "GENERAL_RUN_WORKFLOW_PROTOCOL_V1",
    "WORKFLOW_SIGNAL_INTENT_SCHEMA_VERSION",
    "WorkflowSignalDeliveryState",
    "WorkflowSignalIntent",
    "WorkflowSignalKind",
    "WorkflowSignalOwnerKind",
    "WorkflowSignalReceiptKind",
    "WorkflowSignalSourceKind",
    "canonical_workflow_signal_payload",
    "workflow_signal_delivery_receipt_digest",
    "workflow_signal_identity_digest",
    "workflow_signal_owner_identity",
    "workflow_signal_payload_digest",
]
