"""Application service for durable RiftX Code Audit drafts and control admission."""

from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn, Self
from uuid import UUID

from riftx.application.errors import (
    ApplicationConflictError,
    AuditIdempotencyConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    ResourceNotAccessibleError,
    ServiceUnavailableError,
)
from riftx.application.ports import (
    AuditAggregate,
    AuditAggregateReadRepository,
    AuditCreationUnitOfWork,
    AuditDraftCreationEnvelope,
    AuditDraftCreationEnvelopeV2,
    AuditEngagementScope,
    AuditObjectAuthorizer,
)
from riftx.domain import (
    AUDIT_CLIENT_REQUEST_SCHEMA_VERSION,
    AUDIT_CLIENT_REQUEST_V2_SCHEMA_VERSION,
    AnalysisProfile,
    ApprovalMode,
    AuditClientRequest,
    AuditContract,
    AuditContractRecord,
    AuditLifecycleStatus,
    AuditMode,
    AuditProject,
    AuditPurpose,
    AuditRunStateMappingPolicy,
    AuditScan,
    AuditTerminalOutcome,
    Engagement,
    LocalPrincipal,
    Objective,
    OperatorCapability,
    Run,
    RunEvent,
    RunKind,
    RunStatus,
    Scope,
    ValidationPolicy,
)
from riftx.domain.audit_contract_v2 import (
    AUDIT_CONTRACT_V2_STAGE,
    AuditContractRecordV2,
    AuditContractV2,
    AuditDraftBudgetV2,
)
from riftx.domain.audit_preflight_plan import (
    AuditPreflightPlan,
    AuditPreflightPlanStatus,
    audit_preflight_token_hash,
)
from riftx.domain.base import new_id, utc_now

_REQUEST_DIGEST_DOMAIN = AUDIT_CLIENT_REQUEST_SCHEMA_VERSION
_REQUEST_V2_DIGEST_DOMAIN = AUDIT_CLIENT_REQUEST_V2_SCHEMA_VERSION
_MAX_PAGE_SIZE = 200

type AuditIdFactory = Callable[[], str]
type AuditClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class AuditContractBlueprint:
    """Validated contract fields whose server-owned Audit/Project IDs are rebound."""

    template: AuditContract = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "template", AuditContract.model_validate(self.template))

    @classmethod
    def from_contract(cls, contract: AuditContract) -> Self:
        return cls(template=contract)

    def request_payload(self) -> dict[str, object]:
        return self.template.model_dump(
            mode="json",
            exclude={"audit_id", "project_id"},
        )

    def materialize(self, *, audit_id: str, project_id: str) -> AuditContract:
        payload = self.template.model_dump(mode="python")
        payload.update(audit_id=audit_id, project_id=project_id)
        return AuditContract.model_validate(payload)


@dataclass(frozen=True, slots=True)
class CreateAuditDraft:
    client_request_id: str
    project_name: str
    repository_identity_digest: str
    authorization_reference: str
    contract: AuditContractBlueprint
    engagement_id: str | None = None
    default_branch: str | None = None


@dataclass(frozen=True, slots=True)
class CreateAuditDraftV2:
    """Caller-owned AUD-201 preferences; every proof comes from the Plan."""

    client_request_id: str
    preflight_token: str = field(repr=False)
    project_name: str
    mode: AuditMode
    analysis_profile: AnalysisProfile
    model_profile: None
    model_data_egress_mode: Literal["local_only"]
    validation_policy: ValidationPolicy
    baseline_audit_id: None
    execution_node_id: Literal["local"]
    required_sandbox_backend: str
    budget: AuditDraftBudgetV2
    engagement_id: str | None = None


@dataclass(frozen=True, slots=True)
class AuditDraftResult:
    aggregate: AuditAggregate
    created: bool

    @property
    def replayed(self) -> bool:
        return not self.created


class AuditControlAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


class AuditControlDisposition(StrEnum):
    TRANSITION = "transition"
    RECONCILE = "reconcile"
    ALREADY_SATISFIED = "already_satisfied"
    SAFETY_ONLY = "safety_only"


class AuditControlEffect(StrEnum):
    NONE = "none"
    PAUSE_WORKFLOW_THEN_PROJECT = "pause_workflow_then_project"
    RECONCILE_PAUSE = "reconcile_pause"
    RESUME_WORKFLOW_THEN_PROJECT = "resume_workflow_then_project"
    FENCE_NEW_EFFECTS_AND_STOP = "fence_new_effects_and_stop"
    RECONCILE_CANCEL_STOP = "reconcile_cancel_stop"
    SAFETY_STOP_SWEEP_ONLY = "safety_stop_sweep_only"


@dataclass(frozen=True, slots=True)
class AuditControlPlan:
    """Read-only admission result consumed later by the kind-aware control router."""

    operation: AuditControlAction
    disposition: AuditControlDisposition
    required_effect: AuditControlEffect
    audit_id: str
    run_id: str
    expected_audit_state_version: int
    current_audit_lifecycle: AuditLifecycleStatus
    current_run_status: RunStatus
    reason_code: str
    target_audit_lifecycle: AuditLifecycleStatus | None = None
    target_run_status: RunStatus | None = None

    @property
    def action(self) -> AuditControlAction:
        """Compatibility alias for callers written before ADR-0003 was frozen."""

        return self.operation

    @property
    def audit_state_version(self) -> int:
        return self.expected_audit_state_version

    @property
    def current_audit_status(self) -> AuditLifecycleStatus:
        return self.current_audit_lifecycle

    @property
    def target_audit_status(self) -> AuditLifecycleStatus | None:
        return self.target_audit_lifecycle


class _AuditDraftBuilder:
    def __init__(
        self,
        command: CreateAuditDraft,
        *,
        authorized_engagement_scope: AuditEngagementScope,
        workspace_root: Path,
        id_factory: AuditIdFactory,
        clock: AuditClock,
    ) -> None:
        self._command = _validate_create_command(command)
        self._authorized_engagement_scope = authorized_engagement_scope
        self._workspace_root = workspace_root
        self._id_factory = id_factory
        self._clock = clock
        self._created_at: datetime | None = None
        self._request_digest = _request_digest(self._command)
        _validate_workspace_separation(
            workspace_root,
            self._command.contract.template.source_target.repository_path,
        )

    @property
    def client_request_id(self) -> str:
        return self._command.client_request_id

    @property
    def request_digest(self) -> str:
        return self._request_digest

    @property
    def repository_identity_digest(self) -> str:
        return self._command.repository_identity_digest

    @property
    def requested_engagement_id(self) -> str | None:
        return self._command.engagement_id

    @property
    def authorization_reference(self) -> str:
        return self._command.authorization_reference

    @property
    def authorized_engagement_scope(self) -> AuditEngagementScope:
        return self._authorized_engagement_scope

    @property
    def workspace_root(self) -> str:
        return str(self._workspace_root)

    @property
    def source_repository_path(self) -> str:
        return self._command.contract.template.source_target.repository_path

    @property
    def created_at(self) -> datetime:
        if self._created_at is None:
            self._created_at = self._clock()
            if self._created_at.utcoffset() is None:
                raise ValueError("Audit creation clock must return an aware datetime")
        return self._created_at

    def build_engagement(self) -> Engagement:
        return Engagement(
            id=self._id_factory(),
            name=self._command.project_name,
            description="RiftX Code Audit engagement",
            authorization_reference=self.authorization_reference,
            created_at=self.created_at,
            updated_at=self.created_at,
        )

    def build_project(self, engagement: Engagement) -> AuditProject:
        return AuditProject(
            id=self._id_factory(),
            engagement_id=engagement.id,
            display_name=self._command.project_name,
            repository_identity_digest=self.repository_identity_digest,
            default_branch=self._command.default_branch,
            created_at=self.created_at,
            updated_at=self.created_at,
        )

    def build(
        self,
        project: AuditProject,
        engagement: Engagement,
    ) -> AuditDraftCreationEnvelope:
        audit_id = self._id_factory()
        run_id = self._id_factory()
        contract = self._command.contract.materialize(
            audit_id=audit_id,
            project_id=project.id,
        )
        contract_record = AuditContractRecord.from_contract(
            contract,
            contract_id=self._id_factory(),
            created_at=self.created_at,
        )
        workflow_id = f"riftx-code-audit-{audit_id}"
        run = Run(
            id=run_id,
            engagement_id=engagement.id,
            node_id=contract.execution_selection.selected_node_id,
            kind=RunKind.CODE_AUDIT,
            objective=Objective(description=f"Code Audit: {project.display_name}"),
            scope=Scope(),
            status=RunStatus.CREATED,
            approval_mode=ApprovalMode.MANUAL,
            model_profile=contract.model_profile,
            workspace_path=str(self._workspace_root / audit_id),
            temporal_workflow_id=workflow_id,
            created_at=self.created_at,
        )
        scan = AuditScan(
            id=audit_id,
            run_id=run_id,
            project_id=project.id,
            contract_id=contract_record.contract_id,
            baseline_audit_id=contract.baseline_audit_id,
            purpose=AuditPurpose.PRIMARY,
            mode=contract.mode,
            analysis_profile=contract.analysis_profile,
            model_profile=contract.model_profile,
            selected_node_id=contract.execution_selection.selected_node_id,
            required_backend_id=contract.execution_selection.required_backend_id,
            policy_digest=contract.policy_digest,
            budget_digest=contract.budget.digest,
            config_digest=contract.config_digest,
            contract_digest=contract.contract_digest,
            temporal_workflow_id=workflow_id,
            created_at=self.created_at,
        )
        run_event = RunEvent(
            id=self._id_factory(),
            run_id=run_id,
            sequence=1,
            event_type="run.created",
            payload={"status": RunStatus.CREATED.value},
            created_at=self.created_at,
        )
        audit_event = RunEvent(
            id=self._id_factory(),
            run_id=run_id,
            sequence=2,
            event_type="audit.created",
            payload={
                "audit_id": audit_id,
                "project_id": project.id,
                "lifecycle_status": scan.lifecycle_status.value,
                "mode": scan.mode.value,
                "analysis_profile": scan.analysis_profile.value,
                "contract_digest": scan.contract_digest,
            },
            created_at=self.created_at,
        )
        client_request = AuditClientRequest(
            client_request_id=self.client_request_id,
            request_digest=self.request_digest,
            audit_id=audit_id,
            run_id=run_id,
            project_id=project.id,
            engagement_id=engagement.id,
            contract_id=contract_record.contract_id,
            contract_digest=contract_record.contract_digest,
            temporal_workflow_id=workflow_id,
            created_at=self.created_at,
        )
        return AuditDraftCreationEnvelope(
            engagement=engagement,
            project=project,
            run=run,
            run_created_event=run_event,
            audit=scan,
            contract=contract_record,
            audit_created_event=audit_event,
            client_request=client_request,
        )


class _AuditDraftV2Builder:
    """Pure v2 factory bound to server authorization and a durable Plan."""

    def __init__(
        self,
        command: CreateAuditDraftV2,
        *,
        operator_principal_id: str,
        authorization_scope_digest: str,
        authorization_reference: str,
        authorized_engagement_scope: AuditEngagementScope,
        workspace_root: Path,
        id_factory: AuditIdFactory,
        clock: AuditClock,
    ) -> None:
        self._command = _validate_create_v2_command(command)
        if (
            not isinstance(operator_principal_id, str)
            or operator_principal_id != operator_principal_id.strip()
            or not operator_principal_id
            or len(operator_principal_id) > 128
            or _contains_control_character(operator_principal_id)
        ):
            raise ValueError("operator principal identity is invalid")
        _require_lower_digest(authorization_scope_digest, "authorization scope")
        _require_lower_digest(authorization_reference, "authorization reference")
        if not isinstance(authorized_engagement_scope, AuditEngagementScope):
            raise TypeError("authorized_engagement_scope must be AuditEngagementScope")
        self._operator_principal_id = operator_principal_id
        self._authorization_scope_digest = authorization_scope_digest
        self._authorization_reference = authorization_reference
        self._authorized_engagement_scope = authorized_engagement_scope
        self._workspace_root = workspace_root
        self._id_factory = id_factory
        self._clock = clock
        self._audit_id = id_factory()
        self._created_at: datetime | None = None
        self._preflight_token_hash = audit_preflight_token_hash(command.preflight_token)

    @property
    def client_request_id(self) -> str:
        return self._command.client_request_id

    @property
    def preflight_token(self) -> str:
        return self._command.preflight_token

    @property
    def preflight_token_hash(self) -> str:
        return self._preflight_token_hash

    @property
    def operator_principal_id(self) -> str:
        return self._operator_principal_id

    @property
    def authorization_scope_digest(self) -> str:
        return self._authorization_scope_digest

    @property
    def authorization_reference(self) -> str:
        return self._authorization_reference

    @property
    def authorized_engagement_scope(self) -> AuditEngagementScope:
        return self._authorized_engagement_scope

    @property
    def requested_engagement_id(self) -> str | None:
        return self._command.engagement_id

    @property
    def workspace_root(self) -> str:
        return str(self._workspace_root)

    @property
    def audit_id(self) -> str:
        return self._audit_id

    @property
    def created_at(self) -> datetime:
        if self._created_at is None:
            self._created_at = self._clock()
            if self._created_at.utcoffset() is None:
                raise ValueError("Audit creation clock must return an aware datetime")
        return self._created_at

    def validate_plan(self, plan: AuditPreflightPlan) -> None:
        if not isinstance(plan, AuditPreflightPlan):
            raise TypeError("plan must be an AuditPreflightPlan")
        command = self._command
        if (
            plan.operator_principal_id != self.operator_principal_id
            or not hmac.compare_digest(
                plan.authorization_scope_digest,
                self.authorization_scope_digest,
            )
            or plan.target.mode is not command.mode
            or plan.target.source_node_id != command.execution_node_id
            or plan.target.source_ingest_backend_id != command.required_sandbox_backend
            or command.mode is not AuditMode.STANDARD
            or command.analysis_profile is not AnalysisProfile.DETERMINISTIC
            or command.model_profile is not None
            or command.model_data_egress_mode != "local_only"
            or command.validation_policy is not ValidationPolicy.STATIC_ONLY
            or command.baseline_audit_id is not None
            or plan.security_context_id != "riftx.audit-empty-security-context/v1"
        ):
            raise ValueError("caller preferences do not match the authoritative Preflight Plan")
        _validate_workspace_separation(
            self._workspace_root,
            plan.target.repository_path,
        )

    def request_digest_for(self, plan: AuditPreflightPlan) -> str:
        self.validate_plan(plan)
        payload = {
            "authorization_domain_digest": self.authorization_reference,
            "preflight_plan_id": plan.plan_id,
            "preflight_plan_digest": plan.plan_digest,
            "security_context_id": plan.security_context_id,
            "security_context_digest": plan.security_context_digest,
            "contract_stage": AUDIT_CONTRACT_V2_STAGE,
            "caller_preferences": _create_v2_caller_preferences(self._command),
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(
            _REQUEST_V2_DIGEST_DOMAIN.encode("ascii") + b"\0" + canonical
        ).hexdigest()

    def build_engagement(self) -> Engagement:
        return Engagement(
            id=self._id_factory(),
            name=self._command.project_name,
            description="RiftX Code Audit engagement",
            authorization_reference=self.authorization_reference,
            created_at=self.created_at,
            updated_at=self.created_at,
        )

    def build_project(
        self,
        engagement: Engagement,
        plan: AuditPreflightPlan,
    ) -> AuditProject:
        self.validate_plan(plan)
        return AuditProject(
            id=self._id_factory(),
            engagement_id=engagement.id,
            display_name=self._command.project_name,
            repository_identity_digest=plan.repository_identity_digest,
            default_branch=None,
            created_at=self.created_at,
            updated_at=self.created_at,
        )

    def build(
        self,
        project: AuditProject,
        engagement: Engagement,
        reserved_plan: AuditPreflightPlan,
        *,
        request_digest: str,
    ) -> AuditDraftCreationEnvelopeV2:
        self.validate_plan(reserved_plan)
        if (
            reserved_plan.status is not AuditPreflightPlanStatus.RESERVED
            or reserved_plan.reserved_audit_id != self.audit_id
            or reserved_plan.reserved_client_request_id != self.client_request_id
        ):
            raise ValueError("Preflight Plan is not reserved for this Audit request")
        expected_request_digest = self.request_digest_for(reserved_plan)
        if not hmac.compare_digest(request_digest, expected_request_digest):
            raise ValueError("Audit v2 request digest does not match its Plan binding")

        contract = AuditContractV2.from_preflight_plan(
            audit_id=self.audit_id,
            project_id=project.id,
            plan=reserved_plan,
            budget=self._command.budget,
        )
        contract_record = AuditContractRecordV2.from_contract(
            contract,
            contract_id=self._id_factory(),
            created_at=self.created_at,
        )
        run_id = self._id_factory()
        workflow_id = f"riftx-code-audit-{self.audit_id}"
        run = Run(
            id=run_id,
            engagement_id=engagement.id,
            node_id=reserved_plan.source_node_id,
            kind=RunKind.CODE_AUDIT,
            objective=Objective(description=f"Code Audit: {project.display_name}"),
            scope=Scope(),
            status=RunStatus.CREATED,
            approval_mode=ApprovalMode.MANUAL,
            model_profile=None,
            workspace_path=str(self._workspace_root / self.audit_id),
            temporal_workflow_id=workflow_id,
            created_at=self.created_at,
        )
        scan = AuditScan(
            id=self.audit_id,
            run_id=run_id,
            project_id=project.id,
            contract_id=contract_record.contract_id,
            baseline_audit_id=None,
            purpose=AuditPurpose.PRIMARY,
            mode=contract.mode,
            analysis_profile=contract.analysis_profile,
            model_profile=None,
            selected_node_id=reserved_plan.source_node_id,
            required_backend_id=None,
            policy_digest=None,
            budget_digest=contract.budget.budget_digest,
            config_digest=None,
            contract_digest=contract.contract_digest,
            temporal_workflow_id=workflow_id,
            created_at=self.created_at,
        )
        run_event = RunEvent(
            id=self._id_factory(),
            run_id=run_id,
            sequence=1,
            event_type="run.created",
            payload={"status": RunStatus.CREATED.value},
            created_at=self.created_at,
        )
        audit_event = RunEvent(
            id=self._id_factory(),
            run_id=run_id,
            sequence=2,
            event_type="audit.created",
            payload={
                "audit_id": self.audit_id,
                "project_id": project.id,
                "lifecycle_status": scan.lifecycle_status.value,
                "mode": scan.mode.value,
                "analysis_profile": scan.analysis_profile.value,
                "contract_digest": scan.contract_digest,
            },
            created_at=self.created_at,
        )
        client_request = AuditClientRequest(
            client_request_id=self.client_request_id,
            request_schema_version=AUDIT_CLIENT_REQUEST_V2_SCHEMA_VERSION,
            request_digest=request_digest,
            preflight_plan_id=reserved_plan.plan_id,
            preflight_plan_digest=reserved_plan.plan_digest,
            security_context_id=reserved_plan.security_context_id,
            security_context_digest=reserved_plan.security_context_digest,
            contract_stage=AUDIT_CONTRACT_V2_STAGE,
            audit_id=self.audit_id,
            run_id=run_id,
            project_id=project.id,
            engagement_id=engagement.id,
            contract_id=contract_record.contract_id,
            contract_digest=contract_record.contract_digest,
            temporal_workflow_id=workflow_id,
            created_at=self.created_at,
        )
        return AuditDraftCreationEnvelopeV2(
            engagement=engagement,
            project=project,
            run=run,
            run_created_event=run_event,
            audit=scan,
            contract=contract_record,
            security_context_binding=contract.security_context_binding,
            audit_created_event=audit_event,
            client_request=client_request,
        )


class AuditApplicationService:
    def __init__(
        self,
        *,
        creation_uow: AuditCreationUnitOfWork,
        aggregate_repository: AuditAggregateReadRepository,
        feature_enabled: bool,
        workspace_root: Path,
        legacy_draft_api_enabled: bool = False,
        id_factory: AuditIdFactory = new_id,
        clock: AuditClock = utc_now,
    ) -> None:
        workspace_root = Path(workspace_root)
        if not workspace_root.is_absolute() or ".." in workspace_root.parts:
            raise ValueError("Audit workspace root must be an absolute normalized path")
        self._creation_uow = creation_uow
        self._aggregate_repository = aggregate_repository
        self._feature_enabled = feature_enabled
        self._legacy_draft_api_enabled = bool(legacy_draft_api_enabled)
        self._workspace_root = workspace_root
        self._id_factory = id_factory
        self._clock = clock

    def require_legacy_draft_api_enabled(self) -> None:
        """Keep the AUD-104 synthetic wire available only to explicit tests."""

        self._require_enabled()
        if not self._legacy_draft_api_enabled:
            raise ServiceUnavailableError(
                "audit_legacy_draft_disabled",
                "The synthetic Code Audit v1 draft API is disabled",
            )

    async def create_draft(self, command: CreateAuditDraft) -> AuditDraftResult:
        """Trusted application edge retained for non-HTTP composition and tests."""

        return await self._create_draft(
            command,
            authorized_engagement_scope=AuditEngagementScope.profile_a(),
        )

    async def create_draft_authorized(
        self,
        command: CreateAuditDraft,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditDraftResult:
        """Create through a server-derived authorization domain and scope."""

        self._require_enabled()
        scope = authorizer.authorized_engagement_scope(
            principal,
            capability=OperatorCapability.WRITE,
        )
        expected_reference = authorizer.draft_authorization_reference(
            principal,
            capability=OperatorCapability.WRITE,
        )
        if (
            not isinstance(command, CreateAuditDraft)
            or not isinstance(command.authorization_reference, str)
            or not isinstance(expected_reference, str)
            or not hmac.compare_digest(
                command.authorization_reference,
                expected_reference,
            )
            or (
                command.engagement_id is not None
                and not scope.permits(command.engagement_id)
            )
        ):
            raise ApplicationConflictError(
                "audit_creation_conflict",
                "The Code Audit draft conflicts with an existing authorization domain",
            ) from None
        return await self._create_draft(
            command,
            authorized_engagement_scope=scope,
        )

    async def create_draft_v2_authorized(
        self,
        command: CreateAuditDraftV2,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditDraftResult:
        """Create the only production AUD-201 draft from an authoritative Plan."""

        self._require_enabled()
        scope = authorizer.authorized_engagement_scope(
            principal,
            capability=OperatorCapability.WRITE,
        )
        authorization_scope_digest = authorizer.preflight_authorization_scope_digest(
            principal,
            capability=OperatorCapability.WRITE,
        )
        authorization_reference = authorizer.draft_authorization_reference(
            principal,
            capability=OperatorCapability.WRITE,
        )
        try:
            builder = _AuditDraftV2Builder(
                command,
                operator_principal_id=principal.id,
                authorization_scope_digest=authorization_scope_digest,
                authorization_reference=authorization_reference,
                authorized_engagement_scope=scope,
                workspace_root=self._workspace_root,
                id_factory=self._id_factory,
                clock=self._clock,
            )
        except (AttributeError, TypeError, ValueError):
            raise ApplicationConflictError(
                "audit_preflight_token_invalid",
                "The Code Audit Preflight token or draft request is invalid",
            ) from None
        try:
            aggregate, created = await self._creation_uow.create_draft_v2(builder)
        except AuditIdempotencyConflictError:
            raise ApplicationConflictError(
                "audit_idempotency_conflict",
                "The client request identifier is already bound to different content",
            ) from None
        except EntityNotFoundError:
            raise ApplicationConflictError(
                "audit_creation_conflict",
                "The Code Audit draft conflicts with an existing authorization domain",
            ) from None
        except RepositoryConflictError:
            raise ApplicationConflictError(
                "audit_preflight_plan_unavailable",
                "The Code Audit Preflight Plan cannot be used for this request",
            ) from None
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise ServiceUnavailableError(
                "audit_persistence_unavailable",
                "RiftX Code Audit persistence is temporarily unavailable",
            ) from None
        _validate_run_projection(aggregate)
        return AuditDraftResult(aggregate=aggregate, created=created)

    async def _create_draft(
        self,
        command: CreateAuditDraft,
        *,
        authorized_engagement_scope: AuditEngagementScope,
    ) -> AuditDraftResult:
        self._require_enabled()
        try:
            builder = _AuditDraftBuilder(
                command,
                authorized_engagement_scope=authorized_engagement_scope,
                workspace_root=self._workspace_root,
                id_factory=self._id_factory,
                clock=self._clock,
            )
        except (AttributeError, TypeError, ValueError):
            raise ApplicationConflictError(
                "audit_contract_invalid",
                "The Code Audit draft request or frozen contract is invalid",
            ) from None
        try:
            aggregate, created = await self._creation_uow.create_draft(builder)
        except AuditIdempotencyConflictError:
            raise ApplicationConflictError(
                "audit_idempotency_conflict",
                "The client request identifier is already bound to different content",
            ) from None
        except (EntityNotFoundError, RepositoryConflictError):
            raise ApplicationConflictError(
                "audit_creation_conflict",
                "The Code Audit draft conflicts with an existing authorization domain",
            ) from None
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise ServiceUnavailableError(
                "audit_persistence_unavailable",
                "RiftX Code Audit persistence is temporarily unavailable",
            ) from None
        _validate_run_projection(aggregate)
        return AuditDraftResult(aggregate=aggregate, created=created)

    async def get_authorized(
        self,
        audit_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
        capability: OperatorCapability = OperatorCapability.READ,
    ) -> AuditAggregate:
        """Authorize a contract-free owner binding before loading the aggregate."""

        try:
            aggregate = await self._aggregate_repository.get_authorized(
                audit_id,
                authorize=lambda binding: authorizer.require_audit_binding(
                    principal,
                    binding,
                    capability=capability,
                ),
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _audit_persistence_unavailable() from None
        if aggregate is None:
            raise _audit_not_accessible() from None
        _validate_authorized_projection(aggregate)
        return aggregate

    async def list_authorized(
        self,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
        run_id: str | None = None,
        project_id: str | None = None,
        engagement_id: str | None = None,
        lifecycle_status: AuditLifecycleStatus | None = None,
        mode: AuditMode | None = None,
        run_status: RunStatus | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditAggregate]:
        """Apply the server scope in SQL before ordering and pagination."""

        _validate_page(limit=limit, offset=offset)
        _validate_created_range(created_from, created_to)
        scope = authorizer.authorized_engagement_scope(
            principal,
            capability=OperatorCapability.READ,
        )
        try:
            aggregates = await self._aggregate_repository.list_authorized(
                authorized_scope=scope,
                run_id=run_id,
                project_id=project_id,
                engagement_id=engagement_id,
                lifecycle_status=lifecycle_status,
                mode=mode,
                run_status=run_status,
                created_from=created_from,
                created_to=created_to,
                limit=limit,
                offset=offset,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _audit_persistence_unavailable() from None
        for aggregate in aggregates:
            _validate_authorized_projection(aggregate)
        return aggregates

    async def get_by_run_authorized(
        self,
        run_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditAggregate:
        """Resolve an Audit-owned generic Run through the Audit ACL root."""

        try:
            aggregate = await self._aggregate_repository.get_by_run_authorized(
                run_id,
                authorize=lambda binding: authorizer.require_audit_binding(
                    principal,
                    binding,
                    capability=OperatorCapability.READ,
                ),
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _audit_persistence_unavailable() from None
        if aggregate is None:
            raise _audit_not_accessible() from None
        _validate_authorized_projection(aggregate)
        return aggregate

    async def get(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
        engagement_id: str | None = None,
    ) -> AuditAggregate:
        aggregate = await self._aggregate_repository.get(
            audit_id,
            project_id=project_id,
            engagement_id=engagement_id,
        )
        if aggregate is None:
            raise EntityNotFoundError("Audit", audit_id)
        _validate_run_projection(aggregate)
        return aggregate

    async def list(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        engagement_id: str | None = None,
        lifecycle_status: AuditLifecycleStatus | None = None,
        mode: AuditMode | None = None,
        run_status: RunStatus | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditAggregate]:
        _validate_page(limit=limit, offset=offset)
        _validate_created_range(created_from, created_to)
        aggregates = await self._aggregate_repository.list(
            run_id=run_id,
            project_id=project_id,
            engagement_id=engagement_id,
            lifecycle_status=lifecycle_status,
            mode=mode,
            run_status=run_status,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )
        for aggregate in aggregates:
            _validate_run_projection(aggregate)
        return aggregates

    async def pause(self, audit_id: str) -> AuditControlPlan:
        return self.plan_pause(await self.get(audit_id))

    def plan_pause(self, aggregate: AuditAggregate) -> AuditControlPlan:
        """Derive pause admission from one already-authorized aggregate read."""

        _validate_run_projection(aggregate)
        status = aggregate.audit.value.lifecycle_status
        if status in {
            AuditLifecycleStatus.RUNNING,
            AuditLifecycleStatus.WAITING_APPROVAL,
        }:
            disposition = AuditControlDisposition.TRANSITION
            target_audit = AuditLifecycleStatus.PAUSING
            target_run = RunStatus.PAUSING
            effect = AuditControlEffect.PAUSE_WORKFLOW_THEN_PROJECT
            reason_code = "audit_pause_requested"
        elif status is AuditLifecycleStatus.PAUSING:
            disposition = AuditControlDisposition.RECONCILE
            target_audit = AuditLifecycleStatus.PAUSED
            target_run = RunStatus.PAUSED
            effect = AuditControlEffect.RECONCILE_PAUSE
            reason_code = "audit_pause_reconciliation_required"
        elif status is AuditLifecycleStatus.PAUSED:
            disposition = AuditControlDisposition.ALREADY_SATISFIED
            target_audit = AuditLifecycleStatus.PAUSED
            target_run = RunStatus.PAUSED
            effect = AuditControlEffect.NONE
            reason_code = "audit_already_paused"
        else:
            self._not_controllable(aggregate, AuditControlAction.PAUSE)
        return _control_plan(
            aggregate,
            operation=AuditControlAction.PAUSE,
            disposition=disposition,
            effect=effect,
            reason_code=reason_code,
            target_audit=target_audit,
            target_run=target_run,
        )

    async def resume(self, audit_id: str) -> AuditControlPlan:
        self._require_enabled()
        return self.plan_resume(await self.get(audit_id))

    def plan_resume(self, aggregate: AuditAggregate) -> AuditControlPlan:
        """Derive resume admission from one already-authorized aggregate read."""

        self._require_enabled()
        _validate_run_projection(aggregate)
        if aggregate.audit.value.lifecycle_status is not AuditLifecycleStatus.PAUSED:
            self._not_controllable(aggregate, AuditControlAction.RESUME)
        return _control_plan(
            aggregate,
            operation=AuditControlAction.RESUME,
            disposition=AuditControlDisposition.TRANSITION,
            effect=AuditControlEffect.RESUME_WORKFLOW_THEN_PROJECT,
            reason_code="audit_resume_requested",
            target_audit=AuditLifecycleStatus.RUNNING,
            target_run=RunStatus.RUNNING,
        )

    async def cancel(self, audit_id: str) -> AuditControlPlan:
        return self.plan_cancel(await self.get(audit_id))

    def plan_cancel(self, aggregate: AuditAggregate) -> AuditControlPlan:
        """Derive cancel admission from one already-authorized aggregate read."""

        _validate_run_projection(aggregate)
        scan = aggregate.audit.value
        cleanup_converged = (
            scan.cleanup_proof_digest is not None and scan.run_terminal_status is not None
        )
        publication_or_terminal = scan.lifecycle_status in {
            AuditLifecycleStatus.SEALING_CORE,
            AuditLifecycleStatus.REPORTING,
            AuditLifecycleStatus.PACKAGING,
            AuditLifecycleStatus.COMPLETED,
            AuditLifecycleStatus.COMPLETED_PARTIAL,
            AuditLifecycleStatus.FAILED,
            AuditLifecycleStatus.CANCELLED,
        }
        if cleanup_converged or publication_or_terminal:
            disposition = AuditControlDisposition.SAFETY_ONLY
            target_audit = scan.lifecycle_status
            target_run = aggregate.run.status
            effect = AuditControlEffect.SAFETY_STOP_SWEEP_ONLY
            reason_code = "audit_cancel_safety_sweep"
        elif scan.lifecycle_status is AuditLifecycleStatus.CANCELLING:
            disposition = AuditControlDisposition.RECONCILE
            target_audit = AuditLifecycleStatus.CLEANING
            target_run = RunStatus.CANCELLING
            effect = AuditControlEffect.RECONCILE_CANCEL_STOP
            reason_code = "audit_cancel_reconciliation_required"
        elif scan.lifecycle_status is AuditLifecycleStatus.CLEANING:
            disposition = AuditControlDisposition.RECONCILE
            target_audit = (
                AuditLifecycleStatus.CLEANING
                if scan.terminal_outcome is AuditTerminalOutcome.CANCELLED
                else AuditLifecycleStatus.CANCELLING
            )
            target_run = RunStatus.CANCELLING
            effect = AuditControlEffect.RECONCILE_CANCEL_STOP
            reason_code = "audit_cancel_reconciliation_required"
        elif (
            scan.can_transition_to(AuditLifecycleStatus.CANCELLING)
            and scan.cleanup_proof_digest is None
            and scan.closure_status is None
        ):
            disposition = AuditControlDisposition.TRANSITION
            target_audit = AuditLifecycleStatus.CANCELLING
            target_run = RunStatus.CANCELLING
            effect = AuditControlEffect.FENCE_NEW_EFFECTS_AND_STOP
            reason_code = "audit_cancel_requested"
        else:
            disposition = AuditControlDisposition.SAFETY_ONLY
            target_audit = scan.lifecycle_status
            target_run = aggregate.run.status
            effect = AuditControlEffect.SAFETY_STOP_SWEEP_ONLY
            reason_code = "audit_cancel_safety_sweep"
        return _control_plan(
            aggregate,
            operation=AuditControlAction.CANCEL,
            disposition=disposition,
            effect=effect,
            reason_code=reason_code,
            target_audit=target_audit,
            target_run=target_run,
        )

    def _require_enabled(self) -> None:
        if not self._feature_enabled:
            raise ServiceUnavailableError(
                "feature_disabled",
                "RiftX Code Audit is disabled",
            )

    @staticmethod
    def _not_controllable(
        aggregate: AuditAggregate,
        action: AuditControlAction,
    ) -> NoReturn:
        scan = aggregate.audit.value
        code = {
            AuditControlAction.PAUSE: "audit_not_pauseable",
            AuditControlAction.RESUME: "audit_not_resumable",
            AuditControlAction.CANCEL: "audit_not_cancellable",
        }[action]
        raise ApplicationConflictError(
            code,
            f"Cannot {action.value} Audit {scan.id!r} while it is {scan.lifecycle_status.value}",
            details={
                "audit_id": scan.id,
                "lifecycle_status": scan.lifecycle_status.value,
                "run_status": aggregate.run.status.value,
            },
        )


def _validate_create_command(command: CreateAuditDraft) -> CreateAuditDraft:
    if not isinstance(command, CreateAuditDraft):
        raise TypeError("command must be CreateAuditDraft")
    try:
        request_uuid = UUID(command.client_request_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("client_request_id must be a canonical UUID") from exc
    if request_uuid.int == 0 or str(request_uuid) != command.client_request_id:
        raise ValueError("client_request_id must be a non-zero canonical UUID")
    project_name = command.project_name.strip()
    if (
        project_name != command.project_name
        or not project_name
        or len(project_name) > 255
        or len(project_name.encode("utf-8")) > 1024
        or _contains_control_character(project_name)
    ):
        raise ValueError("project_name is invalid")
    _require_lower_digest(command.repository_identity_digest, "repository identity")
    _require_lower_digest(command.authorization_reference, "authorization reference")
    if command.engagement_id is not None and (
        command.engagement_id != command.engagement_id.strip()
        or not command.engagement_id
        or len(command.engagement_id) > 64
        or _contains_control_character(command.engagement_id)
    ):
        raise ValueError("engagement_id is invalid")
    if command.default_branch is not None and (
        command.default_branch != command.default_branch.strip()
        or not command.default_branch
        or len(command.default_branch) > 1024
        or len(command.default_branch.encode("utf-8")) > 4096
        or _contains_control_character(command.default_branch)
    ):
        raise ValueError("default_branch is invalid")
    AuditContractBlueprint.from_contract(command.contract.template)
    return command


def _validate_create_v2_command(command: CreateAuditDraftV2) -> CreateAuditDraftV2:
    if not isinstance(command, CreateAuditDraftV2):
        raise TypeError("command must be CreateAuditDraftV2")
    try:
        request_uuid = UUID(command.client_request_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("client_request_id must be a canonical UUID") from exc
    if request_uuid.int == 0 or str(request_uuid) != command.client_request_id:
        raise ValueError("client_request_id must be a non-zero canonical UUID")
    if (
        command.project_name != command.project_name.strip()
        or not command.project_name
        or len(command.project_name) > 255
        or len(command.project_name.encode("utf-8")) > 1_024
        or _contains_control_character(command.project_name)
    ):
        raise ValueError("project_name is invalid")
    if command.engagement_id is not None and (
        command.engagement_id != command.engagement_id.strip()
        or not command.engagement_id
        or len(command.engagement_id) > 64
        or _contains_control_character(command.engagement_id)
    ):
        raise ValueError("engagement_id is invalid")
    if (
        not isinstance(command.required_sandbox_backend, str)
        or command.required_sandbox_backend
        != command.required_sandbox_backend.strip()
        or not command.required_sandbox_backend
        or len(command.required_sandbox_backend) > 128
        or _contains_control_character(command.required_sandbox_backend)
    ):
        raise ValueError("required_sandbox_backend is invalid")
    AuditDraftBudgetV2.model_validate(command.budget)
    audit_preflight_token_hash(command.preflight_token)
    return command


def _create_v2_caller_preferences(command: CreateAuditDraftV2) -> dict[str, object]:
    return {
        "project_name": command.project_name,
        "engagement_id": command.engagement_id,
        "mode": command.mode.value,
        "analysis_profile": command.analysis_profile.value,
        "model_profile": command.model_profile,
        "model_data_egress": {"mode": command.model_data_egress_mode},
        "validation_policy": command.validation_policy.value,
        "baseline_audit_id": command.baseline_audit_id,
        "execution_target": {
            "node_id": command.execution_node_id,
            "required_sandbox_backend": command.required_sandbox_backend,
        },
        "budget": command.budget.model_dump(mode="json"),
    }


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _require_lower_digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _request_digest(command: CreateAuditDraft) -> str:
    payload = {
        "request_schema_version": AUDIT_CLIENT_REQUEST_SCHEMA_VERSION,
        "project_name": command.project_name,
        "repository_identity_digest": command.repository_identity_digest,
        "authorization_reference": command.authorization_reference,
        "engagement_id": command.engagement_id,
        "default_branch": command.default_branch,
        "contract": command.contract.request_payload(),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(_REQUEST_DIGEST_DOMAIN.encode("ascii") + b"\0" + canonical).hexdigest()


def _validate_workspace_separation(workspace_root: Path, source_path: str) -> None:
    if not source_path.startswith("/"):
        return
    workspace = PurePosixPath(str(workspace_root))
    source = PurePosixPath(source_path)
    if (
        ".." in source.parts
        or str(source) != source_path
        or workspace == source
        or workspace.is_relative_to(source)
        or source.is_relative_to(workspace)
    ):
        raise ValueError("Audit workspace and source repository must not overlap")


def _validate_page(*, limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {_MAX_PAGE_SIZE}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must not be negative")


def _validate_created_range(
    created_from: datetime | None,
    created_to: datetime | None,
) -> None:
    for value in (created_from, created_to):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Audit created-time filters must be timezone-aware")
    if created_from is not None and created_to is not None and created_from > created_to:
        raise ValueError("created_from must not be later than created_to")


def _validate_run_projection(aggregate: AuditAggregate) -> None:
    scan = aggregate.audit.value
    try:
        expected = AuditRunStateMappingPolicy.expected_run_status(scan)
    except ValueError:
        expected = None
    if (
        aggregate.run.kind is not RunKind.CODE_AUDIT
        or expected is None
        or aggregate.run.status is not expected
        or aggregate.run.id != scan.run_id
        or aggregate.run.temporal_workflow_id != scan.temporal_workflow_id
    ):
        raise ApplicationConflictError(
            "audit_run_state_conflict",
            "The Code Audit and its Run have inconsistent durable state",
            details={
                "audit_id": scan.id,
                "lifecycle_status": scan.lifecycle_status.value,
                "run_id": aggregate.run.id,
                "run_status": aggregate.run.status.value,
            },
        )


def _validate_authorized_projection(aggregate: AuditAggregate) -> None:
    try:
        _validate_run_projection(aggregate)
    except ApplicationConflictError:
        raise _audit_persistence_unavailable() from None


def _audit_not_accessible() -> ResourceNotAccessibleError:
    return ResourceNotAccessibleError(
        "resource_not_accessible",
        "The requested resource was not found",
        details={"messages": {"zh-CN": "未找到请求的资源"}},
    )


def _audit_persistence_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_persistence_unavailable",
        "RiftX Code Audit persistence is temporarily unavailable",
    )


def _control_plan(
    aggregate: AuditAggregate,
    *,
    operation: AuditControlAction,
    disposition: AuditControlDisposition,
    effect: AuditControlEffect,
    reason_code: str,
    target_audit: AuditLifecycleStatus | None,
    target_run: RunStatus | None,
) -> AuditControlPlan:
    scan = aggregate.audit.value
    return AuditControlPlan(
        operation=operation,
        disposition=disposition,
        required_effect=effect,
        audit_id=scan.id,
        run_id=aggregate.run.id,
        expected_audit_state_version=aggregate.audit.state_version,
        current_audit_lifecycle=scan.lifecycle_status,
        current_run_status=aggregate.run.status,
        reason_code=reason_code,
        target_audit_lifecycle=target_audit,
        target_run_status=target_run,
    )


__all__ = [
    "AuditApplicationService",
    "AuditContractBlueprint",
    "AuditControlAction",
    "AuditControlDisposition",
    "AuditControlEffect",
    "AuditControlPlan",
    "AuditDraftResult",
    "AuditRunStateMappingPolicy",
    "CreateAuditDraft",
]
