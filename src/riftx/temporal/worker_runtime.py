"""Production dependency assembly for the RiftX Temporal Worker."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from riftx import __version__
from riftx.agent import AgentCycle, AgentRuntimeServices, SQLAlchemyCheckpointStore
from riftx.application.services import (
    ApprovalRequestRecorder,
    ArtifactApplicationService,
    FindingApplicationService,
    NodeApplicationService,
    NodeRegistration,
    ReportApplicationService,
    RunnerControlService,
    TerminalApplicationService,
)
from riftx.config import RiftXConfig
from riftx.models import RiftXModelProvider, load_models_config
from riftx.persistence import (
    Database,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyReportRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunnerCommandRepository,
    SQLAlchemyRunnerCredentialRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.runner import ProcessSupervisor, RunnerPaths, TerminalSupervisor
from riftx.runner.remote import NodeExecutionRouter, RemoteExecutionSupervisor
from riftx.runner.remote_terminal import NodeTerminalRouter, RemoteTerminalSupervisor
from riftx.skills import create_default_skill_registry
from riftx.tools import ToolRegistry

from .activities import RiftXActivities
from .runtime import TemporalRuntimeConfig, create_worker


@dataclass(slots=True)
class TemporalWorkerRuntime:
    worker: Worker
    database: Database
    process_supervisor: ProcessSupervisor
    terminal_supervisor: TerminalSupervisor
    model_provider: RiftXModelProvider
    _closed: bool = False

    async def run(self) -> None:
        try:
            await self.worker.run()
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.terminal_supervisor.close_all()
        await self.process_supervisor.close()
        await self.model_provider.aclose()
        await self.database.dispose()


async def build_temporal_worker(
    config: RiftXConfig,
    *,
    temporal_client: Client | None = None,
) -> TemporalWorkerRuntime:
    """Build all production Activity dependencies and one Temporal Worker."""

    _prepare_local_paths(config)
    database = Database(config.database.url)
    model_provider: RiftXModelProvider | None = None
    process_supervisor: ProcessSupervisor | None = None
    terminal_supervisor: TerminalSupervisor | None = None
    try:
        await database.create_schema()
        registry = ToolRegistry(config.tools.path.expanduser(), node_id=config.runner.node_id)
        tool_snapshot = await registry.refresh()

        run_repository = SQLAlchemyRunRepository(database.session_factory)
        event_repository = SQLAlchemyRunEventRepository(database.session_factory)
        execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
        finding_repository = SQLAlchemyFindingRepository(database.session_factory)
        artifact_repository = SQLAlchemyArtifactRepository(database.session_factory)
        report_repository = SQLAlchemyReportRepository(database.session_factory)
        approval_repository = SQLAlchemyApprovalRepository(database.session_factory)
        node_repository = SQLAlchemyNodeRepository(database.session_factory)
        terminal_repository = SQLAlchemyTerminalRepository(database.session_factory)
        runner_credential_repository = SQLAlchemyRunnerCredentialRepository(
            database.session_factory
        )
        runner_command_repository = SQLAlchemyRunnerCommandRepository(database.session_factory)

        node_service = NodeApplicationService(
            node_repository,
            offline_after=timedelta(seconds=config.runner.node_offline_after_seconds),
            lost_after=timedelta(seconds=config.runner.node_lost_after_seconds),
        )
        await node_service.register(
            NodeRegistration(
                node_id=config.runner.node_id,
                name=platform.node() or config.runner.node_id,
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
                    "mode": "worker-local",
                    "shell": os.environ.get("SHELL") or os.environ.get("COMSPEC", "unknown"),
                    "working_directory": str(Path.cwd()),
                    "tool_count": str(len(tool_snapshot.definitions)),
                },
            )
        )

        paths = RunnerPaths(config.runner.state_path.expanduser())
        runner_control = RunnerControlService(
            credentials=runner_credential_repository,
            commands=runner_command_repository,
            nodes=node_service,
            executions=execution_repository,
            paths=paths,
            registration_token=config.runner.registration_token,
            terminals=terminal_repository,
            events=event_repository,
            lease_duration=timedelta(seconds=config.runner.command_lease_seconds),
        )
        process_supervisor = ProcessSupervisor(execution_repository, paths)
        await process_supervisor.recover()
        remote_supervisor = RemoteExecutionSupervisor(
            execution_repository,
            paths,
            runner_control,
            node_service,
        )
        execution_runner = NodeExecutionRouter(
            local_node_id=config.runner.node_id,
            repository=execution_repository,
            local=process_supervisor,
            remote=remote_supervisor,
        )

        terminal_supervisor = TerminalSupervisor(
            terminal_repository=terminal_repository,
            execution_repository=execution_repository,
            event_repository=event_repository,
            paths=paths,
        )
        await terminal_supervisor.recover(node_id=config.runner.node_id)
        remote_terminal = RemoteTerminalSupervisor(
            terminal_repository=terminal_repository,
            execution_repository=execution_repository,
            event_repository=event_repository,
            control=runner_control,
            paths=paths,
        )
        terminal_router = NodeTerminalRouter(
            local_node_id=config.runner.node_id,
            terminal_repository=terminal_repository,
            execution_repository=execution_repository,
            local=terminal_supervisor,
            remote=remote_terminal,
        )

        artifact_service = ArtifactApplicationService(
            run_repository=run_repository,
            execution_repository=execution_repository,
            artifact_repository=artifact_repository,
            event_repository=event_repository,
            paths=paths,
        )
        finding_service = FindingApplicationService(
            run_repository=run_repository,
            finding_repository=finding_repository,
            artifact_repository=artifact_repository,
            execution_repository=execution_repository,
            event_repository=event_repository,
        )
        report_service = ReportApplicationService(
            run_repository=run_repository,
            finding_repository=finding_repository,
            artifact_repository=artifact_repository,
            report_repository=report_repository,
            event_repository=event_repository,
            artifact_service=artifact_service,
        )
        terminal_service = TerminalApplicationService(
            run_repository=run_repository,
            supervisor=terminal_router,
        )

        skill_registry = create_default_skill_registry()
        skill_registry.load_entry_points()
        models = load_models_config(config.models.path.expanduser())
        profile = config.models.profile or models.default_profile
        model_provider = RiftXModelProvider(models)
        agent_services = AgentRuntimeServices(
            tool_registry=registry,
            skill_registry=skill_registry,
            supervisor=execution_runner,
            finding_repository=finding_repository,
            event_repository=event_repository,
            finding_service=finding_service,
            artifact_service=artifact_service,
            terminal_service=terminal_service,
            approval_repository=approval_repository,
        )
        agent_cycle = AgentCycle(
            services=agent_services,
            session_factory=database.session_factory,
            checkpoint_store=SQLAlchemyCheckpointStore(database.session_factory),
            model=profile,
            model_provider=model_provider,
            max_history_items=config.agent.max_history_items,
            max_turns=config.agent.max_turns,
        )
        activities = RiftXActivities(
            run_repository=run_repository,
            event_repository=event_repository,
            execution_repository=execution_repository,
            tool_registry=registry,
            supervisor=execution_runner,
            agent_cycle=agent_cycle,
            approval_recorder=ApprovalRequestRecorder(
                approval_repository=approval_repository,
                event_repository=event_repository,
                tool_registry=registry,
            ),
            report_service=report_service,
            session_factory=database.session_factory,
        )
        client = temporal_client or await Client.connect(
            config.temporal.target,
            namespace=config.temporal.namespace,
        )
        worker_config = TemporalRuntimeConfig(
            task_queue=config.temporal.task_queue,
            workflow_id_prefix=config.temporal.workflow_id_prefix,
            max_concurrent_activities=config.temporal.max_concurrent_activities,
            max_cached_workflows=config.temporal.max_cached_workflows,
        )
        return TemporalWorkerRuntime(
            worker=create_worker(client, activities, worker_config),
            database=database,
            process_supervisor=process_supervisor,
            terminal_supervisor=terminal_supervisor,
            model_provider=model_provider,
        )
    except Exception:
        if terminal_supervisor is not None:
            await terminal_supervisor.close_all()
        if process_supervisor is not None:
            await process_supervisor.close()
        if model_provider is not None:
            await model_provider.aclose()
        await database.dispose()
        raise


def _prepare_local_paths(config: RiftXConfig) -> None:
    config.workspace.root.expanduser().mkdir(parents=True, exist_ok=True)
    config.runner.state_path.expanduser().mkdir(parents=True, exist_ok=True)
    if config.database.url.startswith("sqlite+aiosqlite:///"):
        raw_path = config.database.url.removeprefix("sqlite+aiosqlite:///")
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
