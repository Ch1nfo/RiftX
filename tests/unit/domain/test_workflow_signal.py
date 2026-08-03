from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from riftx.domain.workflow_signal import (
    CODE_AUDIT_WORKFLOW_PROTOCOL_V1,
    GENERAL_RUN_WORKFLOW_PROTOCOL_V1,
    WorkflowSignalDeliveryState,
    WorkflowSignalIntent,
    WorkflowSignalKind,
    WorkflowSignalOwnerKind,
    WorkflowSignalSourceKind,
    canonical_workflow_signal_payload,
    workflow_signal_payload_digest,
)

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _general(**updates: object) -> WorkflowSignalIntent:
    payload: dict[str, object] = {
        "run_id": "run-1",
        "workflow_id": "riftx-run-run-1",
        "signal_kind": WorkflowSignalKind.EXECUTION_COMPLETED,
        "source_event_kind": WorkflowSignalSourceKind.EXECUTION_TERMINAL,
        "source_event_id": "execution-1",
        "source_state_version": 3,
        "payload": {"execution_id": "execution-1", "status": "completed"},
        "created_at": NOW,
    }
    payload.update(updates)
    return WorkflowSignalIntent.general_run(**payload)  # type: ignore[arg-type]


def test_payload_is_canonical_and_domain_separated() -> None:
    left = {"nested": {"z": 1, "a": True}, "items": [3, 2, 1]}
    right = {"items": [3, 2, 1], "nested": {"a": True, "z": 1}}

    assert canonical_workflow_signal_payload(left) == canonical_workflow_signal_payload(right)
    assert workflow_signal_payload_digest(left) == workflow_signal_payload_digest(right)
    assert workflow_signal_payload_digest(left) != __import__("hashlib").sha256(
        canonical_workflow_signal_payload(left)
    ).hexdigest()


def test_general_owner_contract_is_computed_and_exact() -> None:
    intent = _general()

    assert intent.owner_kind is WorkflowSignalOwnerKind.GENERAL_RUN
    assert intent.owner_identity == "general_run:run-1"
    assert intent.workflow_protocol_version == GENERAL_RUN_WORKFLOW_PROTOCOL_V1
    assert intent.audit_id is None
    assert len(intent.identity_digest) == 64
    assert len(intent.payload_digest) == 64


def test_code_audit_cannot_fallback_to_general_workflow_identity_or_protocol() -> None:
    intent = WorkflowSignalIntent.code_audit(
        audit_id="audit-1",
        run_id="run-1",
        workflow_id="riftx-code-audit-audit-1",
        signal_kind=WorkflowSignalKind.CANCEL,
        source_event_kind=WorkflowSignalSourceKind.CONTROL_INTENT,
        source_event_id="cancel-request-1",
        source_state_version=2,
        payload={"audit_id": "audit-1"},
        created_at=NOW,
    )

    assert intent.owner_identity == "code_audit:audit-1"
    assert intent.workflow_protocol_version == CODE_AUDIT_WORKFLOW_PROTOCOL_V1

    payload = intent.model_dump(mode="python")
    payload["workflow_id"] = "riftx-run-run-1"
    payload["workflow_protocol_version"] = GENERAL_RUN_WORKFLOW_PROTOCOL_V1
    payload["owner_identity"] = ""
    payload["identity_digest"] = ""
    with pytest.raises(ValidationError, match="Code Audit Workflow signal owner binding"):
        WorkflowSignalIntent.model_validate(payload)


def test_stop_ack_cannot_be_encoded_as_ordinary_completion_source() -> None:
    payload = _general().model_dump(mode="python")
    payload["source_event_kind"] = "runner_stop_ack"
    payload["identity_digest"] = ""

    with pytest.raises(ValidationError):
        WorkflowSignalIntent.model_validate(payload)


def test_source_event_and_signal_kind_must_match() -> None:
    with pytest.raises(ValidationError, match="does not match its source event"):
        _general(signal_kind=WorkflowSignalKind.APPROVE)


def test_payload_and_identity_digest_tampering_is_rejected() -> None:
    intent = _general()
    payload = intent.model_dump(mode="python")
    payload["payload"] = {"execution_id": "different"}
    with pytest.raises(ValidationError, match="payload digest"):
        WorkflowSignalIntent.model_validate(payload)

    payload = intent.model_dump(mode="python")
    payload["source_state_version"] = 4
    with pytest.raises(ValidationError, match="identity digest"):
        WorkflowSignalIntent.model_validate(payload)


def test_delivery_state_requires_lease_or_receipt_shape() -> None:
    payload = _general().model_dump(mode="python")
    payload.update(
        delivery_state=WorkflowSignalDeliveryState.CLAIMED,
        attempt=1,
        next_attempt_at=None,
    )
    with pytest.raises(ValidationError, match="active lease"):
        WorkflowSignalIntent.model_validate(payload)

    payload = _general().model_dump(mode="python")
    payload.update(
        delivery_state=WorkflowSignalDeliveryState.DELIVERED,
        attempt=1,
        next_attempt_at=None,
        delivered_at=NOW,
    )
    with pytest.raises(ValidationError, match="incomplete receipt"):
        WorkflowSignalIntent.model_validate(payload)


def test_superseded_state_requires_terminal_reason_without_retry_or_lease() -> None:
    payload = _general().model_dump(mode="python")
    payload.update(
        delivery_state=WorkflowSignalDeliveryState.SUPERSEDED,
        attempt=1,
        next_attempt_at=None,
        last_error_code="run_terminal",
    )

    superseded = WorkflowSignalIntent.model_validate(payload)

    assert superseded.delivery_state is WorkflowSignalDeliveryState.SUPERSEDED
    assert superseded.last_error_code == "run_terminal"

    payload["last_error_code"] = None
    with pytest.raises(ValidationError, match="terminal reason"):
        WorkflowSignalIntent.model_validate(payload)

    payload["last_error_code"] = "run_terminal"
    payload["lease_owner"] = "stale-worker"
    payload["lease_expires_at"] = NOW.replace(hour=13)
    with pytest.raises(ValidationError, match="terminal reason"):
        WorkflowSignalIntent.model_validate(payload)
