from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.application.test_audits import _aggregate_for_scan
from tests.unit.domain.test_audit_domain import _scan

from riftx.application.errors import ApplicationConflictError, ServiceUnavailableError
from riftx.application.ports.audit_start import (
    AuditStartRevalidationDisposition,
    AuditStartRevalidationProof,
    AuditStartRevalidationRequest,
)
from riftx.application.services.audit_start import (
    AuditStartApplicationService,
    StartAudit,
)
from riftx.domain import (
    AuditLifecycleStatus,
    LocalPrincipal,
    OperatorCapability,
)

NOW = datetime(2026, 8, 4, 8, tzinfo=UTC)
START_REQUEST_ID = "323e4567-e89b-42d3-a456-426614174000"
PRINCIPAL = LocalPrincipal(
    id="principal-1",
    capabilities=frozenset(OperatorCapability),
)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _revalidation_request(**updates: object) -> AuditStartRevalidationRequest:
    payload: dict[str, object] = {
        "audit_id": "audit-1",
        "run_id": "run-1",
        "start_request_id": START_REQUEST_ID,
        "preflight_plan_id": "plan-1",
        "preflight_plan_digest": _digest("plan"),
        "contract_digest": _digest("contract"),
        "security_context_id": "riftx.audit-empty-security-context/v1",
        "security_context_digest": _digest("context"),
        "operator_principal_id": "principal-1",
        "authorization_scope_digest": _digest("authorization"),
        "source_node_id": "local",
        "source_root_identity_digest": _digest("source-root"),
        "repository_identity_digest": _digest("repository"),
        "expected_content_identity_digest": _digest("content"),
        "source_ingest_backend_id": "linux_container",
        "source_ingest_image_digest": _digest("image"),
        "source_ingest_policy_digest": _digest("policy"),
        "source_repository_path": "/srv/authorized/private-repository",
        "requested_at": NOW,
        "expires_at": NOW + timedelta(seconds=30),
    }
    payload.update(updates)
    return AuditStartRevalidationRequest(**payload)  # type: ignore[arg-type]


def _matched_proof(request: AuditStartRevalidationRequest) -> AuditStartRevalidationProof:
    return AuditStartRevalidationProof(
        request_digest=request.request_digest,
        disposition=AuditStartRevalidationDisposition.MATCHED,
        reason_code="source_content_matched",
        observed_content_identity_digest=request.expected_content_identity_digest,
        proof_digest=_digest("revalidation-proof"),
        issued_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=20),
    )


def test_revalidation_request_is_canonical_bound_and_hides_repository_path() -> None:
    request = _revalidation_request()
    replay = _revalidation_request()
    changed_path = _revalidation_request(
        source_repository_path="/srv/authorized/other-repository"
    )

    assert request.request_digest == replay.request_digest
    assert request.request_digest != changed_path.request_digest
    assert request.source_repository_path not in repr(request)
    assert "private-repository" not in repr(request)


def test_revalidation_proof_accepts_only_exact_fresh_matched_content() -> None:
    request = _revalidation_request()
    proof = _matched_proof(request)

    assert proof.accepts(request, at=NOW + timedelta(seconds=2)) is True
    assert proof.accepts(
        replace(request, expected_content_identity_digest=_digest("changed")),
        at=NOW + timedelta(seconds=2),
    ) is False
    assert proof.accepts(request, at=proof.expires_at) is False
    assert replace(
        proof,
        disposition=AuditStartRevalidationDisposition.CHANGED,
    ).accepts(request, at=NOW + timedelta(seconds=2)) is False


def test_unavailable_revalidation_cannot_smuggle_observed_or_proof_digests() -> None:
    request = _revalidation_request()
    with pytest.raises(ValueError, match="cannot carry observed proof"):
        AuditStartRevalidationProof(
            request_digest=request.request_digest,
            disposition=AuditStartRevalidationDisposition.UNAVAILABLE,
            reason_code="source_revalidation_unavailable",
            observed_content_identity_digest=request.expected_content_identity_digest,
            proof_digest=_digest("invented-proof"),
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=10),
        )


class FakeAuthorizedAudits:
    def __init__(self, aggregate: object | None = None) -> None:
        self.aggregate = aggregate
        self.calls: list[tuple[str, OperatorCapability]] = []

    async def get_authorized(
        self,
        audit_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: object,
        capability: OperatorCapability = OperatorCapability.READ,
    ) -> object:
        assert principal == PRINCIPAL
        assert authorizer is AUTHORIZER
        self.calls.append((audit_id, capability))
        assert self.aggregate is not None
        return self.aggregate


class ForbiddenStartPort:
    def __init__(self) -> None:
        self.calls = 0

    async def revalidate(self, request: object) -> object:
        self.calls += 1
        raise AssertionError("current AUD-201 contract must not revalidate source")

    async def admit(self, request: object) -> object:
        self.calls += 1
        raise AssertionError("current AUD-201 contract must not open Start UoW")


AUTHORIZER = object()


def _service(
    audits: FakeAuthorizedAudits,
    ports: ForbiddenStartPort,
    *,
    enabled: bool = True,
) -> AuditStartApplicationService:
    return AuditStartApplicationService(
        audits=audits,  # type: ignore[arg-type]
        revalidation_port=ports,  # type: ignore[arg-type]
        admission_uow=ports,  # type: ignore[arg-type]
        feature_enabled=enabled,
    )


def _command(contract_digest: str) -> StartAudit:
    return StartAudit(
        start_request_id=START_REQUEST_ID,
        reviewed_contract_digest=contract_digest,
    )


@pytest.mark.asyncio
async def test_start_feature_and_wire_fences_run_before_authorized_read() -> None:
    audits = FakeAuthorizedAudits()
    ports = ForbiddenStartPort()
    with pytest.raises(ServiceUnavailableError) as disabled:
        await _service(audits, ports, enabled=False).start_authorized(
            "audit-1",
            _command(_digest("contract")),
            principal=PRINCIPAL,
            authorizer=AUTHORIZER,  # type: ignore[arg-type]
        )
    assert disabled.value.code == "feature_disabled"

    with pytest.raises(ApplicationConflictError) as invalid:
        await _service(audits, ports).start_authorized(
            "audit-1",
            StartAudit(
                start_request_id="not-a-uuid",
                reviewed_contract_digest=_digest("contract"),
            ),
            principal=PRINCIPAL,
            authorizer=AUTHORIZER,  # type: ignore[arg-type]
        )
    assert invalid.value.code == "audit_start_request_invalid"
    assert audits.calls == []
    assert ports.calls == 0


@pytest.mark.asyncio
async def test_historical_v1_start_is_rejected_after_host_execute_authorization() -> None:
    aggregate = _aggregate_for_scan(_scan())
    audits = FakeAuthorizedAudits(aggregate)
    ports = ForbiddenStartPort()

    with pytest.raises(ApplicationConflictError) as captured:
        await _service(audits, ports).start_authorized(
            aggregate.audit.value.id,
            _command(aggregate.contract.value.contract_digest),
            principal=PRINCIPAL,
            authorizer=AUTHORIZER,  # type: ignore[arg-type]
        )

    assert captured.value.code == "audit_start_not_eligible"
    assert audits.calls == [
        (aggregate.audit.value.id, OperatorCapability.HOST_EXECUTE)
    ]
    assert ports.calls == 0


@pytest.mark.asyncio
async def test_review_and_state_conflicts_reject_before_version_or_source_oracles() -> None:
    aggregate = _aggregate_for_scan(_scan())
    ports = ForbiddenStartPort()

    with pytest.raises(ApplicationConflictError) as review:
        await _service(FakeAuthorizedAudits(aggregate), ports).start_authorized(
            aggregate.audit.value.id,
            _command(_digest("different-contract")),
            principal=PRINCIPAL,
            authorizer=AUTHORIZER,  # type: ignore[arg-type]
        )
    assert review.value.code == "audit_contract_review_required"

    queued = _aggregate_for_scan(
        aggregate.audit.value.transition_to(AuditLifecycleStatus.QUEUED, at=NOW),
    )
    with pytest.raises(ApplicationConflictError) as state:
        await _service(FakeAuthorizedAudits(queued), ports).start_authorized(
            queued.audit.value.id,
            _command(queued.contract.value.contract_digest),
            principal=PRINCIPAL,
            authorizer=AUTHORIZER,  # type: ignore[arg-type]
        )
    assert state.value.code == "audit_start_state_conflict"
    assert ports.calls == 0
