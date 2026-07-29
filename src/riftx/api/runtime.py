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
    FindingApplicationService,
    NodeApplicationService,
    NodeRegistration,
    ReportApplicationService,
    RunApplicationService,
    RunWorkflowClient,
    TerminalApplicationService,
    ToolApplicationService,
)
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
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.runner import RunnerPaths, TerminalSupervisor
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
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    @classmethod
    def from_environment(cls) -> APISettings:
        defaults = cls()
        return cls(
            database_url=os.getenv("RIFTX_DATABASE_URL", defaults.database_url),
            tools_config_path=Path(
                os.getenv("RIFTX_TOOLS_CONFIG", str(defaults.tools_config_path))
            ),
            node_id=os.getenv("RIFTX_NODE_ID", defaults.node_id),
            workspace_root=Path(os.getenv("RIFTX_WORKSPACE_ROOT", str(defaults.workspace_root))),
            runner_state_path=Path(
                os.getenv("RIFTX_RUNNER_STATE", str(defaults.runner_state_path))
            ),
            web_dist_path=Path(os.getenv("RIFTX_WEB_DIST", str(defaults.web_dist_path))),
            temporal_address=os.getenv("RIFTX_TEMPORAL_ADDRESS", defaults.temporal_address),
            temporal_namespace=os.getenv("RIFTX_TEMPORAL_NAMESPACE", defaults.temporal_namespace),
            temporal_task_queue=os.getenv(
                "RIFTX_TEMPORAL_TASK_QUEUE", defaults.temporal_task_queue
            ),
            temporal_workflow_id_prefix=os.getenv(
                "RIFTX_TEMPORAL_WORKFLOW_ID_PREFIX",
                defaults.temporal_workflow_id_prefix,
            ),
            sse_poll_interval_seconds=float(
                os.getenv(
                    "RIFTX_SSE_POLL_INTERVAL_SECONDS",
                    str(defaults.sse_poll_interval_seconds),
                )
            ),
            sse_heartbeat_seconds=float(
                os.getenv(
                    "RIFTX_SSE_HEARTBEAT_SECONDS",
                    str(defaults.sse_heartbeat_seconds),
                )
            ),
            node_offline_after_seconds=float(
                os.getenv(
                    "RIFTX_NODE_OFFLINE_AFTER_SECONDS",
                    str(defaults.node_offline_after_seconds),
                )
            ),
            node_lost_after_seconds=float(
                os.getenv(
                    "RIFTX_NODE_LOST_AFTER_SECONDS",
                    str(defaults.node_lost_after_seconds),
                )
            ),
            cors_origins=tuple(
                item.strip()
                for item in os.getenv(
                    "RIFTX_CORS_ORIGINS",
                    ",".join(defaults.cors_origins),
                ).split(",")
                if item.strip()
            ),
        )


@dataclass(slots=True)
class ControlPlane:
    settings: APISettings
    database: Database
    run_service: RunApplicationService
    event_service: EventApplicationService
    finding_service: FindingApplicationService
    node_service: NodeApplicationService
    report_service: ReportApplicationService
    tool_service: ToolApplicationService
    approval_service: ApprovalApplicationService
    artifact_service: ArtifactApplicationService
    terminal_service: TerminalApplicationService
    terminal_supervisor: TerminalSupervisor

    async def close(self) -> None:
        await self.terminal_supervisor.close_all()
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
    execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
    terminal_repository = SQLAlchemyTerminalRepository(database.session_factory)
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
            labels={"mode": "local"},
        )
    )
    runner_paths = RunnerPaths(settings.runner_state_path)
    terminal_supervisor = TerminalSupervisor(
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        event_repository=event_repository,
        paths=runner_paths,
    )
    await terminal_supervisor.recover()
    artifact_service = ArtifactApplicationService(
        run_repository=run_repository,
        execution_repository=execution_repository,
        artifact_repository=artifact_repository,
        event_repository=event_repository,
        paths=runner_paths,
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
        node_service=node_service,
        finding_service=FindingApplicationService(
            run_repository=run_repository,
            finding_repository=finding_repository,
            artifact_repository=artifact_repository,
            execution_repository=execution_repository,
            event_repository=event_repository,
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
        ),
        artifact_service=artifact_service,
        terminal_service=TerminalApplicationService(
            run_repository=run_repository,
            supervisor=terminal_supervisor,
        ),
        terminal_supervisor=terminal_supervisor,
    )


def _prepare_local_paths(settings: APISettings) -> None:
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    settings.runner_state_path.mkdir(parents=True, exist_ok=True)
    if settings.database_url.startswith("sqlite+aiosqlite:///"):
        raw_path = settings.database_url.removeprefix("sqlite+aiosqlite:///")
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
