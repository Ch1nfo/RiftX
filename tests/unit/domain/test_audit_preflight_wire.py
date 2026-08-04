from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from riftx.domain.audit import AuditMode, SourceTargetKind
from riftx.domain.audit_preflight import (
    AuditPreflightEffectOwner,
    AuditPreflightLeaseEnvelope,
    AuditPreflightSourceExecutionTarget,
    AuditPreflightTarget,
    PreflightRequest,
)
from riftx.domain.audit_preflight_wire import (
    AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
    AuditPreflightDispatchEnvelope,
)
from riftx.domain.runner import RunnerPrincipal


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _dispatch() -> AuditPreflightDispatchEnvelope:
    now = datetime(2026, 8, 4, 12, tzinfo=UTC)
    request = PreflightRequest(
        client_request_id="123e4567-e89b-42d3-a456-426614174000",
        repository_path="/srv/source/repository",
        source_execution_target=AuditPreflightSourceExecutionTarget(
            source_ingest_backend="linux_container"
        ),
        target=AuditPreflightTarget(
            kind=SourceTargetKind.WORKING_TREE,
            revision="HEAD",
        ),
        mode=AuditMode.STANDARD,
    )
    owner = AuditPreflightEffectOwner.from_request(
        job_id="job-1",
        operator_principal_id="operator-1",
        authorization_scope_digest=_digest("authorization"),
        source_root_identity_digest=_digest("root"),
        request=request,
        backend_id="linux_container",
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    lease = AuditPreflightLeaseEnvelope(
        owner=owner,
        runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=1),
        lease_id="lease-1",
        lease_expires_at=now + timedelta(minutes=1),
        expected_state_version=2,
        output_contract_digest=AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
    )
    return AuditPreflightDispatchEnvelope(
        owner=owner,
        lease=lease,
        request=request,
        capsule_id="capsule-1",
        state_version=2,
    )


def test_dispatch_envelope_binds_exact_owner_lease_request_and_contract() -> None:
    dispatch = _dispatch()

    assert dispatch.owner_kind == "preflight_job"
    assert dispatch.lease.owner.effect_owner_digest == dispatch.owner.effect_owner_digest
    assert dispatch.lease.output_contract_digest == AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST


def test_dispatch_envelope_rejects_cross_wire_state_and_request_drift() -> None:
    dispatch = _dispatch()
    payload = dispatch.model_dump(mode="python")
    payload["state_version"] = 3
    with pytest.raises(ValidationError, match="state version"):
        AuditPreflightDispatchEnvelope.model_validate(payload)

    request_payload = dispatch.request.model_dump(mode="python")
    request_payload["repository_path"] = "/srv/source/other"
    payload = dispatch.model_dump(mode="python")
    payload["request"] = PreflightRequest.model_validate(request_payload)
    with pytest.raises(ValidationError, match="request digest"):
        AuditPreflightDispatchEnvelope.model_validate(payload)
