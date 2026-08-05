from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from tests.unit.domain.test_audit_preflight_plan import (
    PLAN_CREATED,
    _codec,
    _succeeded_job,
)

from riftx.api.routes.audit_preflight import issue_audit_preflight_plan
from riftx.application.errors import (
    ApplicationConflictError,
    ResourceNotAccessibleError,
    ServiceUnavailableError,
)
from riftx.application.ports.audit_preflight import (
    AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION,
    AuditPreflightOwnerBinding,
)
from riftx.application.ports.audit_preflight_plan import (
    AuditPreflightPlanOwnerBinding,
)
from riftx.application.services.audit_preflight_plan import (
    AuditPreflightPlanApplicationService,
    AuditPreflightPlanIssuanceResult,
)
from riftx.domain import LocalPrincipal, OperatorCapability
from riftx.domain.audit_preflight import AuditPreflightJob
from riftx.domain.audit_preflight_plan import (
    AuditPreflightPlan,
    AuditPreflightPlanStatus,
    AuditPreflightTokenCodec,
)

PRINCIPAL = LocalPrincipal(
    id="operator-1",
    capabilities=frozenset(OperatorCapability),
)


class FakeAuthorizer:
    def __init__(self, scope_digest: str) -> None:
        self.scope_digest = scope_digest
        self.capabilities: list[OperatorCapability] = []

    def preflight_authorization_scope_digest(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> str:
        assert principal == PRINCIPAL
        self.capabilities.append(capability)
        return self.scope_digest


class FakePreflightRepository:
    def __init__(
        self,
        job: AuditPreflightJob,
        *,
        issuance_marker: str | None = AUDIT_PREFLIGHT_PLAN_ISSUANCE_SCHEMA_VERSION,
    ) -> None:
        self.job = job
        self.issuance_marker = issuance_marker
        self.owner_reads = 0
        self.full_reads = 0

    async def get_owner_binding(self, job_id: str) -> AuditPreflightOwnerBinding | None:
        self.owner_reads += 1
        if job_id != self.job.job_id:
            return None
        return AuditPreflightOwnerBinding(
            job_id=self.job.job_id,
            operator_principal_id=self.job.operator_principal_id,
            authorization_scope_digest=self.job.authorization_scope_digest,
            request_schema_version=self.job.request_schema_version,
            request_digest=self.job.request_digest,
            source_node_id=self.job.source_node_id,
            source_root_identity_digest=self.job.source_root_identity_digest,
            backend_id=self.job.backend_id,
            image_digest=self.job.image_digest,
            policy_digest=self.job.policy_digest,
            status=self.job.status,
            state_version=self.job.state_version,
            effect_owner_digest=self.job.effect_owner_digest,
            plan_issuance_schema_version=self.issuance_marker,
        )

    async def get(self, job_id: str) -> AuditPreflightJob | None:
        self.full_reads += 1
        return self.job if job_id == self.job.job_id else None


class FakePlanRepository:
    def __init__(self, plan: AuditPreflightPlan | None = None) -> None:
        self.plan = plan
        self.binding_reads = 0
        self.full_reads = 0
        self.create_calls = 0
        self.binding_override: AuditPreflightPlanOwnerBinding | None = None

    async def create(
        self,
        plan: AuditPreflightPlan,
    ) -> tuple[AuditPreflightPlan, bool]:
        self.create_calls += 1
        if self.plan is not None:
            return self.plan, False
        self.plan = plan
        return plan, True

    async def get_owner_binding_for_job(
        self,
        preflight_job_id: str,
    ) -> AuditPreflightPlanOwnerBinding | None:
        self.binding_reads += 1
        if self.binding_override is not None:
            return self.binding_override
        if self.plan is None or self.plan.preflight_job_id != preflight_job_id:
            return None
        return _plan_binding(self.plan)

    async def get(self, plan_id: str) -> AuditPreflightPlan | None:
        self.full_reads += 1
        if self.plan is None or self.plan.plan_id != plan_id:
            return None
        return self.plan


def _plan_binding(plan: AuditPreflightPlan) -> AuditPreflightPlanOwnerBinding:
    return AuditPreflightPlanOwnerBinding(
        plan_id=plan.plan_id,
        preflight_job_id=plan.preflight_job_id,
        operator_principal_id=plan.operator_principal_id,
        authorization_scope_digest=plan.authorization_scope_digest,
        plan_digest=plan.plan_digest,
        status=plan.status,
        state_version=plan.state_version,
        expires_at=plan.expires_at,
        reserved_audit_id=plan.reserved_audit_id,
        reserved_client_request_id=plan.reserved_client_request_id,
        consumed_audit_id=plan.consumed_audit_id,
    )


def _service(
    preflight: FakePreflightRepository,
    plans: FakePlanRepository,
    *,
    token_codec: AuditPreflightTokenCodec | None,
    feature_enabled: bool = True,
    clock: Any = lambda: PLAN_CREATED,
) -> AuditPreflightPlanApplicationService:
    return AuditPreflightPlanApplicationService(
        preflight_repository=preflight,  # type: ignore[arg-type]
        plan_repository=plans,  # type: ignore[arg-type]
        feature_enabled=feature_enabled,
        token_codec=token_codec,
        plan_ttl_seconds=900,
        id_factory=lambda: "preflight-plan-service-1",
        clock=clock,
    )


@pytest.mark.asyncio
async def test_issue_authorizes_and_fences_feature_before_any_repository_or_nonce() -> None:
    _request, job, _result = _succeeded_job()
    preflight = FakePreflightRepository(job)
    plans = FakePlanRepository()
    nonce_calls: list[int] = []
    codec = AuditPreflightTokenCodec(
        key_id="key-1",
        key=b"K" * 32,
        nonce_factory=lambda size: nonce_calls.append(size) or b"N" * size,
    )
    authorizer = FakeAuthorizer(job.authorization_scope_digest)

    with pytest.raises(ServiceUnavailableError) as captured:
        await _service(
            preflight,
            plans,
            token_codec=codec,
            feature_enabled=False,
        ).issue_authorized(job.job_id, principal=PRINCIPAL, authorizer=authorizer)

    assert captured.value.code == "audit_feature_disabled"
    assert authorizer.capabilities == [OperatorCapability.WRITE]
    assert preflight.owner_reads == preflight.full_reads == 0
    assert plans.binding_reads == plans.create_calls == 0
    assert nonce_calls == []


@pytest.mark.asyncio
async def test_issue_rejects_foreign_scope_and_historical_job_before_full_reads() -> None:
    _request, job, _result = _succeeded_job()
    foreign = FakePreflightRepository(job)
    plans = FakePlanRepository()
    with pytest.raises(ResourceNotAccessibleError):
        await _service(foreign, plans, token_codec=_codec()).issue_authorized(
            job.job_id,
            principal=PRINCIPAL,
            authorizer=FakeAuthorizer("0" * 64),
        )
    assert foreign.full_reads == 0
    assert plans.binding_reads == 0

    historical = FakePreflightRepository(job, issuance_marker=None)
    with pytest.raises(ApplicationConflictError) as captured:
        await _service(historical, plans, token_codec=_codec()).issue_authorized(
            job.job_id,
            principal=PRINCIPAL,
            authorizer=FakeAuthorizer(job.authorization_scope_digest),
        )
    assert captured.value.code == "audit_preflight_plan_unavailable"
    assert historical.full_reads == 0
    assert plans.binding_reads == 0


@pytest.mark.asyncio
async def test_issue_creates_once_and_restart_rederives_the_same_hidden_token() -> None:
    _request, job, _result = _succeeded_job()
    preflight = FakePreflightRepository(job)
    plans = FakePlanRepository()
    first = await _service(preflight, plans, token_codec=_codec()).issue_authorized(
        job.job_id,
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(job.authorization_scope_digest),
    )

    def forbidden_nonce(_size: int) -> bytes:
        raise AssertionError("an exact replay must use the persisted nonce")

    restarted_codec = AuditPreflightTokenCodec(
        key_id="preflight-key-1",
        key=b"K" * 32,
        nonce_factory=forbidden_nonce,
    )
    replay = await _service(
        preflight,
        plans,
        token_codec=restarted_codec,
    ).issue_authorized(
        job.job_id,
        principal=PRINCIPAL,
        authorizer=FakeAuthorizer(job.authorization_scope_digest),
    )

    assert first.created is True and first.replayed is False
    assert replay.created is False and replay.replayed is True
    assert replay.plan == first.plan
    assert replay.preflight_token == first.preflight_token
    assert plans.create_calls == 1
    assert first.preflight_token not in repr(first)


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["reserved", "consumed", "revoked", "expired"])
async def test_non_available_or_expired_plan_never_replays_a_token(lifecycle: str) -> None:
    _request, job, result = _succeeded_job()
    issue = AuditPreflightPlan.from_succeeded(
        job=job,
        result=result,
        restricted_request=_request,
        token_codec=_codec(),
        plan_id="preflight-plan-existing",
        created_at=PLAN_CREATED,
        expires_at=PLAN_CREATED + timedelta(minutes=15),
    )
    plan = issue.plan
    expected_code = "audit_preflight_plan_unavailable"
    clock_at = PLAN_CREATED
    if lifecycle in {"reserved", "consumed"}:
        plan = plan.reserve(
            audit_id="audit-1",
            client_request_id="223e4567-e89b-42d3-a456-426614174000",
            at=PLAN_CREATED + timedelta(minutes=1),
        )
    if lifecycle == "consumed":
        plan = plan.consume(
            audit_id="audit-1",
            start_request_id="323e4567-e89b-42d3-a456-426614174000",
            at=PLAN_CREATED + timedelta(minutes=2),
        )
    elif lifecycle == "revoked":
        plan = plan.revoke(
            reason_code="audit_policy_changed",
            at=PLAN_CREATED + timedelta(minutes=1),
        )
    elif lifecycle == "expired":
        expected_code = "audit_preflight_expired"
        clock_at = plan.expires_at

    def clock() -> datetime:
        return clock_at

    plans = FakePlanRepository(plan)
    with pytest.raises((ApplicationConflictError, ServiceUnavailableError)) as captured:
        await _service(
            FakePreflightRepository(job),
            plans,
            token_codec=None,
            clock=clock,
        ).issue_authorized(
            job.job_id,
            principal=PRINCIPAL,
            authorizer=FakeAuthorizer(job.authorization_scope_digest),
        )

    assert captured.value.code == expected_code
    assert plans.full_reads == 0


@pytest.mark.asyncio
async def test_available_replay_requires_its_retained_key_and_exact_binding() -> None:
    request, job, result = _succeeded_job()
    issue = AuditPreflightPlan.from_succeeded(
        job=job,
        result=result,
        restricted_request=request,
        token_codec=_codec(),
        plan_id="preflight-plan-existing",
        created_at=PLAN_CREATED,
        expires_at=PLAN_CREATED + timedelta(minutes=15),
    )
    plans = FakePlanRepository(issue.plan)
    preflight = FakePreflightRepository(job)

    with pytest.raises(ServiceUnavailableError) as missing_key:
        await _service(preflight, plans, token_codec=None).issue_authorized(
            job.job_id,
            principal=PRINCIPAL,
            authorizer=FakeAuthorizer(job.authorization_scope_digest),
        )
    assert missing_key.value.code == "audit_preflight_token_key_unavailable"

    plans.binding_override = replace(
        _plan_binding(issue.plan),
        plan_digest="0" * 64,
    )
    with pytest.raises(ServiceUnavailableError) as mismatch:
        await _service(preflight, plans, token_codec=_codec()).issue_authorized(
            job.job_id,
            principal=PRINCIPAL,
            authorizer=FakeAuthorizer(job.authorization_scope_digest),
        )
    assert mismatch.value.code == "audit_preflight_plan_mismatch"


@pytest.mark.asyncio
async def test_operator_route_returns_only_no_store_issuance_projection() -> None:
    request, job, result = _succeeded_job()
    issue = AuditPreflightPlan.from_succeeded(
        job=job,
        result=result,
        restricted_request=request,
        token_codec=_codec(),
        plan_id="preflight-plan-existing",
        created_at=PLAN_CREATED,
        expires_at=PLAN_CREATED + timedelta(minutes=15),
    )
    application_result = AuditPreflightPlanIssuanceResult(
        plan=issue.plan,
        preflight_token=issue.token,
        created=True,
    )

    class StubService:
        async def issue_authorized(self, *_: object, **__: object) -> Any:
            return application_result

    response = await issue_audit_preflight_plan(
        job.job_id,
        StubService(),  # type: ignore[arg-type]
        PRINCIPAL,
        FakeAuthorizer(job.authorization_scope_digest),
    )
    body = json.loads(response.body)

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert body == {
        "created": True,
        "replayed": False,
        "plan": {
            "id": issue.plan.plan_id,
            "digest": issue.plan.plan_digest,
            "status": AuditPreflightPlanStatus.AVAILABLE.value,
            "preflight_job_id": job.job_id,
            "expires_at": issue.plan.expires_at.isoformat().replace("+00:00", "Z"),
        },
        "preflight_token": issue.token,
    }
    assert issue.token not in repr(application_result)
