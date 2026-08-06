"""Production dependency assembly for the RiftX control plane."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import platform
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from pydantic import SecretStr
from temporalio.client import Client

from riftx import __version__
from riftx.application.services import (
    ActionApplicationService,
    ApprovalApplicationService,
    ArtifactApplicationService,
    AuditApplicationService,
    AuditControlApplicationService,
    AuditPreflightApplicationService,
    AuditPreflightPlanApplicationService,
    AuditPreflightRunnerService,
    AuditRunStateProjector,
    EventApplicationService,
    ExecutionApplicationService,
    FindingApplicationService,
    ModelProfileApplicationService,
    NodeApplicationService,
    NodeRegistration,
    PentestApplicationService,
    PentestCapabilityResolver,
    ReportApplicationService,
    RunApplicationService,
    RunnerControlService,
    RunSafetyStopService,
    TerminalApplicationService,
    ToolApplicationService,
)
from riftx.application.services.workflow_signals import (
    WorkflowSignalDispatcher,
    WorkflowSignalReconciler,
)
from riftx.application.workflow_router import RunWorkflowControlRouter
from riftx.audit import (
    LocalAuditJobService,
    LocalAuditWorker,
    LocalAuditWorkerConfig,
)
from riftx.browser.service import BrowserApplicationService
from riftx.config import (
    AuditConfig,
    RiftXConfig,
    RiftXConfigError,
    audit_source_ingest_policy_digest,
    load_riftx_config,
    validate_audit_storage_isolation,
)
from riftx.connectors.service import ConnectorApplicationService
from riftx.context import ContextApplicationService
from riftx.domain import (
    AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    NodeStatus,
    OperatorCapability,
    RunKind,
    RunStatus,
    TrustProfile,
)
from riftx.domain.audit_preflight_plan import AuditPreflightTokenCodec
from riftx.domain.base import utc_now
from riftx.executors import DirectProcessExecutor, LinuxCgroupV2Manager
from riftx.hooks import HookBus, RunEventHookAuditSink
from riftx.memory import MemoryService, MemoryWriter
from riftx.models import ModelProfileRegistry
from riftx.observability import RuntimeObservabilityService
from riftx.packs import OfficialPackCatalog, bootstrap_official_packs
from riftx.persistence import (
    Database,
    SQLAlchemyActionReadRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyAuditControlUnitOfWork,
    SQLAlchemyAuditCreationUnitOfWork,
    SQLAlchemyAuditPreflightPlanRepository,
    SQLAlchemyAuditPreflightRepository,
    SQLAlchemyCapabilityRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyGraphReadRepository,
    SQLAlchemyLocalAuditJobRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyPentestCreationUnitOfWork,
    SQLAlchemyReportRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunnerCommandRepository,
    SQLAlchemyRunnerCredentialRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyRuntimeApprovalRepository,
    SQLAlchemyTerminalRepository,
    SQLAlchemyToolCallIntentRepository,
    SQLAlchemyTrafficMetadataReadRepository,
)
from riftx.persistence.browser_repositories import SQLAlchemyBrowserRepository
from riftx.persistence.connector_repositories import (
    SQLAlchemyConnectorSubmissionRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.memory_repositories import SQLAlchemyMemoryRepository
from riftx.persistence.observability_repository import (
    SQLAlchemyRuntimeObservabilityRepository,
)
from riftx.persistence.target_http_repositories import (
    SQLAlchemyTargetHttpRequestRepository,
)
from riftx.persistence.workflow_signals import (
    SQLAlchemyWorkflowSignalIntentRepository,
)
from riftx.runner import (
    ExecutionRunner,
    NodeBrowserRouter,
    NodeTargetHttpRouter,
    ProcessSupervisor,
    RemoteBrowserClient,
    RemoteTargetHttpClient,
    RunnerBrowserManager,
    RunnerPaths,
    RunnerTargetHttpClient,
    TerminalSupervisor,
)
from riftx.runner.remote import NodeExecutionRouter, RemoteExecutionSupervisor
from riftx.runner.remote_terminal import NodeTerminalRouter, RemoteTerminalSupervisor
from riftx.security import (
    LocalObjectAuthorizer,
    LocalOperatorSecurity,
    validate_deployment_profile,
    validate_operator_runner_credential_separation,
)
from riftx.skills import create_default_skill_registry
from riftx.target_http.service import TargetHttpApplicationService
from riftx.temporal.connection import TemporalConnectionSettings, connect_temporal
from riftx.temporal.runtime import LazyTemporalRunClient, TemporalRuntimeConfig
from riftx.temporal.workflow_signal_transport import (
    RoutedWorkflowSignalTransport,
    TemporalWorkflowSignalOutcomeProbe,
)
from riftx.tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class APISettings:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8787
    trust_profile: TrustProfile | None = None
    trust_proxy_auth: bool = False
    local_principal_path: Path = Path(".riftx/secrets/local-principal.json")
    local_operator_capabilities: frozenset[OperatorCapability] = field(
        default_factory=lambda: frozenset(OperatorCapability)
    )
    database_url: str = "sqlite+aiosqlite:///./.riftx/riftx.db"
    tools_config_path: Path = Path("configs/tools.yaml")
    skills_config_path: Path = Path(".riftx/skills")
    models_config_path: Path = Path("configs/models.yaml")
    model_secrets_path: Path = Path(".riftx/secrets/models.json")
    model_profile_override: str | None = None
    node_id: str = "local"
    workspace_root: Path = Path(".riftx/workspaces")
    runner_state_path: Path = Path(".riftx/runner")
    runner_credential_path: Path = field(
        default=Path(".riftx/secrets/runner-credentials.json"),
        repr=False,
    )
    web_dist_path: Path = Path("apps/web/dist")
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "riftx-v2"
    temporal_workflow_id_prefix: str = "riftx-run"
    temporal_tls_enabled: bool = False
    temporal_tls_server_root_ca_path: Path | None = field(default=None, repr=False)
    temporal_tls_server_name: str | None = field(default=None, repr=False)
    temporal_tls_client_cert_path: Path | None = field(default=None, repr=False)
    temporal_tls_client_private_key_path: Path | None = field(default=None, repr=False)
    temporal_api_key: SecretStr | None = field(default=None, repr=False)
    sse_poll_interval_seconds: float = 0.5
    sse_heartbeat_seconds: float = 15.0
    node_offline_after_seconds: float = 30.0
    node_lost_after_seconds: float = 300.0
    runner_registration_token: str | None = field(default=None, repr=False)
    runner_command_lease_seconds: float = 30.0
    require_containment: bool = True
    payload_uid: int | None = None
    payload_gid: int | None = None
    admin_token: str | None = field(default=None, repr=False)
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )
    audit: AuditConfig = field(default_factory=AuditConfig, repr=False)

    @classmethod
    def from_config(cls, config: RiftXConfig) -> APISettings:
        settings = cls(
            listen_host=config.server.host,
            listen_port=config.server.port,
            trust_profile=config.security.trust_profile,
            trust_proxy_auth=config.security.trust_proxy_auth,
            local_principal_path=config.security.local_principal_path.expanduser(),
            local_operator_capabilities=config.security.local_operator_capabilities,
            database_url=config.database.url,
            tools_config_path=config.tools.path.expanduser(),
            skills_config_path=config.skills.path.expanduser(),
            models_config_path=config.models.path.expanduser(),
            model_secrets_path=config.models.secrets_path.expanduser(),
            model_profile_override=config.models.profile,
            node_id=config.runner.node_id,
            workspace_root=config.workspace.root.expanduser(),
            runner_state_path=config.runner.state_path.expanduser(),
            runner_credential_path=config.runner.credential_path.expanduser(),
            web_dist_path=config.web.dist_path.expanduser(),
            temporal_address=config.temporal.target,
            temporal_namespace=config.temporal.namespace,
            temporal_task_queue=config.temporal.task_queue,
            temporal_workflow_id_prefix=config.temporal.workflow_id_prefix,
            temporal_tls_enabled=config.temporal.tls_enabled,
            temporal_tls_server_root_ca_path=config.temporal.tls_server_root_ca_path,
            temporal_tls_server_name=config.temporal.tls_server_name,
            temporal_tls_client_cert_path=config.temporal.tls_client_cert_path,
            temporal_tls_client_private_key_path=config.temporal.tls_client_private_key_path,
            temporal_api_key=config.temporal.api_key,
            sse_poll_interval_seconds=config.server.sse_poll_interval_seconds,
            sse_heartbeat_seconds=config.server.sse_heartbeat_seconds,
            node_offline_after_seconds=config.runner.node_offline_after_seconds,
            node_lost_after_seconds=config.runner.node_lost_after_seconds,
            runner_registration_token=config.runner.registration_token,
            runner_command_lease_seconds=config.runner.command_lease_seconds,
            require_containment=config.execution.require_containment,
            payload_uid=config.execution.payload_uid,
            payload_gid=config.execution.payload_gid,
            admin_token=config.security.admin_token,
            cors_origins=tuple(config.server.cors_origins),
            audit=config.audit.model_copy(deep=True),
        )
        settings.validate_api_security_boundary()
        return settings

    @classmethod
    def from_environment(cls) -> APISettings:
        return cls.from_config(load_riftx_config())

    def temporal_connection_settings(self) -> TemporalConnectionSettings:
        return TemporalConnectionSettings(
            target=self.temporal_address,
            namespace=self.temporal_namespace,
            tls_enabled=self.temporal_tls_enabled,
            tls_server_root_ca_path=self.temporal_tls_server_root_ca_path,
            tls_server_name=self.temporal_tls_server_name,
            tls_client_cert_path=self.temporal_tls_client_cert_path,
            tls_client_private_key_path=self.temporal_tls_client_private_key_path,
            api_key=self.temporal_api_key,
        )

    def validate_deployment_profile(self) -> TrustProfile:
        return validate_deployment_profile(
            trust_profile=self.trust_profile,
            listen_host=self.listen_host,
            trust_proxy_auth=self.trust_proxy_auth,
            cors_origins=self.cors_origins,
        )

    def create_local_operator_security(self) -> LocalOperatorSecurity:
        self.validate_api_security_boundary()
        return LocalOperatorSecurity.create(
            principal_path=self.local_principal_path,
            configured_token=self.admin_token,
            capabilities=self.local_operator_capabilities,
            allowed_origins=self.allowed_browser_origins(),
        )

    def validate_api_security_boundary(self) -> TrustProfile:
        profile = self.validate_deployment_profile()
        validate_operator_runner_credential_separation(
            self.admin_token,
            self.runner_registration_token,
        )
        return profile

    def allowed_browser_origins(self) -> tuple[str, ...]:
        host = self.listen_host.strip().lower().rstrip(".")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        authority_host = f"[{host}]" if ":" in host else host
        control_plane_origin = f"http://{authority_host}:{self.listen_port}"
        return tuple(dict.fromkeys((*self.cors_origins, control_plane_origin)))


def _create_temporal_connector(settings: APISettings) -> Callable[[], Awaitable[Client]]:
    connection_settings = settings.temporal_connection_settings()

    async def connector() -> Client:
        return await connect_temporal(connection_settings)

    return connector


def _create_audit_service(
    settings: APISettings,
    database: Database,
    *,
    aggregate_repository: SQLAlchemyAuditAggregateReadRepository | None = None,
) -> AuditApplicationService:
    """Assemble the always-present, database-only Code Audit application edge."""

    return AuditApplicationService(
        creation_uow=SQLAlchemyAuditCreationUnitOfWork(database.session_factory),
        aggregate_repository=(
            aggregate_repository
            or SQLAlchemyAuditAggregateReadRepository(database.session_factory)
        ),
        feature_enabled=settings.audit.enabled,
        workspace_root=settings.audit.temp_root,
    )


async def _create_local_audit_job_service(
    settings: APISettings,
    database: Database,
) -> LocalAuditJobService:
    repository = SQLAlchemyLocalAuditJobRepository(database.session_factory)
    worker = None
    if settings.audit.enabled and settings.audit.source_roots:
        worker = LocalAuditWorker(
            repository,
            LocalAuditWorkerConfig(
                allowed_roots=settings.audit.source_roots,
                protected_paths=(
                    settings.audit.fix_root,
                    settings.workspace_root.expanduser().resolve(strict=False),
                    settings.runner_state_path.expanduser().resolve(strict=False),
                ),
                staging_root=settings.audit.temp_root / "local-jobs",
                snapshot_root=settings.audit.snapshot_root,
                max_file_bytes=settings.audit.max_file_bytes,
                max_repository_bytes=settings.audit.max_repository_bytes,
                max_manifest_entries=settings.audit.max_files,
                max_text_characters=settings.audit.max_file_bytes,
            ),
        )
    service = LocalAuditJobService(
        repository,
        worker,
        auto_dispatch=True,
    )
    await service.recover()
    return service


def _create_audit_preflight_service(
    settings: APISettings,
    database: Database,
    *,
    repository: SQLAlchemyAuditPreflightRepository | None = None,
    source_ingest_available: bool | Callable[[], bool | Awaitable[bool]] = False,
) -> AuditPreflightApplicationService:
    """Assemble the always-present, fail-closed pre-Audit Operator edge.

    SourceIngest capability is not inferred from the Control Plane host.  The
    independent Runner/Capsule protocol must later supply authoritative backend
    availability; until then an enabled create returns ``audit_sandbox_unavailable``.
    Existing jobs remain readable and cancellable through the durable repository.
    """

    audit = settings.audit
    return AuditPreflightApplicationService(
        repository=(
            repository
            if repository is not None
            else SQLAlchemyAuditPreflightRepository(database.session_factory)
        ),
        feature_enabled=audit.enabled,
        source_roots=audit.source_roots,
        backend_id=audit.source_ingest.backend_id,
        image_digest=audit.source_ingest.image_digest,
        policy_digest=audit_source_ingest_policy_digest(audit.source_ingest),
        source_ingest_available=source_ingest_available,
        node_mode=audit.node_mode,
        allowed_node_ids=audit.allowed_node_ids,
        job_ttl_seconds=audit.source_ingest.job_ttl_seconds,
    )


def _create_audit_preflight_plan_service(
    settings: APISettings,
    database: Database,
    *,
    preflight_repository: SQLAlchemyAuditPreflightRepository | None = None,
    plan_repository: SQLAlchemyAuditPreflightPlanRepository | None = None,
) -> AuditPreflightPlanApplicationService:
    """Assemble Plan issuance without inventing a fallback signing key."""

    audit = settings.audit
    token_codec: AuditPreflightTokenCodec | None = None
    if audit.preflight_token_key is not None:
        encoded = audit.preflight_token_key.get_secret_value()
        token_codec = AuditPreflightTokenCodec(
            key_id=audit.preflight_token_key_id,
            key=base64.urlsafe_b64decode(encoded + "="),
        )
    return AuditPreflightPlanApplicationService(
        preflight_repository=(
            preflight_repository
            if preflight_repository is not None
            else SQLAlchemyAuditPreflightRepository(database.session_factory)
        ),
        plan_repository=(
            plan_repository
            if plan_repository is not None
            else SQLAlchemyAuditPreflightPlanRepository(database.session_factory)
        ),
        feature_enabled=audit.enabled,
        token_codec=token_codec,
    )


def _create_audit_preflight_availability_check(
    settings: APISettings,
    *,
    node_service: NodeApplicationService,
    credentials: SQLAlchemyRunnerCredentialRepository,
) -> Callable[[], Awaitable[bool]]:
    """Require one live, probe-backed local Runner identity for SourceIngest.

    The immutable credential capability is the protocol authorization gate. The
    Node capability and exact readiness labels are a separate, revocable
    availability signal emitted only after the Runner's production backend probe
    and recovery-journal validation succeed.
    """

    audit = settings.audit
    backend_id = audit.source_ingest.backend_id
    image_digest = audit.source_ingest.image_digest
    policy_digest = audit_source_ingest_policy_digest(audit.source_ingest)

    async def available() -> bool:
        if image_digest is None:
            return False
        try:
            node = await node_service.get("local")
            credential = await credentials.get_current("local")
        except Exception:
            return False
        if (
            credential is None
            or credential.revoked_at is not None
            or node.current_owner != credential.principal
            or node.status not in {NodeStatus.ONLINE, NodeStatus.DEGRADED}
            or node.platform.strip().lower() != "linux"
            or AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY
            not in credential.protocol_capabilities
            or AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY not in node.capabilities
        ):
            return False
        expected_labels = {
            "audit_source_ingest_available": "true",
            "audit_source_ingest_backend_id": backend_id,
            "audit_source_ingest_image_digest": image_digest,
            "audit_source_ingest_policy_digest": policy_digest,
        }
        return all(
            isinstance(node.labels.get(name), str)
            and secrets.compare_digest(node.labels[name], expected)
            for name, expected in expected_labels.items()
        )

    return available


@dataclass(slots=True)
class ControlPlane:
    settings: APISettings
    database: Database
    run_service: RunApplicationService
    audit_service: AuditApplicationService
    action_service: ActionApplicationService
    event_service: EventApplicationService
    execution_service: ExecutionApplicationService
    finding_service: FindingApplicationService
    node_service: NodeApplicationService
    runner_control_service: RunnerControlService
    report_service: ReportApplicationService
    tool_service: ToolApplicationService
    model_profile_service: ModelProfileApplicationService
    approval_service: ApprovalApplicationService
    artifact_service: ArtifactApplicationService
    context_service: ContextApplicationService
    memory_service: MemoryService
    runtime_observability_service: RuntimeObservabilityService
    terminal_service: TerminalApplicationService
    terminal_supervisor: TerminalSupervisor
    graph_repository: SQLAlchemyGraphReadRepository
    traffic_repository: SQLAlchemyTrafficMetadataReadRepository
    pentest_service: PentestApplicationService | None = None
    local_audit_job_service: LocalAuditJobService | None = None
    audit_control_service: AuditControlApplicationService | None = None
    audit_preflight_service: AuditPreflightApplicationService | None = None
    audit_preflight_plan_service: AuditPreflightPlanApplicationService | None = None
    audit_preflight_runner_service: AuditPreflightRunnerService | None = None
    workflow_signal_dispatcher: WorkflowSignalDispatcher | None = None
    workflow_signal_reconciler: WorkflowSignalReconciler | None = None
    browser_service: BrowserApplicationService | None = None
    connector_service: ConnectorApplicationService | None = None
    browser_manager: RunnerBrowserManager | None = None
    process_supervisor: ProcessSupervisor | None = None
    execution_runner: ExecutionRunner | None = None
    target_http_service: TargetHttpApplicationService | None = None
    _cleanup_reconciler_task: asyncio.Task[None] | None = None
    _workflow_signal_task: asyncio.Task[None] | None = None
    _runner_reconciliation_task: asyncio.Task[None] | None = None
    _audit_preflight_reconciliation_task: asyncio.Task[None] | None = None
    _cleanup_failures: set[str] = field(default_factory=set)

    def start_cleanup_reconciler(self) -> None:
        """Start owner-process cleanup for in-memory local effect handles."""

        if self._cleanup_reconciler_task is not None:
            return
        self._cleanup_reconciler_task = asyncio.create_task(
            self._reconcile_completing_runs(),
            name="riftx-control-plane-cleanup-reconciler",
        )
        if (
            self._workflow_signal_task is None
            and self.workflow_signal_dispatcher is not None
            and self.workflow_signal_reconciler is not None
        ):
            self._workflow_signal_task = asyncio.create_task(
                self._reconcile_workflow_signals(),
                name="riftx-control-plane-workflow-signal-reconciler",
            )
        if self._runner_reconciliation_task is None:
            self._runner_reconciliation_task = asyncio.create_task(
                self._reconcile_runner_state(),
                name="riftx-control-plane-runner-reconciler",
            )
        if (
            self._audit_preflight_reconciliation_task is None
            and self.audit_preflight_runner_service is not None
        ):
            self._audit_preflight_reconciliation_task = asyncio.create_task(
                self._reconcile_audit_preflight_jobs(),
                name="riftx-control-plane-audit-preflight-reconciler",
            )

    async def _reconcile_audit_preflight_jobs(self) -> None:
        """Converge expired Preflight jobs without redispatching unknown work."""

        assert self.audit_preflight_runner_service is not None
        unavailable = False
        while True:
            try:
                await self.audit_preflight_runner_service.reconcile_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not unavailable:
                    logger.exception(
                        "Control Plane Audit Preflight reconciliation failed; retrying"
                    )
                unavailable = True
            else:
                if unavailable:
                    logger.info("Control Plane Audit Preflight reconciliation recovered")
                unavailable = False
            await asyncio.sleep(0.1)

    async def _reconcile_runner_state(self) -> None:
        unavailable = False
        while True:
            try:
                await self.runner_control_service.reconcile_stop_receipts()
                await self.runner_control_service.reconcile_quarantined_commands()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not unavailable:
                    logger.exception("Control Plane Runner reconciliation failed; retrying")
                unavailable = True
            else:
                if unavailable:
                    logger.info("Control Plane Runner reconciliation recovered")
                unavailable = False
            await asyncio.sleep(0.1)

    async def _reconcile_workflow_signals(self) -> None:
        assert self.workflow_signal_dispatcher is not None
        assert self.workflow_signal_reconciler is not None
        unavailable = False
        while True:
            try:
                await self.workflow_signal_dispatcher.dispatch_batch()
                await self.workflow_signal_reconciler.reconcile_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not unavailable:
                    logger.exception(
                        "Control Plane Workflow signal reconciliation failed; retrying"
                    )
                unavailable = True
            else:
                if unavailable:
                    logger.info("Control Plane Workflow signal reconciliation recovered")
                unavailable = False
            await asyncio.sleep(0.1)

    async def _reconcile_completing_runs(self) -> None:
        scan_unavailable = False
        while True:
            try:
                for status in (
                    RunStatus.PAUSING,
                    RunStatus.CANCELLING,
                    RunStatus.COMPLETING,
                ):
                    created_through = utc_now()
                    after_created_at = None
                    after_id = None
                    while True:
                        runs = await self.run_service.list_runs_for_reconciliation(
                            status=status,
                            created_through=created_through,
                            after_created_at=after_created_at,
                            after_id=after_id,
                            limit=100,
                        )
                        for run in runs:
                            try:
                                if run.kind in {RunKind.GENERAL, RunKind.PENTEST}:
                                    result = await self.run_service.stop_resources_for_cleanup(
                                        run.id
                                    )
                                elif run.kind is RunKind.CODE_AUDIT:
                                    if self.audit_control_service is None:
                                        raise RuntimeError(
                                            "Code Audit cleanup service is not assembled"
                                        )
                                    result = await self.audit_control_service.reconcile_run(run.id)
                                else:
                                    raise RuntimeError(
                                        f"unsupported RunKind for cleanup: {run.kind!r}"
                                    )
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                if run.id not in self._cleanup_failures:
                                    logger.exception(
                                        "Owner-process cleanup reconciliation failed for Run %s",
                                        run.id,
                                    )
                                self._cleanup_failures.add(run.id)
                            else:
                                if result.succeeded:
                                    self._cleanup_failures.discard(run.id)
                                elif run.id not in self._cleanup_failures:
                                    logger.warning(
                                        "Owner-process cleanup remains unconfirmed for Run %s: %s",
                                        run.id,
                                        result.failed_resource_types,
                                    )
                                    self._cleanup_failures.add(run.id)
                        if len(runs) < 100:
                            break
                        after_created_at = runs[-1].created_at
                        after_id = runs[-1].id
            except asyncio.CancelledError:
                raise
            except Exception:
                if not scan_unavailable:
                    logger.exception("Owner-process cleanup reconciliation scan failed; retrying")
                scan_unavailable = True
            else:
                if scan_unavailable:
                    logger.info("Owner-process cleanup reconciliation scan recovered")
                scan_unavailable = False
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        if self.local_audit_job_service is not None:
            await self.local_audit_job_service.close()
        audit_preflight_reconciliation_task = self._audit_preflight_reconciliation_task
        self._audit_preflight_reconciliation_task = None
        if audit_preflight_reconciliation_task is not None:
            audit_preflight_reconciliation_task.cancel()
            try:
                await audit_preflight_reconciliation_task
            except asyncio.CancelledError:
                pass
        runner_reconciliation_task = self._runner_reconciliation_task
        self._runner_reconciliation_task = None
        if runner_reconciliation_task is not None:
            runner_reconciliation_task.cancel()
            try:
                await runner_reconciliation_task
            except asyncio.CancelledError:
                pass
        workflow_signal_task = self._workflow_signal_task
        self._workflow_signal_task = None
        if workflow_signal_task is not None:
            workflow_signal_task.cancel()
            try:
                await workflow_signal_task
            except asyncio.CancelledError:
                pass
        cleanup_task = self._cleanup_reconciler_task
        self._cleanup_reconciler_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        if self.browser_manager is not None:
            await self.browser_manager.close_all()
        await self.terminal_supervisor.close_all()
        if self.process_supervisor is not None:
            await self.process_supervisor.close(cancel_running=True)
        await self.database.dispose()


async def build_control_plane(settings: APISettings) -> ControlPlane:
    settings.validate_api_security_boundary()
    _prepare_local_paths(settings)
    database = Database(settings.database_url)
    await database.create_schema()

    capability_repository = SQLAlchemyCapabilityRepository(database.session_factory)
    official_pack_catalog = OfficialPackCatalog()
    await bootstrap_official_packs(capability_repository, official_pack_catalog)
    registry = ToolRegistry(settings.tools_config_path, node_id=settings.node_id)
    tool_snapshot = await registry.refresh()
    skill_registry = create_default_skill_registry(
        settings.skills_config_path,
        official_skill_roots=official_pack_catalog.skill_roots(),
    )
    skill_registry.load_entry_points()
    model_registry = ModelProfileRegistry(
        settings.models_config_path,
        settings.model_secrets_path,
    )
    await asyncio.to_thread(model_registry.refresh)

    temporal_config = TemporalRuntimeConfig(
        task_queue=settings.temporal_task_queue,
        workflow_id_prefix=settings.temporal_workflow_id_prefix,
    )

    # Do not make read/write Control Plane availability depend on Temporal at
    # startup. Creating a Run only records its conversation context; the first
    # message connects and starts/signals the durable Workflow atomically.
    temporal_connector = _create_temporal_connector(settings)
    workflow_client = LazyTemporalRunClient(
        temporal_connector,
        temporal_config,
    )

    engagement_repository = SQLAlchemyEngagementRepository(database.session_factory)
    run_repository = SQLAlchemyRunRepository(database.session_factory)
    audit_aggregate_repository = SQLAlchemyAuditAggregateReadRepository(
        database.session_factory
    )
    action_read_repository = SQLAlchemyActionReadRepository(database.session_factory)
    graph_repository = SQLAlchemyGraphReadRepository(database.session_factory)
    traffic_repository = SQLAlchemyTrafficMetadataReadRepository(
        database.session_factory,
        digest_key=secrets.token_bytes(32),
        artifact_reference_key=secrets.token_bytes(32),
    )
    event_repository = SQLAlchemyRunEventRepository(database.session_factory)
    finding_repository = SQLAlchemyFindingRepository(database.session_factory)
    node_repository = SQLAlchemyNodeRepository(database.session_factory)
    artifact_repository = SQLAlchemyArtifactRepository(database.session_factory)
    report_repository = SQLAlchemyReportRepository(database.session_factory)
    approval_repository = SQLAlchemyApprovalRepository(database.session_factory)
    runtime_approval_repository = SQLAlchemyRuntimeApprovalRepository(database.session_factory)
    execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
    workflow_execution_repository = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=True,
    )
    tool_call_intent_repository = SQLAlchemyToolCallIntentRepository(database.session_factory)
    terminal_repository = SQLAlchemyTerminalRepository(database.session_factory)
    agent_session_repository = SQLAlchemyAgentSessionRepository(database.session_factory)
    browser_repository = SQLAlchemyBrowserRepository(database.session_factory)
    runner_credential_repository = SQLAlchemyRunnerCredentialRepository(database.session_factory)
    runner_command_repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    audit_preflight_repository = SQLAlchemyAuditPreflightRepository(
        database.session_factory
    )
    local_audit_job_service = await _create_local_audit_job_service(settings, database)
    context_repository = SQLAlchemyContextCompilationRepository(database.session_factory)
    memory_repository = SQLAlchemyMemoryRepository(database.session_factory)
    model_profile_service = ModelProfileApplicationService(
        model_registry,
        run_repository=run_repository,
        session_repository=agent_session_repository,
        profile_override=settings.model_profile_override,
    )
    node_service = NodeApplicationService(
        node_repository,
        offline_after=timedelta(seconds=settings.node_offline_after_seconds),
        lost_after=timedelta(seconds=settings.node_lost_after_seconds),
    )
    await node_service.register(
        NodeRegistration(
            node_id=settings.node_id,
            name=platform.node() or settings.node_id,
            platform=platform.system().lower() or os.name,
            architecture=platform.machine() or "unknown",
            runner_version=__version__,
            capabilities=tuple(
                sorted(
                    {
                        capability
                        for tool_id, definition in tool_snapshot.definitions.items()
                        if tool_snapshot.states[tool_id].availability.value == "available"
                        for capability in definition.capabilities
                    }
                )
            ),
            labels={
                "mode": "local",
                "shell": os.environ.get("SHELL") or os.environ.get("COMSPEC", "unknown"),
                "working_directory": str(Path.cwd()),
                "tool_count": str(len(tool_snapshot.definitions)),
            },
        )
    )
    runner_paths = RunnerPaths(settings.runner_state_path)
    runner_control_service = RunnerControlService(
        credentials=runner_credential_repository,
        commands=runner_command_repository,
        nodes=node_service,
        executions=workflow_execution_repository,
        stop_projection_executions=execution_repository,
        runs=run_repository,
        paths=runner_paths,
        registration_token=settings.runner_registration_token,
        terminals=terminal_repository,
        browser_sessions=browser_repository,
        tool_call_intents=tool_call_intent_repository,
        events=event_repository,
        lease_duration=timedelta(seconds=settings.runner_command_lease_seconds),
    )
    audit_preflight_runner_service = AuditPreflightRunnerService(
        repository=audit_preflight_repository,
        credentials=runner_credential_repository,
        lease_duration=timedelta(seconds=settings.audit.source_ingest.lease_seconds),
    )
    # Process and PTY work share one trusted containment root so durable
    # identities resolve to the same kernel ownership namespace after restart.
    containment_manager = LinuxCgroupV2Manager.autodetect(
        payload_uid=settings.payload_uid,
        payload_gid=settings.payload_gid,
    )
    process_executor = DirectProcessExecutor(
        containment_manager=containment_manager,
        autodetect_containment=False,
        require_containment=settings.require_containment,
        defer_activation=True,
    )
    terminal_supervisor = TerminalSupervisor(
        terminal_repository=terminal_repository,
        execution_repository=workflow_execution_repository,
        event_repository=event_repository,
        paths=runner_paths,
        containment_manager=process_executor.containment_manager,
        autodetect_containment=False,
        require_containment=settings.require_containment,
    )
    await terminal_supervisor.recover(node_id=settings.node_id)
    process_supervisor = ProcessSupervisor(
        workflow_execution_repository,
        runner_paths,
        process_executor=process_executor,
    )
    await process_supervisor.recover()
    remote_supervisor = RemoteExecutionSupervisor(
        execution_repository,
        runner_paths,
        runner_control_service,
        node_service,
    )
    execution_runner = NodeExecutionRouter(
        local_node_id=settings.node_id,
        repository=execution_repository,
        local=process_supervisor,
        remote=remote_supervisor,
        local_terminal=terminal_supervisor,
    )
    remote_terminal_supervisor = RemoteTerminalSupervisor(
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        event_repository=event_repository,
        control=runner_control_service,
        paths=runner_paths,
    )
    terminal_controller = NodeTerminalRouter(
        local_node_id=settings.node_id,
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        local=terminal_supervisor,
        remote=remote_terminal_supervisor,
    )
    browser_manager = RunnerBrowserManager(
        node_id=settings.node_id,
        paths=runner_paths,
    )
    browser_router = NodeBrowserRouter(
        local_node_id=settings.node_id,
        local=browser_manager,
        remote_factory=lambda node_id: RemoteBrowserClient(
            node_id=node_id, control=runner_control_service
        ),
    )
    artifact_service = ArtifactApplicationService(
        run_repository=run_repository,
        execution_repository=execution_repository,
        artifact_repository=artifact_repository,
        event_repository=event_repository,
        paths=runner_paths,
        max_artifact_bytes=settings.audit.max_artifact_bytes,
        audit_repository=audit_aggregate_repository,
    )
    browser_service = BrowserApplicationService(
        runs=run_repository,
        agent_sessions=agent_session_repository,
        repository=browser_repository,
        runner=browser_router,
        artifacts=artifact_service,
        events=event_repository,
    )
    target_http_service = TargetHttpApplicationService(
        runs=run_repository,
        tool_calls=tool_call_intent_repository,
        requests=SQLAlchemyTargetHttpRequestRepository(database.session_factory),
        runner=NodeTargetHttpRouter(
            local_node_id=settings.node_id,
            local=RunnerTargetHttpClient(node_id=settings.node_id),
            remote=RemoteTargetHttpClient(runner_control_service),
        ),
        artifacts=artifact_service,
        events=event_repository,
    )

    hooks = HookBus(audit_sink=RunEventHookAuditSink(event_repository))
    memory_writer = MemoryWriter(
        memory_repository,
        hooks=hooks,
        events=event_repository,
    )
    audit_service = _create_audit_service(
        settings,
        database,
        aggregate_repository=audit_aggregate_repository,
    )
    audit_preflight_service = _create_audit_preflight_service(
        settings,
        database,
        repository=audit_preflight_repository,
        source_ingest_available=_create_audit_preflight_availability_check(
            settings,
            node_service=node_service,
            credentials=runner_credential_repository,
        ),
    )
    audit_preflight_plan_service = _create_audit_preflight_plan_service(
        settings,
        database,
        preflight_repository=audit_preflight_repository,
    )
    workflow_router = RunWorkflowControlRouter(
        runs=run_repository,
        audits=audit_aggregate_repository,
        general=workflow_client,
    )
    safety_stopper = RunSafetyStopService(
        execution_repository=execution_repository,
        execution_runner=execution_runner,
        resource_stoppers={
            "browser_sessions": browser_service,
            "target_http_requests": target_http_service,
        },
    )
    run_service = RunApplicationService(
        engagement_repository=engagement_repository,
        run_repository=run_repository,
        event_repository=event_repository,
        workflow_client=workflow_router,
        execution_repository=execution_repository,
        execution_runner=execution_runner,
        workspace_root=settings.workspace_root,
        model_profiles=model_profile_service,
        safety_stopper=safety_stopper,
    )
    pentest_service = PentestApplicationService(
        creation_uow=SQLAlchemyPentestCreationUnitOfWork(database.session_factory),
        engagement_repository=engagement_repository,
        event_repository=event_repository,
        workflow_client=workflow_router,
        workspace_root=settings.workspace_root,
        model_profiles=model_profile_service,
        capability_resolver=PentestCapabilityResolver(
            tools=registry,
            skills=skill_registry,
            capabilities=capability_repository,
            packs=official_pack_catalog,
        ),
    )
    audit_control_service = AuditControlApplicationService(
        audits=audit_service,
        projector=AuditRunStateProjector(
            SQLAlchemyAuditControlUnitOfWork(database.session_factory)
        ),
        safety_stopper=safety_stopper,
        events=event_repository,
    )
    workflow_signal_repository = SQLAlchemyWorkflowSignalIntentRepository(
        database.session_factory
    )
    workflow_signal_transport = RoutedWorkflowSignalTransport(
        workflow_router,
        runs=run_repository,
        sources=workflow_signal_repository,
    )
    workflow_signal_dispatcher = WorkflowSignalDispatcher(
        repository=workflow_signal_repository,
        transport=workflow_signal_transport,
        lease_owner=f"control-plane:{settings.node_id}:{secrets.token_hex(16)}",
    )
    workflow_signal_reconciler = WorkflowSignalReconciler(
        repository=workflow_signal_repository,
        probe=TemporalWorkflowSignalOutcomeProbe(temporal_connector),
        lease_owner=f"control-plane-probe:{settings.node_id}:{secrets.token_hex(16)}",
    )

    control_plane = ControlPlane(
        settings=settings,
        database=database,
        run_service=run_service,
        pentest_service=pentest_service,
        audit_service=audit_service,
        action_service=ActionApplicationService(
            action_read_repository,
            authorizer=LocalObjectAuthorizer(settings.create_local_operator_security()),
        ),
        event_service=EventApplicationService(
            run_repository=run_repository,
            event_repository=event_repository,
            artifact_associations=artifact_repository,
        ),
        execution_service=ExecutionApplicationService(
            run_repository=run_repository,
            execution_repository=execution_repository,
            event_repository=event_repository,
            runner=execution_runner,
        ),
        node_service=node_service,
        runner_control_service=runner_control_service,
        finding_service=FindingApplicationService(
            run_repository=run_repository,
            finding_repository=finding_repository,
            artifact_repository=artifact_repository,
            execution_repository=execution_repository,
            event_repository=event_repository,
            memory_writer=memory_writer,
        ),
        report_service=ReportApplicationService(
            run_repository=run_repository,
            finding_repository=finding_repository,
            artifact_repository=artifact_repository,
            report_repository=report_repository,
            event_repository=event_repository,
            artifact_service=artifact_service,
        ),
        tool_service=ToolApplicationService(registry),
        model_profile_service=model_profile_service,
        approval_service=ApprovalApplicationService(
            approval_repository=approval_repository,
            run_repository=run_repository,
            event_repository=event_repository,
            runtime_approval_repository=runtime_approval_repository,
        ),
        artifact_service=artifact_service,
        context_service=ContextApplicationService(context_repository),
        memory_service=MemoryService(
            memory_repository,
            run_repository=run_repository,
        ),
        runtime_observability_service=RuntimeObservabilityService(
            SQLAlchemyRuntimeObservabilityRepository(database.session_factory)
        ),
        terminal_service=TerminalApplicationService(
            run_repository=run_repository,
            supervisor=terminal_controller,
            artifact_service=artifact_service,
            event_repository=event_repository,
            hooks=hooks,
        ),
        browser_service=browser_service,
        connector_service=ConnectorApplicationService(
            runs=run_service,
            submissions=SQLAlchemyConnectorSubmissionRepository(database.session_factory),
            artifacts=artifact_service,
        ),
        terminal_supervisor=terminal_supervisor,
        graph_repository=graph_repository,
        traffic_repository=traffic_repository,
        local_audit_job_service=local_audit_job_service,
        audit_control_service=audit_control_service,
        audit_preflight_service=audit_preflight_service,
        audit_preflight_plan_service=audit_preflight_plan_service,
        audit_preflight_runner_service=audit_preflight_runner_service,
        workflow_signal_dispatcher=workflow_signal_dispatcher,
        workflow_signal_reconciler=workflow_signal_reconciler,
        browser_manager=browser_manager,
        process_supervisor=process_supervisor,
        execution_runner=execution_runner,
        target_http_service=target_http_service,
    )
    control_plane.start_cleanup_reconciler()
    return control_plane


def _prepare_local_paths(settings: APISettings) -> None:
    _validate_audit_settings_path_isolation(settings)
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    settings.runner_state_path.mkdir(parents=True, exist_ok=True)
    if settings.audit.enabled:
        settings.audit.snapshot_root.mkdir(parents=True, exist_ok=True)
        settings.audit.temp_root.mkdir(parents=True, exist_ok=True)
    if settings.database_url.startswith("sqlite+aiosqlite:///"):
        raw_path = settings.database_url.removeprefix("sqlite+aiosqlite:///")
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    _validate_audit_settings_path_isolation(settings)


def _validate_audit_settings_path_isolation(settings: APISettings) -> None:
    try:
        validate_audit_storage_isolation(
            audit=settings.audit,
            workspace_root=settings.workspace_root,
            runner_state_path=settings.runner_state_path,
            runner_credential_path=settings.runner_credential_path,
            models_secrets_path=settings.model_secrets_path,
            local_principal_path=settings.local_principal_path,
            database_url=settings.database_url,
            temporal_tls_server_root_ca_path=settings.temporal_tls_server_root_ca_path,
            temporal_tls_client_cert_path=settings.temporal_tls_client_cert_path,
            temporal_tls_client_private_key_path=(settings.temporal_tls_client_private_key_path),
        )
    except ValueError as exc:
        raise RiftXConfigError(f"invalid Audit path isolation: {exc}") from None
