from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.integration.application.test_audit_create_v2 import (
    _Authorizer,
    _command,
    _digest,
)
from tests.integration.persistence.test_audit_preflight_plan_repository import _issue_plan

from riftx.application.errors import ApplicationConflictError
from riftx.application.ports import (
    AuditAuthorizationBinding,
    AuditEngagementScope,
    AuditStartAdmissionRequest,
    AuditStartRevalidationDisposition,
    AuditStartRevalidationProof,
    AuditStartRevalidationRequest,
)
from riftx.application.services.audit_start import (
    AuditStartApplicationService,
    StartAudit,
)
from riftx.application.services.audits import AuditApplicationService
from riftx.domain import AuditStartIntent, LocalPrincipal, OperatorCapability
from riftx.persistence import (
    Database,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyAuditCreationUnitOfWork,
    SQLAlchemyAuditPreflightPlanRepository,
    SQLAlchemyAuditPreflightRepository,
)
from riftx.persistence.audit_preflight_plan import AuditPreflightPlanRecord
from riftx.persistence.orm import (
    AuditScanRecord,
    AuditStartIntentRecord,
    RunEventRecord,
    RunRecord,
)

START_REQUEST_ID = "323e4567-e89b-42d3-a456-426614174000"


class _StartAuthorizer(_Authorizer):
    def __init__(self) -> None:
        self.capabilities: list[OperatorCapability] = []

    def authorized_engagement_scope(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> AuditEngagementScope:
        self.capabilities.append(capability)
        return super().authorized_engagement_scope(principal, capability=capability)

    def require_audit_binding(
        self,
        principal: LocalPrincipal,
        binding: AuditAuthorizationBinding,
        *,
        capability: OperatorCapability,
    ) -> None:
        assert principal.id == "operator-1"
        assert binding.requested_audit_id == binding.audit_id
        self.capabilities.append(capability)


class _ForbiddenStartPorts:
    def __init__(self) -> None:
        self.revalidation_calls = 0
        self.admission_calls = 0

    async def revalidate(self, request: object) -> object:
        self.revalidation_calls += 1
        raise AssertionError("preflight-bound draft must not revalidate source")

    async def admit(self, request: object) -> object:
        self.admission_calls += 1
        raise AssertionError("preflight-bound draft must not open Start UoW")


async def test_current_v2_start_rejects_without_consuming_or_writing_any_effect(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-start-closed.db'}")
    await database.create_schema()
    jobs = SQLAlchemyAuditPreflightRepository(database.session_factory)
    plans = SQLAlchemyAuditPreflightPlanRepository(database.session_factory)
    try:
        issue = await _issue_plan(jobs)
        await plans.create(issue.plan)
        aggregates = SQLAlchemyAuditAggregateReadRepository(database.session_factory)
        audits = AuditApplicationService(
            creation_uow=SQLAlchemyAuditCreationUnitOfWork(database.session_factory),
            aggregate_repository=aggregates,
            feature_enabled=True,
            workspace_root=tmp_path / "workspaces",
            clock=lambda: issue.plan.created_at + timedelta(minutes=1),
        )
        principal = LocalPrincipal(
            id="operator-1",
            capabilities=frozenset(OperatorCapability),
        )
        create_authorizer = _Authorizer()
        created = await audits.create_draft_v2_authorized(
            _command(issue.token),
            principal=principal,
            authorizer=create_authorizer,  # type: ignore[arg-type]
        )
        start_authorizer = _StartAuthorizer()
        ports = _ForbiddenStartPorts()
        service = AuditStartApplicationService(
            audits=audits,
            revalidation_port=ports,  # type: ignore[arg-type]
            admission_uow=ports,  # type: ignore[arg-type]
            feature_enabled=True,
        )

        with pytest.raises(ApplicationConflictError) as captured:
            await service.start_authorized(
                created.aggregate.audit.value.id,
                StartAudit(
                    start_request_id=START_REQUEST_ID,
                    reviewed_contract_digest=created.aggregate.contract.value.contract_digest,
                ),
                principal=principal,
                authorizer=start_authorizer,  # type: ignore[arg-type]
            )

        assert captured.value.code == "audit_start_capability_unavailable"
        assert captured.value.details == {
            "contract_stage": "preflight_bound_draft",
            "missing_capabilities": [
                "analysis_backend_prepare",
                "detector_registry",
                "scope_ledger",
                "snapshot_materializer",
                "snapshot_mount",
                "snapshot_store",
                "start_delivery",
            ],
        }
        assert start_authorizer.capabilities == [OperatorCapability.HOST_EXECUTE]
        assert ports.revalidation_calls == ports.admission_calls == 0

        async with database.session() as session:
            plan = await session.get(AuditPreflightPlanRecord, issue.plan.plan_id)
            scan = await session.get(AuditScanRecord, created.aggregate.audit.value.id)
            run = await session.get(RunRecord, created.aggregate.run.id)
            intent_count = await session.scalar(
                select(func.count()).select_from(AuditStartIntentRecord)
            )
            event_count = await session.scalar(select(func.count()).select_from(RunEventRecord))
        assert plan is not None and plan.status == "reserved"
        assert plan.consumed_audit_id is None
        assert plan.consumed_start_request_id is None
        assert scan is not None and scan.lifecycle_status == "draft"
        assert run is not None and run.status == "created"
        assert intent_count == 0
        assert event_count == 2

        reserved_plan = await plans.get(issue.plan.plan_id)
        assert reserved_plan is not None
        contract = created.aggregate.contract.value.contract()
        revalidation_request = AuditStartRevalidationRequest(
            audit_id=created.aggregate.audit.value.id,
            run_id=created.aggregate.run.id,
            start_request_id=START_REQUEST_ID,
            preflight_plan_id=reserved_plan.plan_id,
            preflight_plan_digest=reserved_plan.plan_digest,
            contract_digest=created.aggregate.contract.value.contract_digest,
            security_context_id=contract.security_context_bundle_id,
            security_context_digest=contract.security_context_bundle_digest,
            operator_principal_id=reserved_plan.operator_principal_id,
            authorization_scope_digest=reserved_plan.authorization_scope_digest,
            source_node_id=reserved_plan.source_node_id,
            source_root_identity_digest=reserved_plan.source_root_identity_digest,
            repository_identity_digest=reserved_plan.repository_identity_digest,
            expected_content_identity_digest=reserved_plan.content_identity_digest,
            source_ingest_backend_id=reserved_plan.backend_id,
            source_ingest_image_digest=reserved_plan.image_digest,
            source_ingest_policy_digest=reserved_plan.policy_digest,
            source_repository_path=reserved_plan.target.repository_path,
            requested_at=issue.plan.created_at + timedelta(minutes=2),
            expires_at=issue.plan.created_at + timedelta(minutes=2, seconds=30),
        )
        revalidation_proof = AuditStartRevalidationProof(
            request_digest=revalidation_request.request_digest,
            disposition=AuditStartRevalidationDisposition.MATCHED,
            reason_code="source_content_matched",
            observed_content_identity_digest=reserved_plan.content_identity_digest,
            proof_digest=_digest("invented-start-proof"),
            issued_at=revalidation_request.requested_at + timedelta(seconds=1),
            expires_at=revalidation_request.expires_at,
        )
        intent = AuditStartIntent(
            audit_id=created.aggregate.audit.value.id,
            run_id=created.aggregate.run.id,
            start_request_id=START_REQUEST_ID,
            contract_digest=created.aggregate.contract.value.contract_digest,
            workflow_id=created.aggregate.audit.value.temporal_workflow_id,
            task_queue="riftx-code-audit",
            created_at=revalidation_request.requested_at + timedelta(seconds=2),
            updated_at=revalidation_request.requested_at + timedelta(seconds=2),
        )
        with pytest.raises(ValueError, match="inconsistent authoritative bindings"):
            AuditStartAdmissionRequest(
                aggregate=created.aggregate,
                plan=reserved_plan,
                start_request_id=START_REQUEST_ID,
                reviewed_contract_digest=(
                    created.aggregate.contract.value.contract_digest
                ),
                expected_audit_state_version=created.aggregate.audit.state_version,
                expected_plan_state_version=reserved_plan.state_version,
                revalidation_request=revalidation_request,
                revalidation_proof=revalidation_proof,
                intent=intent,
                occurred_at=revalidation_request.requested_at + timedelta(seconds=2),
            )
    finally:
        await database.dispose()
