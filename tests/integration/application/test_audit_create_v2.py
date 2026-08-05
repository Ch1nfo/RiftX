from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.integration.persistence.test_audit_preflight_plan_repository import _issue_plan
from tests.unit.domain.test_audit_contract_v2 import _draft_budget

from riftx.application.ports import AuditEngagementScope
from riftx.application.services.audits import (
    AuditApplicationService,
    CreateAuditDraftV2,
)
from riftx.domain import (
    AnalysisProfile,
    AuditMode,
    LocalPrincipal,
    OperatorCapability,
    ValidationPolicy,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyAuditCreationUnitOfWork,
    SQLAlchemyAuditPreflightPlanRepository,
    SQLAlchemyAuditPreflightRepository,
)
from riftx.persistence.audit_preflight_plan import AuditPreflightPlanRecord
from riftx.persistence.orm import (
    AuditClientRequestRecord,
    AuditScanRecord,
    AuditSecurityContextBindingRecord,
    RunRecord,
)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


class _Authorizer:
    def authorized_engagement_scope(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> AuditEngagementScope:
        assert principal.id == "operator-1"
        assert capability is OperatorCapability.WRITE
        return AuditEngagementScope.profile_a()

    def preflight_authorization_scope_digest(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> str:
        assert principal.id == "operator-1"
        assert capability is OperatorCapability.WRITE
        return _digest("authorization")

    def draft_authorization_reference(
        self,
        principal: LocalPrincipal,
        *,
        capability: OperatorCapability,
    ) -> str:
        assert principal.id == "operator-1"
        assert capability is OperatorCapability.WRITE
        return _digest("draft-authorization")


def _command(token: str) -> CreateAuditDraftV2:
    return CreateAuditDraftV2(
        client_request_id="223e4567-e89b-42d3-a456-426614174000",
        preflight_token=token,
        project_name="RiftX v2 Audit",
        mode=AuditMode.STANDARD,
        analysis_profile=AnalysisProfile.DETERMINISTIC,
        model_profile=None,
        model_data_egress_mode="local_only",
        validation_policy=ValidationPolicy.STATIC_ONLY,
        baseline_audit_id=None,
        execution_node_id="local",
        required_sandbox_backend="linux_container",
        budget=_draft_budget(),
    )


async def test_create_v2_reserves_plan_and_persists_one_complete_draft(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-create-v2.db'}")
    await database.create_schema()
    jobs = SQLAlchemyAuditPreflightRepository(database.session_factory)
    plans = SQLAlchemyAuditPreflightPlanRepository(database.session_factory)
    try:
        issue = await _issue_plan(jobs)
        await plans.create(issue.plan)
        creation_uow = SQLAlchemyAuditCreationUnitOfWork(database.session_factory)
        service = AuditApplicationService(
            creation_uow=creation_uow,
            aggregate_repository=SQLAlchemyAuditAggregateReadRepository(
                database.session_factory
            ),
            feature_enabled=True,
            workspace_root=tmp_path / "workspaces",
            clock=lambda: issue.plan.created_at + timedelta(minutes=1),
        )
        principal = LocalPrincipal(
            id="operator-1",
            capabilities=frozenset(OperatorCapability),
        )

        created = await service.create_draft_v2_authorized(
            _command(issue.token),
            principal=principal,
            authorizer=_Authorizer(),  # type: ignore[arg-type]
        )
        replay = await service.create_draft_v2_authorized(
            _command(issue.token),
            principal=principal,
            authorizer=_Authorizer(),  # type: ignore[arg-type]
        )

        assert created.created is True
        assert replay.replayed is True
        assert replay.aggregate == created.aggregate
        assert created.aggregate.audit.value.started_at is None
        assert created.aggregate.contract.value.schema_version == "riftx.audit-contract/v2"
        async with database.session() as session:
            counts = {
                "plans": await session.scalar(
                    select(func.count()).select_from(AuditPreflightPlanRecord)
                ),
                "runs": await session.scalar(select(func.count()).select_from(RunRecord)),
                "audits": await session.scalar(select(func.count()).select_from(AuditScanRecord)),
                "bindings": await session.scalar(
                    select(func.count()).select_from(AuditSecurityContextBindingRecord)
                ),
                "requests": await session.scalar(
                    select(func.count()).select_from(AuditClientRequestRecord)
                ),
            }
            plan = await session.get(AuditPreflightPlanRecord, issue.plan.plan_id)
        assert counts == {
            "plans": 1,
            "runs": 1,
            "audits": 1,
            "bindings": 1,
            "requests": 1,
        }
        assert plan is not None
        assert plan.status == "reserved"
        assert plan.reserved_audit_id == created.aggregate.audit.value.id
    finally:
        await database.dispose()


@pytest.mark.parametrize(
    "failure_stage",
    [
        "after_engagement",
        "after_project",
        "after_plan_reservation",
        "after_run",
        "after_run_event",
        "after_contract",
        "after_scan",
        "after_security_context_binding",
        "after_audit_event",
        "after_client_request",
    ],
)
async def test_create_v2_failure_injection_rolls_back_plan_and_aggregate(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{tmp_path / f'audit-create-v2-{failure_stage}.db'}"
    )
    await database.create_schema()
    jobs = SQLAlchemyAuditPreflightRepository(database.session_factory)
    plans = SQLAlchemyAuditPreflightPlanRepository(database.session_factory)

    def failpoint(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError("injected AUD-201 create failure")

    try:
        issue = await _issue_plan(jobs, job_id=f"job-{failure_stage}")
        await plans.create(issue.plan)
        service = AuditApplicationService(
            creation_uow=SQLAlchemyAuditCreationUnitOfWork(
                database.session_factory,
                creation_failpoint=failpoint,
            ),
            aggregate_repository=SQLAlchemyAuditAggregateReadRepository(
                database.session_factory
            ),
            feature_enabled=True,
            workspace_root=tmp_path / "workspaces",
            clock=lambda: issue.plan.created_at + timedelta(minutes=1),
        )
        principal = LocalPrincipal(
            id="operator-1",
            capabilities=frozenset(OperatorCapability),
        )

        with pytest.raises(RuntimeError, match="injected AUD-201"):
            await service.create_draft_v2_authorized(
                _command(issue.token),
                principal=principal,
                authorizer=_Authorizer(),  # type: ignore[arg-type]
            )

        persisted_plan = await plans.get(issue.plan.plan_id)
        assert persisted_plan == issue.plan
        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(RunRecord)) == 0
            assert (
                await session.scalar(select(func.count()).select_from(AuditScanRecord))
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(AuditSecurityContextBindingRecord)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(AuditClientRequestRecord)
                )
                == 0
            )
    finally:
        await database.dispose()
