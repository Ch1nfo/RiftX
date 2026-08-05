from __future__ import annotations

import riftx.api.routes as api_routes
import riftx.api.schemas as api_schemas
import riftx.application.ports as application_ports
import riftx.application.services as application_services
import riftx.domain as domain
import riftx.persistence as persistence
from riftx.api.routes import audit_preflight_router
from riftx.api.schemas import (
    AuditPreflightCreateResponse,
    AuditPreflightJobResponse,
    AuditPreflightResultResponse,
    CreateAuditPreflightRequest,
)
from riftx.application.ports import (
    AuditPreflightDispatch,
    AuditPreflightOwnerBinding,
    AuditPreflightReconciliationCandidate,
    AuditPreflightRepository,
)
from riftx.application.services import (
    AuditPreflightApplicationService,
    AuditPreflightCreationResult,
)
from riftx.domain import (
    AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST,
    AuditPreflightDispatchEnvelope,
    AuditPreflightJob,
    AuditPreflightJobStatus,
    AuditPreflightResult,
    PreflightRequest,
)
from riftx.persistence import SQLAlchemyAuditPreflightRepository


def test_audit_preflight_public_packages_export_the_operator_contract() -> None:
    expected_exports = {
        api_routes: {"audit_preflight_router"},
        api_schemas: {
            "AuditPreflightCreateResponse",
            "AuditPreflightJobResponse",
            "AuditPreflightResultResponse",
            "CreateAuditPreflightRequest",
        },
        application_ports: {
            "AuditPreflightDispatch",
            "AuditPreflightOwnerBinding",
            "AuditPreflightReconciliationCandidate",
            "AuditPreflightRepository",
        },
        application_services: {
            "AuditPreflightApplicationService",
            "AuditPreflightAvailabilityCheck",
            "AuditPreflightCreationResult",
        },
        domain: {
            "AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY",
            "AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST",
            "AuditPreflightDispatchEnvelope",
            "AuditPreflightJob",
            "AuditPreflightJobStatus",
            "AuditPreflightResult",
            "PreflightRequest",
        },
        persistence: {"SQLAlchemyAuditPreflightRepository"},
    }

    for package, names in expected_exports.items():
        assert names <= set(package.__all__)

    assert audit_preflight_router.prefix == "/audits/preflight"
    assert AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY == "preflight_job_owner_v1"
    assert len(AUDIT_PREFLIGHT_OUTPUT_CONTRACT_DIGEST) == 64
    assert AuditPreflightJobStatus.PENDING.value == "pending"
    assert all(
        symbol is not None
        for symbol in (
            AuditPreflightCreateResponse,
            AuditPreflightJobResponse,
            AuditPreflightResultResponse,
            CreateAuditPreflightRequest,
            AuditPreflightDispatch,
            AuditPreflightOwnerBinding,
            AuditPreflightReconciliationCandidate,
            AuditPreflightRepository,
            AuditPreflightApplicationService,
            AuditPreflightCreationResult,
            AuditPreflightDispatchEnvelope,
            AuditPreflightJob,
            AuditPreflightResult,
            PreflightRequest,
            SQLAlchemyAuditPreflightRepository,
        )
    )
