"""Current-version Code Audit Start admission remains deliberately fail-closed."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from uuid import UUID

from riftx.application.errors import (
    ApplicationConflictError,
    ServiceUnavailableError,
)
from riftx.application.ports.audit_start import (
    AuditAuthorizedAggregateReader,
    AuditStartAdmissionProjection,
    AuditStartAdmissionUnitOfWork,
    AuditStartRevalidationPort,
)
from riftx.application.ports.audits import AuditObjectAuthorizer
from riftx.domain import (
    AuditLifecycleStatus,
    LocalPrincipal,
    OperatorCapability,
    RunStatus,
)
from riftx.domain.audit_contract_v2 import (
    AUDIT_CONTRACT_V2_STAGE,
    AuditContractRecordV2,
)

_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@+~-"
)


@dataclass(frozen=True, slots=True)
class StartAudit:
    start_request_id: str
    reviewed_contract_digest: str


class AuditStartApplicationService:
    """Authorize Start and reject every current non-start-ready contract without effects."""

    def __init__(
        self,
        *,
        audits: AuditAuthorizedAggregateReader,
        revalidation_port: AuditStartRevalidationPort,
        admission_uow: AuditStartAdmissionUnitOfWork,
        feature_enabled: bool,
    ) -> None:
        self._audits = audits
        self._revalidation_port = revalidation_port
        self._admission_uow = admission_uow
        self._feature_enabled = bool(feature_enabled)

    async def start_authorized(
        self,
        audit_id: str,
        command: StartAudit,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditStartAdmissionProjection:
        """Reject before source revalidation/UoW until a start-ready Contract exists."""

        self._require_enabled()
        _validate_start_request(audit_id, command)
        aggregate = await self._audits.get_authorized(
            audit_id,
            principal=principal,
            authorizer=authorizer,
            capability=OperatorCapability.HOST_EXECUTE,
        )
        scan = aggregate.audit.value
        contract_record = aggregate.contract.value
        if not hmac.compare_digest(
            contract_record.contract_digest,
            command.reviewed_contract_digest,
        ):
            raise ApplicationConflictError(
                "audit_contract_review_required",
                "The reviewed Code Audit contract no longer matches",
            ) from None
        if (
            scan.lifecycle_status is not AuditLifecycleStatus.DRAFT
            or aggregate.run.status is not RunStatus.CREATED
        ):
            raise ApplicationConflictError(
                "audit_start_state_conflict",
                "The Code Audit is not in a startable draft state",
            ) from None
        if not isinstance(contract_record, AuditContractRecordV2):
            raise ApplicationConflictError(
                "audit_start_not_eligible",
                "This historical Code Audit contract can never be started",
            ) from None
        contract = contract_record.contract()
        if (
            contract.contract_stage == AUDIT_CONTRACT_V2_STAGE
            or not contract.start_eligible
        ):
            raise ApplicationConflictError(
                "audit_start_capability_unavailable",
                "The Code Audit contract is not eligible for Start",
                details={
                    "contract_stage": contract.contract_stage,
                    "missing_capabilities": list(
                        contract.execution_readiness.missing_capabilities
                    ),
                },
            ) from None

        # No currently accepted Contract schema can reach this point. Keeping the
        # future ports injected makes any accidental widening observable in tests;
        # a start-ready schema must add the exact Plan/Binding-to-proof builder
        # before either port is invoked.
        _ = self._revalidation_port, self._admission_uow
        raise ServiceUnavailableError(
            "audit_start_admission_unavailable",
            "Code Audit Start admission is not implemented for this contract schema",
        )

    def _require_enabled(self) -> None:
        if not self._feature_enabled:
            raise ServiceUnavailableError(
                "feature_disabled",
                "RiftX Code Audit is disabled",
            )


def _validate_start_request(audit_id: str, command: StartAudit) -> None:
    if (
        not isinstance(audit_id, str)
        or not 1 <= len(audit_id) <= 128
        or any(character not in _ID_CHARACTERS for character in audit_id)
        or not isinstance(command, StartAudit)
    ):
        raise ApplicationConflictError(
            "audit_start_request_invalid",
            "The Code Audit Start request is invalid",
        ) from None
    try:
        request_id = UUID(command.start_request_id)
    except (AttributeError, TypeError, ValueError):
        request_id = None
    if (
        request_id is None
        or request_id.int == 0
        or str(request_id) != command.start_request_id
        or not isinstance(command.reviewed_contract_digest, str)
        or len(command.reviewed_contract_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in command.reviewed_contract_digest
        )
    ):
        raise ApplicationConflictError(
            "audit_start_request_invalid",
            "The Code Audit Start request is invalid",
        ) from None


__all__ = [
    "AuditStartApplicationService",
    "StartAudit",
]
