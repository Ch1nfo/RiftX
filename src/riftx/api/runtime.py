"""Production dependency assembly for the RiftX control plane."""

from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client

from riftx import __version__
from riftx.application.services import (
    ApprovalApplicationService,
    ArtifactApplicationService,
    EventApplicationService,
    ExecutionApplicationService,
    FindingApplicationService,
    NodeApplicationService,
    NodeRegistration,
    ReportApplicationService,
    RunApplicationService,
    RunnerControlService,
    RunWorkflowClient,
    TerminalApplicationService,
    ToolApplicationService,
)
from riftx.config import RiftXConfig, load_riftx_config
from riftx.context import ContextApplicationService
from riftx.hooks import HookBus, RunEventHookAuditSink
from riftx.memory import MemoryService, MemoryWriter
from riftx.persistence import (
    Database,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyReportRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunnerCommandRepository,
    SQLAlchemyRunnerCredentialRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyRuntimeApprovalRepository,
    SQLAlchemyTerminalRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.memory_repositories import SQLAlchemyMemoryRepository
from riftx.persistence.target_http_repositories import (
    SQLAlchemyTargetHttpRequestRepository,
)
from riftx.runner import (
    ExecutionRunner,
    NodeTargetHttpRouter,
    ProcessSupervisor,
    RemoteTargetHttpClient,
    RunnerPaths,
    RunnerTargetHttpClient,
    TerminalSupervisor,
)
from riftx.runner.remote import NodeExecutionRouter, RemoteExecutionSupervisor
from riftx.runner.remote_terminal import NodeTerminalRouter, RemoteTerminalSupervisor
from riftx.target_http.service import TargetHttpApplicationService
from riftx.temporal.runtime import TemporalRunClient, TemporalRuntimeConfig
from riftx.tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class APISettings:
    database_url: str = "sqlite+aiosqlite:///./.riftx/riftx.db"
    tools_config_path: Path = Path("configs/tools.example.yaml")
    node_id: str = "local"
    workspace_root: Path = Path(".riftx/workspaces")
    runner_state_path: Path = Path(".riftx/runner")
    web_dist_path: Path = Path("apps/web/dist")
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "riftx-v2"
    temporal_workflow_id_prefix: str = "riftx-run"
    sse_poll_interval_seconds: float = 0.5
    sse_heartbeat_seconds: float = 15.0
    node_offline_after_seconds: float = 30.0
    node_lost_after_seconds: float = 300.0
    runner_registration_token: str | None = None
    runner_command_lease_seconds: float = 30.0
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    @classmethod
    def from_config(cls, config: RiftXConfig) -> APISettings:
        return cls(
            database_url=config.database.url,
            tools_config_path=config.tools.path.expanduser(),
            node_id=config.runner.node_id,
            workspace_root=config.workspace.root.expanduser(),
            runner_state_path=config.runner.state_path.expanduser(),
            web_dist_path=config.web.dist_path.expanduser(),
            temporal_address=config.temporal.target,
            temporal_namespace=config.temporal.namespace,
            temporal_task_queue=config.temporal.task_queue,
            temporal_workflow_id_prefix=config.temporal.workflow_id_prefix,
            sse_poll_interval_seconds=config.server.sse_poll_interval_seconds,
            sse_heartbeat_seconds=config.server.sse_heartbeat_seconds,
            node_offline_after_seconds=config.runner.node_offline_after_seconds,
            node_lost_after_seconds=config.runner.node_lost_after_seconds,
            runner_registration_token=config.runner.registration_token,
            runner_command_lease_seconds=config.runner.command_lease_seconds,
            cors_origins=tuple(config.server.cors_origins),
        )

    @classmethod
    def from_environment(cls) -> APISettings:
        return cls.from_config(load_riftx_config())


@dataclass(slots=True)
class ControlPlane:
    settings: APISettings
    database: Database
    run_service: RunApplicationService
    event_service: EventApplicationService
    execution_service: ExecutionApplicationService
    finding_service: FindingApplicationService
    node_service: NodeApplicationService
    runner_control_service: RunnerControlService
    report_service: ReportApplicationService
    tool_service: ToolApplicationService
    approval_service: ApprovalApplicationService
    artifact_service: ArtifactApplicationService
    context_service: ContextApplicationService
    memory_service: MemoryService
    terminal_service: TerminalApplicationService
    terminal_supervisor: TerminalSupervisor
    process_supervisor: ProcessSupervisor | None = None
    execution_runner: ExecutionRunner | None = None
    target_http_service: TargetHttpApplicationService | None = None

    async def close(self) -> None:
        await self.terminal_supervisor.close_all()
        if self.process_supervisor is not None:
            await self.process_supervisor.close()
        await self.database.dispose()


class UnavailableRunWorkflowClient:
    """Keeps read-only API access available while Temporal is offline."""

    def __init__(self, config: TemporalRuntimeConfig, reason: str) -> None:
        self._config = config
        self._reason = reason

    async def start_run(self, run_id: str) -> object:
        self._raise(run_id)

    async def pause(self, run_id: str) -> None:
        self._raise(run_id)

    async def resume(self, run_id: str) -> None:
        self._raise(run_id)

    async def approve(self, run_id: str, call_id: str) -> None:
        self._raise(run_id)

    async def reject(self, run_id: str, call_id: str) -> None:
        self._raise(run_id)

    async def cancel_current_execution(self, run_id: str) -> None:
        self._raise(run_id)

    async def cancel(self, run_id: str) -> None:
        self._raise(run_id)

    async def compact(self, run_id: str, max_history_items: int = 100) -> None:
        self._raise(run_id)

    async def switch_model(self, run_id: str, model_profile: str) -> None:
        self._raise(run_id)

    async def append_user_message(self, run_id: str, message: str) -> None:
        self._raise(run_id)

    def workflow_id(self, run_id: str) -> str:
        return f"{self._config.workflow_id_prefix}-{run_id}"

    def _raise(self, run_id: str) -> None:
        raise RuntimeError(f"Temporal is unavailable for run {run_id!r}: {self._reason}")


async def build_control_plane(settings: APISettings) -> ControlPlane:
    _prepare_local_paths(settings)
    database = Database(settings.database_url)
    await database.create_schema()

    registry = ToolRegistry(settings.tools_config_path, node_id=settings.node_id)
    tool_snapshot = await registry.refresh()

    temporal_config = TemporalRuntimeConfig(
        task_queue=settings.temporal_task_queue,
        workflow_id_prefix=settings.temporal_workflow_id_prefix,
    )
    workflow_client: RunWorkflowClient
    try:
        temporal_client = await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
        )
        workflow_client = TemporalRunClient(temporal_client, temporal_config)
    except Exception as exc:
        logger.warning("Temporal unavailable during API startup: %s", exc)
        workflow_client = UnavailableRunWorkflowClient(temporal_config, str(exc))

    engagement_repository = SQLAlchemyEngagementRepository(database.session_factory)
    run_repository = SQLAlchemyRunRepository(database.session_factory)
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
    runner_credential_repository = SQLAlchemyRunnerCredentialRepository(database.session_factory)
    runner_command_repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    context_repository = SQLAlchemyContextCompilationRepository(database.session_factory)
    memory_repository = SQLAlchemyMemoryRepository(database.session_factory)
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
        paths=runner_paths,
        registration_token=settings.runner_registration_token,
        terminals=terminal_repository,
        events=event_repository,
        lease_duration=timedelta(seconds=settings.runner_command_lease_seconds),
    )
    terminal_supervisor = TerminalSupervisor(
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        event_repository=event_repository,
        paths=runner_paths,
    )
    await terminal_supervisor.recover(node_id=settings.node_id)
    process_supervisor = ProcessSupervisor(execution_repository, runner_paths)
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
    artifact_service = ArtifactApplicationService(
        run_repository=run_repository,
        execution_repository=execution_repository,
        artifact_repository=artifact_repository,
        event_repository=event_repository,
        paths=runner_paths,
    )

    hooks = HookBus(audit_sink=RunEventHookAuditSink(event_repository))
    memory_writer = MemoryWriter(
        memory_repository,
        hooks=hooks,
        events=event_repository,
    )

    return ControlPlane(
        settings=settings,
        database=database,
        run_service=RunApplicationService(
            engagement_repository=engagement_repository,
            run_repository=run_repository,
            event_repository=event_repository,
            workflow_client=workflow_client,
            workspace_root=settings.workspace_root,
        ),
        event_service=EventApplicationService(
            run_repository=run_repository,
            event_repository=event_repository,
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
        approval_service=ApprovalApplicationService(
            approval_repository=approval_repository,
            run_repository=run_repository,
            event_repository=event_repository,
            workflow_client=workflow_client,
            runtime_approval_repository=runtime_approval_repository,
        ),
        artifact_service=artifact_service,
        context_service=ContextApplicationService(context_repository),
        memory_service=MemoryService(memory_repository),
        terminal_service=TerminalApplicationService(
            run_repository=run_repository,
            supervisor=terminal_controller,
            artifact_service=artifact_service,
            event_repository=event_repository,
            hooks=hooks,
        ),
        terminal_supervisor=terminal_supervisor,
        process_supervisor=process_supervisor,
        execution_runner=execution_runner,
        target_http_service=TargetHttpApplicationService(
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
        ),
    )


def _prepare_local_paths(settings: APISettings) -> None:
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    settings.runner_state_path.mkdir(parents=True, exist_ok=True)
    if settings.database_url.startswith("sqlite+aiosqlite:///"):
        raw_path = settings.database_url.removeprefix("sqlite+aiosqlite:///")
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
