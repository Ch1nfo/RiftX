"""Production dependency assembly for the RiftX control plane."""

from __future__ import annotations

import asyncio
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
    EventApplicationService,
    ExecutionApplicationService,
    FindingApplicationService,
    ModelProfileApplicationService,
    NodeApplicationService,
    NodeRegistration,
    ReportApplicationService,
    RunApplicationService,
    RunnerControlService,
    RunWorkflowClient,
    TerminalApplicationService,
    ToolApplicationService,
)
from riftx.browser.service import BrowserApplicationService
from riftx.config import (
    AuditConfig,
    RiftXConfig,
    RiftXConfigError,
    load_riftx_config,
    validate_audit_storage_isolation,
)
from riftx.connectors.service import ConnectorApplicationService
from riftx.context import ContextApplicationService
from riftx.domain import OperatorCapability, RunStatus, TrustProfile
from riftx.domain.base import utc_now
from riftx.executors import DirectProcessExecutor, LinuxCgroupV2Manager
from riftx.hooks import HookBus, RunEventHookAuditSink
from riftx.memory import MemoryService, MemoryWriter
from riftx.models import ModelProfileRegistry
from riftx.observability import RuntimeObservabilityService
from riftx.persistence import (
    Database,
    SQLAlchemyActionReadRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyAuditCreationUnitOfWork,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyGraphReadRepository,
    SQLAlchemyNodeRepository,
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
from riftx.target_http.service import TargetHttpApplicationService
from riftx.temporal.connection import TemporalConnectionSettings, connect_temporal
from riftx.temporal.runtime import LazyTemporalRunClient, TemporalRuntimeConfig
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
) -> AuditApplicationService:
    """Assemble the always-present, database-only Code Audit application edge."""

    return AuditApplicationService(
        creation_uow=SQLAlchemyAuditCreationUnitOfWork(database.session_factory),
        aggregate_repository=SQLAlchemyAuditAggregateReadRepository(database.session_factory),
        feature_enabled=settings.audit.enabled,
        workspace_root=settings.audit.temp_root,
    )


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
    browser_service: BrowserApplicationService | None = None
    connector_service: ConnectorApplicationService | None = None
    browser_manager: RunnerBrowserManager | None = None
    process_supervisor: ProcessSupervisor | None = None
    execution_runner: ExecutionRunner | None = None
    target_http_service: TargetHttpApplicationService | None = None
    _cleanup_reconciler_task: asyncio.Task[None] | None = None
    _cleanup_failures: set[str] = field(default_factory=set)

    def start_cleanup_reconciler(self) -> None:
        """Start owner-process cleanup for in-memory local effect handles."""

        if self._cleanup_reconciler_task is not None:
            return
        self._cleanup_reconciler_task = asyncio.create_task(
            self._reconcile_completing_runs(),
            name="riftx-control-plane-cleanup-reconciler",
        )

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
                                result = await self.run_service.stop_resources_for_cleanup(run.id)
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

    registry = ToolRegistry(settings.tools_config_path, node_id=settings.node_id)
    tool_snapshot = await registry.refresh()
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
    workflow_client: RunWorkflowClient = LazyTemporalRunClient(
        _create_temporal_connector(settings),
        temporal_config,
    )

    engagement_repository = SQLAlchemyEngagementRepository(database.session_factory)
    run_repository = SQLAlchemyRunRepository(database.session_factory)
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
    tool_call_intent_repository = SQLAlchemyToolCallIntentRepository(database.session_factory)
    terminal_repository = SQLAlchemyTerminalRepository(database.session_factory)
    agent_session_repository = SQLAlchemyAgentSessionRepository(database.session_factory)
    browser_repository = SQLAlchemyBrowserRepository(database.session_factory)
    runner_credential_repository = SQLAlchemyRunnerCredentialRepository(database.session_factory)
    runner_command_repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
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
        executions=execution_repository,
        runs=run_repository,
        paths=runner_paths,
        registration_token=settings.runner_registration_token,
        terminals=terminal_repository,
        events=event_repository,
        lease_duration=timedelta(seconds=settings.runner_command_lease_seconds),
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
        execution_repository=execution_repository,
        event_repository=event_repository,
        paths=runner_paths,
        containment_manager=process_executor.containment_manager,
        autodetect_containment=False,
        require_containment=settings.require_containment,
    )
    await terminal_supervisor.recover(node_id=settings.node_id)
    process_supervisor = ProcessSupervisor(
        execution_repository,
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
    run_service = RunApplicationService(
        engagement_repository=engagement_repository,
        run_repository=run_repository,
        event_repository=event_repository,
        workflow_client=workflow_client,
        execution_repository=execution_repository,
        execution_runner=execution_runner,
        workspace_root=settings.workspace_root,
        model_profiles=model_profile_service,
        resource_stoppers={
            "browser_sessions": browser_service,
            "target_http_requests": target_http_service,
        },
    )

    control_plane = ControlPlane(
        settings=settings,
        database=database,
        run_service=run_service,
        audit_service=_create_audit_service(settings, database),
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
            workflow_client=workflow_client,
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
