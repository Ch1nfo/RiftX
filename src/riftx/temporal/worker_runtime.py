"""Production dependency assembly for the RiftX Temporal Worker."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from riftx import __version__
from riftx.agent import AgentCycle, AgentRuntimeServices, SQLAlchemyCheckpointStore
from riftx.application.errors import RepositoryConflictError
from riftx.application.services import (
    ApprovalRequestRecorder,
    ArtifactApplicationService,
    FindingApplicationService,
    NodeApplicationService,
    NodeHeartbeat,
    NodeRegistration,
    ReportApplicationService,
    RunnerControlService,
    RunSafetyStopService,
    RuntimeApprovalRequestRecorder,
    TerminalApplicationService,
    stop_resources_payload,
)
from riftx.browser.service import BrowserApplicationService
from riftx.config import RiftXConfig, validate_audit_storage_isolation
from riftx.context import (
    ContextApplicationService,
    ContextCompiler,
    ExecutionArtifactStore,
    StableInstructionSource,
    ToolResultProcessor,
    TranscriptContextSource,
    WorkingMemoryContextSource,
    processed_tool_result_context_item,
)
from riftx.context.compaction import ContextCompactionManager
from riftx.domain import (
    Execution,
    ExecutorType,
    InvalidStateTransitionError,
    MessageRole,
    MessageType,
    MessageVisibility,
    RunKind,
    RunStatus,
    TranscriptMessageDraft,
)
from riftx.domain.base import utc_now
from riftx.execution import (
    DeferredExecutionDispatcher,
    ExecutionService,
    ExecutionWaitStatus,
    RegistryDeferredExecutionResolver,
)
from riftx.executors import DirectProcessExecutor, LinuxCgroupV2Manager
from riftx.hooks import HookBus, RunEventHookAuditSink
from riftx.memory import MemoryService, MemoryWriter
from riftx.memory.context_source import RetrievedMemoryContextSource
from riftx.models import ModelProfileRegistry, RiftXModelProvider
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyReportRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunLeaseRepository,
    SQLAlchemyRunnerCommandRepository,
    SQLAlchemyRunnerCredentialRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyRuntimeApprovalRepository,
    SQLAlchemyTerminalRepository,
    SQLAlchemyToolCallIntentRepository,
    SQLAlchemyTranscriptRepository,
    SQLAlchemyUserInputRequestRepository,
)
from riftx.persistence.browser_repositories import SQLAlchemyBrowserRepository
from riftx.persistence.checkpoint_repositories import (
    SQLAlchemyContextCheckpointRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.memory_repositories import SQLAlchemyMemoryRepository
from riftx.persistence.target_http_repositories import SQLAlchemyTargetHttpRequestRepository
from riftx.persistence.working_memory_repositories import SQLAlchemyWorkingMemoryRepository
from riftx.runner import (
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
from riftx.runtime.control_tools import RuntimeControlToolService
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import DeferredRuntimeAgentFactory, OpenAIAgentsEngine
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.session import SessionManager
from riftx.runtime.types import AgentSession
from riftx.skills import create_default_skill_registry
from riftx.subagents import (
    DurableSubagentTaskRunner,
    ModelDelegationExecutor,
    PrimaryResultMerger,
    SubagentManager,
    SubagentOrchestrator,
)
from riftx.target_http.service import TargetHttpApplicationService
from riftx.tools import RawToolDefinition, ToolContextManager, ToolDefinition, ToolRegistry

from .activities import RiftXActivities
from .connection import TemporalConnectionSettings, connect_temporal
from .runtime import TemporalRunClient, TemporalRuntimeConfig, create_worker
from .runtime_activity import RuntimeCycleActivities

logger = logging.getLogger(__name__)


class _PrimarySessionInitializer:
    def __init__(
        self,
        *,
        runs: SQLAlchemyRunRepository,
        sessions: SQLAlchemyAgentSessionRepository,
        model_registry: ModelProfileRegistry,
        profile_override: str | None = None,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._model_registry = model_registry
        self._profile_override = profile_override

    async def ensure_primary_session(self, run_id: str, session_id: str) -> None:
        existing = await self._sessions.get(session_id)
        if existing is not None:
            if existing.run_id != run_id:
                raise RepositoryConflictError(
                    f"agent session {session_id!r} belongs to another Run"
                )
            return
        run = await self._runs.get(run_id)
        if run is None:
            return
        default_model_profile = await asyncio.to_thread(
            self._model_registry.resolve,
            None,
            override=self._profile_override,
        )
        session = AgentSession(
            id=session_id,
            run_id=run_id,
            model_profile=run.model_profile or default_model_profile,
        )
        try:
            await self._sessions.create(session)
        except RepositoryConflictError:
            raced = await self._sessions.get(session_id)
            if raced is None or raced.run_id != run_id:
                raise


class _RunEventUserInputResolver:
    def __init__(
        self,
        *,
        events: SQLAlchemyRunEventRepository,
        sessions: SQLAlchemyAgentSessionRepository,
        transcript: SQLAlchemyTranscriptRepository,
        requests: SQLAlchemyUserInputRequestRepository | None = None,
    ) -> None:
        self._events = events
        self._sessions = sessions
        self._transcript = transcript
        self._requests = requests

    async def resolve_user_input(
        self,
        run_id: str,
        session_id: str,
        user_input_id: str,
    ) -> str:
        existing = await self._find_message(session_id, user_input_id)
        if existing is not None:
            await self._answer_pending(run_id, session_id, existing)
            return existing
        event = await self._events.get(user_input_id)
        if event is None or event.run_id != run_id or event.event_type != "user.message_queued":
            raise RepositoryConflictError(
                f"user input {user_input_id!r} does not belong to Run {run_id!r}"
            )
        message = event.payload.get("message")
        if not isinstance(message, str) or not message:
            raise RepositoryConflictError(f"user input {user_input_id!r} has no message")
        session = await self._sessions.get(session_id)
        if session is None or session.run_id != run_id:
            raise RepositoryConflictError(
                f"agent session {session_id!r} does not belong to Run {run_id!r}"
            )
        draft = TranscriptMessageDraft(
            agent_id=session.agent_type,
            role=MessageRole.USER,
            message_type=MessageType.USER_MESSAGE,
            content=message,
            structured_content={
                "role": MessageRole.USER.value,
                "content": message,
                "source_event_id": user_input_id,
            },
            visibility=MessageVisibility.USER_VISIBLE,
        )
        try:
            persisted = await self._transcript.append(session_id, draft)
            await self._answer_pending(run_id, session_id, persisted.id)
            return persisted.id
        except RepositoryConflictError:
            raced = await self._find_message(session_id, user_input_id)
            if raced is None:
                raise
            await self._answer_pending(run_id, session_id, raced)
            return raced

    async def _answer_pending(
        self,
        run_id: str,
        session_id: str,
        message_id: str,
    ) -> None:
        if self._requests is None:
            return
        request = await self._requests.pending_for_session(run_id, session_id)
        if request is None:
            return
        request.answer(message_id)
        await self._requests.save(request)

    async def _find_message(self, session_id: str, user_input_id: str) -> str | None:
        for message in await self._transcript.list_by_session(session_id):
            content = message.structured_content
            if isinstance(content, dict) and content.get("source_event_id") == user_input_id:
                return message.id
        return None


class _CompletedExecutionInputResolver:
    def __init__(
        self,
        *,
        executions: ExecutionService,
        registry: ToolRegistry,
        processor: ToolResultProcessor,
    ) -> None:
        self._executions = executions
        self._registry = registry
        self._processor = processor

    async def resolve_execution_input(
        self,
        run_id: str,
        execution_id: str,
    ) -> dict[str, object]:
        execution = (
            await self._executions.wait(
                execution_id,
                timeout_seconds=0.1,
            )
        ).execution
        return await self._to_context_input(run_id, execution)

    async def wait_for_execution_input(
        self,
        run_id: str,
        execution_id: str,
        *,
        timeout_seconds: float,
    ) -> dict[str, object]:
        result = await self._executions.wait(
            execution_id,
            timeout_seconds=timeout_seconds,
        )
        if result.wait_status is ExecutionWaitStatus.WAIT_TIMEOUT:
            raise TimeoutError(f"execution {execution_id!r} did not finish before timeout")
        return await self._to_context_input(run_id, result.execution)

    async def _to_context_input(
        self,
        run_id: str,
        execution: Execution,
    ) -> dict[str, object]:
        if execution.run_id != run_id:
            raise RepositoryConflictError(
                f"execution {execution.id!r} does not belong to Run {run_id!r}"
            )
        tool = self._tool_definition(execution.tool_id, execution.executor_type)
        item = processed_tool_result_context_item(await self._processor.process(execution, tool))
        return {
            "id": item.id,
            "type": "tool_result",
            "execution_id": execution.id,
            "tool_call_id": execution.tool_call_id,
            "content": item.content,
            "source_refs": item.source_refs,
            "priority": 100,
            "required": True,
            "compressible": False,
            "removable": False,
        }

    def _tool_definition(
        self,
        tool_id: str | None,
        executor_type: ExecutorType,
    ) -> ToolDefinition:
        if tool_id is not None and tool_id in self._registry.snapshot.definitions:
            return self._registry.snapshot.definitions[tool_id]
        return ToolDefinition.from_raw(
            tool_id or "runtime_execution",
            RawToolDefinition(
                command=["runtime-execution"],
                executor=executor_type,
            ),
        )


@dataclass(slots=True)
class TemporalWorkerRuntime:
    worker: Worker
    database: Database
    process_supervisor: ProcessSupervisor
    terminal_supervisor: TerminalSupervisor
    model_provider: RiftXModelProvider
    node_service: NodeApplicationService
    node_id: str
    heartbeat_interval_seconds: float
    browser_manager: RunnerBrowserManager | None = None
    run_repository: SQLAlchemyRunRepository | None = None
    event_repository: SQLAlchemyRunEventRepository | None = None
    safety_stopper: RunSafetyStopService | None = None
    _heartbeat_task: asyncio.Task[None] | None = None
    _safety_reconciler_task: asyncio.Task[None] | None = None
    _safety_failures: set[str] = field(default_factory=set)
    _closed: bool = False

    async def run(self) -> None:
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"riftx-node-heartbeat-{self.node_id}",
        )
        if self.run_repository is not None and self.safety_stopper is not None:
            self._safety_reconciler_task = asyncio.create_task(
                self._safety_reconciler_loop(),
                name=f"riftx-worker-safety-reconciler-{self.node_id}",
            )
        try:
            await self.worker.run()
        finally:
            await self.close()

    async def _safety_reconciler_loop(self) -> None:
        assert self.run_repository is not None
        assert self.safety_stopper is not None
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
                        runs = list(
                            await self.run_repository.list_for_reconciliation(
                                status=status,
                                created_through=created_through,
                                after_created_at=after_created_at,
                                after_id=after_id,
                                limit=100,
                            )
                        )
                        for run in runs:
                            try:
                                result = await self.safety_stopper.stop_run(run.id, drain=True)
                                if result.succeeded and status is RunStatus.COMPLETING:
                                    await self._reconcile_finalization(run.id, result)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                if run.id not in self._safety_failures:
                                    logger.exception(
                                        "Worker cleanup reconciliation failed for Run %s",
                                        run.id,
                                    )
                                self._safety_failures.add(run.id)
                            else:
                                if result.succeeded:
                                    self._safety_failures.discard(run.id)
                                elif run.id not in self._safety_failures:
                                    logger.warning(
                                        "Worker cleanup remains unconfirmed for Run %s: %s",
                                        run.id,
                                        result.failed_resource_types,
                                    )
                                    self._safety_failures.add(run.id)
                        if len(runs) < 100:
                            break
                        after_created_at = runs[-1].created_at
                        after_id = runs[-1].id
            except asyncio.CancelledError:
                raise
            except Exception:
                if not scan_unavailable:
                    logger.exception("Worker cleanup reconciliation scan failed; retrying")
                scan_unavailable = True
            else:
                if scan_unavailable:
                    logger.info("Worker cleanup reconciliation scan recovered")
                scan_unavailable = False
            await asyncio.sleep(0.1)

    async def _reconcile_finalization(self, run_id: str, stop_result: object) -> None:
        if self.event_repository is None:
            raise RepositoryConflictError(
                f"run {run_id!r} cannot reconcile finalization without an event repository"
            )
        intent = await self.run_repository.get_finalization_intent(run_id)
        if intent is None:
            raise RepositoryConflictError(f"run {run_id!r} has no trustworthy finalization target")

        current = await self.run_repository.get(run_id)
        if current is None:
            return
        if current.status in {RunStatus.COMPLETING, intent.target}:
            try:
                current = await self.run_repository.commit_finalization(
                    run_id,
                    intent.target,
                    defer_cleanup_event=intent.defer_cleanup_event,
                )
            except (InvalidStateTransitionError, RepositoryConflictError):
                current = await self.run_repository.get(run_id)
                if current is None:
                    return
                if current.status in {
                    RunStatus.PAUSING,
                    RunStatus.PAUSED,
                    RunStatus.CANCELLING,
                    RunStatus.CANCELLED,
                } or (
                    current.status in {RunStatus.COMPLETED, RunStatus.FAILED}
                    and current.status is not intent.target
                ):
                    return
                # A malformed intent or canonical-event collision is not a
                # control race. Surface it so the reconciler keeps retrying.
                raise
        if current.status is not intent.target:
            return

        stop_payload = stop_resources_payload(stop_result)
        await self.event_repository.append(
            run_id,
            "run.cleanup_reconciled",
            {
                "status": current.status.value,
                "stop_resources": stop_payload,
                "finalization_target": intent.target.value,
                "owner": "worker",
            },
        )

    async def _heartbeat_loop(self) -> None:
        unavailable = False
        while True:
            try:
                await self.node_service.heartbeat(self.node_id, NodeHeartbeat())
            except asyncio.CancelledError:
                raise
            except Exception:
                if not unavailable:
                    logger.exception("Local Worker node heartbeat failed for %s", self.node_id)
                unavailable = True
            else:
                if unavailable:
                    logger.info("Local Worker node heartbeat recovered for %s", self.node_id)
                unavailable = False
            await asyncio.sleep(self.heartbeat_interval_seconds)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        heartbeat_task = self._heartbeat_task
        self._heartbeat_task = None
        safety_task = self._safety_reconciler_task
        self._safety_reconciler_task = None
        if safety_task is not None:
            safety_task.cancel()
            try:
                await safety_task
            except asyncio.CancelledError:
                pass
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        try:
            await self.node_service.disconnect(self.node_id)
        except Exception:
            logger.exception("Unable to mark local Worker node %s offline", self.node_id)
        if self.browser_manager is not None:
            await self.browser_manager.close_all()
        await self.terminal_supervisor.close_all()
        await self.process_supervisor.close(cancel_running=True)
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
    browser_manager: RunnerBrowserManager | None = None
    try:
        await database.create_schema()
        registry = ToolRegistry(config.tools.path.expanduser(), node_id=config.runner.node_id)
        tool_snapshot = await registry.refresh()
        worker_config = TemporalRuntimeConfig(
            task_queue=config.temporal.task_queue,
            workflow_id_prefix=config.temporal.workflow_id_prefix,
            max_concurrent_activities=config.temporal.max_concurrent_activities,
            max_cached_workflows=config.temporal.max_cached_workflows,
        )
        client = temporal_client or await connect_temporal(
            TemporalConnectionSettings.from_config(config.temporal)
        )
        workflow_client = TemporalRunClient(client, worker_config)

        run_repository = SQLAlchemyRunRepository(database.session_factory)
        event_repository = SQLAlchemyRunEventRepository(database.session_factory)
        execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
        finding_repository = SQLAlchemyFindingRepository(database.session_factory)
        artifact_repository = SQLAlchemyArtifactRepository(database.session_factory)
        report_repository = SQLAlchemyReportRepository(database.session_factory)
        approval_repository = SQLAlchemyApprovalRepository(database.session_factory)
        node_repository = SQLAlchemyNodeRepository(database.session_factory)
        terminal_repository = SQLAlchemyTerminalRepository(database.session_factory)
        browser_repository = SQLAlchemyBrowserRepository(database.session_factory)
        runner_credential_repository = SQLAlchemyRunnerCredentialRepository(
            database.session_factory
        )
        runner_command_repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
        agent_session_repository = SQLAlchemyAgentSessionRepository(database.session_factory)
        agent_cycle_repository = SQLAlchemyAgentCycleRepository(database.session_factory)
        agent_step_repository = SQLAlchemyAgentStepRepository(database.session_factory)
        provider_state_repository = SQLAlchemyProviderStateRepository(database.session_factory)
        run_lease_repository = SQLAlchemyRunLeaseRepository(database.session_factory)
        runtime_approval_repository = SQLAlchemyRuntimeApprovalRepository(database.session_factory)
        tool_call_intent_repository = SQLAlchemyToolCallIntentRepository(database.session_factory)
        transcript_repository = SQLAlchemyTranscriptRepository(database.session_factory)
        user_input_repository = SQLAlchemyUserInputRequestRepository(database.session_factory)
        context_compilation_repository = SQLAlchemyContextCompilationRepository(
            database.session_factory
        )
        working_memory_repository = SQLAlchemyWorkingMemoryRepository(database.session_factory)
        context_checkpoint_repository = SQLAlchemyContextCheckpointRepository(
            database.session_factory
        )
        memory_repository = SQLAlchemyMemoryRepository(database.session_factory)
        memory_service = MemoryService(
            memory_repository,
            run_repository=run_repository,
        )
        hooks = HookBus(audit_sink=RunEventHookAuditSink(event_repository))
        memory_writer = MemoryWriter(
            memory_repository,
            hooks=hooks,
            events=event_repository,
        )

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
            runs=run_repository,
            paths=paths,
            registration_token=config.runner.registration_token,
            terminals=terminal_repository,
            events=event_repository,
            lease_duration=timedelta(seconds=config.runner.command_lease_seconds),
        )
        # The Worker is a production execution owner too.  Share one trusted
        # containment namespace between Process/Shell and PTY effects so the
        # Control Plane can resolve their durable containment identities during
        # an emergency stop even while Temporal or this Worker is unavailable.
        containment_manager = LinuxCgroupV2Manager.autodetect(
            payload_uid=config.execution.payload_uid,
            payload_gid=config.execution.payload_gid,
        )
        process_executor = DirectProcessExecutor(
            containment_manager=containment_manager,
            autodetect_containment=False,
            require_containment=config.execution.require_containment,
            defer_activation=True,
        )
        process_supervisor = ProcessSupervisor(
            execution_repository,
            paths,
            process_executor=process_executor,
            on_completed=lambda execution: _signal_execution_completion(
                workflow_client,
                run_repository,
                run_id=execution.run_id,
                execution_id=execution.id,
            ),
        )
        await process_supervisor.recover()
        remote_supervisor = RemoteExecutionSupervisor(
            execution_repository,
            paths,
            runner_control,
            node_service,
        )
        terminal_supervisor = TerminalSupervisor(
            terminal_repository=terminal_repository,
            execution_repository=execution_repository,
            event_repository=event_repository,
            paths=paths,
            containment_manager=process_executor.containment_manager,
            autodetect_containment=False,
            require_containment=config.execution.require_containment,
            on_completed=lambda execution: _signal_execution_completion(
                workflow_client,
                run_repository,
                run_id=execution.run_id,
                execution_id=execution.id,
            ),
        )
        await terminal_supervisor.recover(node_id=config.runner.node_id)
        execution_runner = NodeExecutionRouter(
            local_node_id=config.runner.node_id,
            repository=execution_repository,
            local=process_supervisor,
            remote=remote_supervisor,
            local_terminal=terminal_supervisor,
        )
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
            max_artifact_bytes=config.audit.max_artifact_bytes,
        )
        browser_manager = RunnerBrowserManager(
            node_id=config.runner.node_id,
            paths=paths,
        )
        browser_service = BrowserApplicationService(
            runs=run_repository,
            agent_sessions=agent_session_repository,
            repository=browser_repository,
            runner=NodeBrowserRouter(
                local_node_id=config.runner.node_id,
                local=browser_manager,
                remote_factory=lambda node_id: RemoteBrowserClient(
                    node_id=node_id,
                    control=runner_control,
                ),
            ),
            artifacts=artifact_service,
            events=event_repository,
        )
        target_http_service = TargetHttpApplicationService(
            runs=run_repository,
            tool_calls=tool_call_intent_repository,
            requests=SQLAlchemyTargetHttpRequestRepository(database.session_factory),
            runner=NodeTargetHttpRouter(
                local_node_id=config.runner.node_id,
                local=RunnerTargetHttpClient(node_id=config.runner.node_id),
                remote=RemoteTargetHttpClient(runner_control),
            ),
            artifacts=artifact_service,
            events=event_repository,
        )
        safety_stopper = RunSafetyStopService(
            execution_repository=execution_repository,
            execution_runner=execution_runner,
            resource_stoppers={
                "browser_sessions": browser_service,
                "target_http_requests": target_http_service,
            },
        )
        tool_result_processor = ToolResultProcessor(
            ExecutionArtifactStore(artifact_service),
            config=config.execution_output,
        )
        finding_service = FindingApplicationService(
            run_repository=run_repository,
            finding_repository=finding_repository,
            artifact_repository=artifact_repository,
            execution_repository=execution_repository,
            event_repository=event_repository,
            memory_writer=memory_writer,
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
            artifact_service=artifact_service,
            event_repository=event_repository,
            hooks=hooks,
        )

        skill_registry = create_default_skill_registry()
        skill_registry.load_entry_points()
        model_registry = ModelProfileRegistry(
            config.models.path.expanduser(),
            config.models.secrets_path.expanduser(),
        )
        await asyncio.to_thread(model_registry.refresh)
        profile = await asyncio.to_thread(
            model_registry.resolve,
            None,
            override=config.models.profile,
        )
        model_provider = RiftXModelProvider(model_registry)
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
        tool_context = ToolContextManager(registry)
        context_compiler = ContextCompiler(
            sources=[
                TranscriptContextSource(
                    transcript_repository,
                    max_items=config.agent.max_history_items or 100,
                ),
                WorkingMemoryContextSource(working_memory_repository),
                RetrievedMemoryContextSource(memory_service),
            ],
            stable_instruction_source=StableInstructionSource(),
            tool_context=tool_context,
            context_service=ContextApplicationService(context_compilation_repository),
        )
        execution_service = ExecutionService(
            execution_repository=execution_repository,
            session_repository=agent_session_repository,
            tool_call_repository=tool_call_intent_repository,
            runner=execution_runner,
            event_repository=event_repository,
            run_repository=run_repository,
        )
        deferred_dispatcher = DeferredExecutionDispatcher(
            tool_call_repository=tool_call_intent_repository,
            execution_service=execution_service,
            resolver=RegistryDeferredExecutionResolver(
                runs=run_repository,
                registry=registry,
                tool_context=tool_context,
            ),
        )
        control_tools = RuntimeControlToolService(
            tools=tool_context,
            executions=execution_service,
            artifacts=artifact_service,
            events=event_repository,
            transcript=transcript_repository,
        )
        runtime_coordinator = RuntimeCoordinator(
            run_repository=run_repository,
            session_repository=agent_session_repository,
            cycle_repository=agent_cycle_repository,
            step_repository=agent_step_repository,
            provider_state_repository=provider_state_repository,
            event_repository=event_repository,
            lease_manager=DatabaseRunLeaseManager(run_lease_repository),
            context_compiler=context_compiler,
            agent_engine=OpenAIAgentsEngine(
                DeferredRuntimeAgentFactory(control_handler=control_tools),
                model_provider=model_provider,
            ),
            transcript_repository=transcript_repository,
            deferred_execution_dispatcher=deferred_dispatcher,
            approval_repository=approval_repository,
            runtime_approval_repository=runtime_approval_repository,
            approval_recorder=RuntimeApprovalRequestRecorder(
                approval_repository=approval_repository,
                runtime_repository=runtime_approval_repository,
                event_repository=event_repository,
            ),
            user_input_repository=user_input_repository,
            terminal_service=terminal_service,
            safety_stopper=safety_stopper,
            hooks=hooks,
        )
        session_manager = SessionManager(
            run_repository=run_repository,
            session_repository=agent_session_repository,
            transcript_repository=transcript_repository,
            provider_state_repository=provider_state_repository,
        )
        execution_inputs = _CompletedExecutionInputResolver(
            executions=execution_service,
            registry=registry,
            processor=tool_result_processor,
        )
        subagent_manager = SubagentManager(
            sessions=session_manager,
            session_repository=agent_session_repository,
            tool_context=tool_context,
            limits=config.subagents,
            events=event_repository,
            result_merger=PrimaryResultMerger(
                working_memory_repository,
                memory_writer=memory_writer,
            ),
            hooks=hooks,
        )
        runtime_coordinator.bind_subagent_executor(
            ModelDelegationExecutor(
                SubagentOrchestrator(
                    subagent_manager,
                    DurableSubagentTaskRunner(
                        coordinator=runtime_coordinator,
                        sessions=session_manager,
                        execution_inputs=execution_inputs,
                        worker_id=config.runner.node_id,
                    ),
                )
            )
        )
        runtime_cycle_activities = RuntimeCycleActivities(
            runtime_coordinator,
            worker_id=config.runner.node_id,
            session_initializer=_PrimarySessionInitializer(
                runs=run_repository,
                sessions=agent_session_repository,
                model_registry=model_registry,
                profile_override=config.models.profile,
            ),
            user_input_resolver=_RunEventUserInputResolver(
                events=event_repository,
                sessions=agent_session_repository,
                transcript=transcript_repository,
                requests=user_input_repository,
            ),
            execution_input_resolver=execution_inputs,
        )
        activities = RiftXActivities(
            run_repository=run_repository,
            event_repository=event_repository,
            tool_registry=registry,
            safety_stopper=safety_stopper,
            agent_cycle=agent_cycle,
            approval_recorder=ApprovalRequestRecorder(
                approval_repository=approval_repository,
                event_repository=event_repository,
                tool_registry=registry,
            ),
            report_service=report_service,
            session_factory=database.session_factory,
            compaction_manager=ContextCompactionManager(
                runs=run_repository,
                sessions=agent_session_repository,
                transcript=transcript_repository,
                working_memory=working_memory_repository,
                compilations=context_compilation_repository,
                checkpoints=context_checkpoint_repository,
                approvals=runtime_approval_repository,
                executions=execution_repository,
                terminals=terminal_repository,
                context_compiler=context_compiler,
            ),
        )
        return TemporalWorkerRuntime(
            worker=create_worker(
                client,
                activities,
                worker_config,
                runtime_cycle_activities=runtime_cycle_activities,
            ),
            database=database,
            process_supervisor=process_supervisor,
            terminal_supervisor=terminal_supervisor,
            browser_manager=browser_manager,
            run_repository=run_repository,
            event_repository=event_repository,
            safety_stopper=safety_stopper,
            model_provider=model_provider,
            node_service=node_service,
            node_id=config.runner.node_id,
            heartbeat_interval_seconds=min(
                10.0,
                max(0.25, config.runner.node_offline_after_seconds / 3.0),
                config.runner.node_offline_after_seconds / 2.0,
            ),
        )
    except Exception:
        if browser_manager is not None:
            await browser_manager.close_all()
        if terminal_supervisor is not None:
            await terminal_supervisor.close_all()
        if process_supervisor is not None:
            await process_supervisor.close(cancel_running=True)
        if model_provider is not None:
            await model_provider.aclose()
        await database.dispose()
        raise


def _prepare_local_paths(config: RiftXConfig) -> None:
    _validate_audit_config_path_isolation(config)
    config.workspace.root.expanduser().mkdir(parents=True, exist_ok=True)
    config.runner.state_path.expanduser().mkdir(parents=True, exist_ok=True)
    if config.database.url.startswith("sqlite+aiosqlite:///"):
        raw_path = config.database.url.removeprefix("sqlite+aiosqlite:///")
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    _validate_audit_config_path_isolation(config)


def _validate_audit_config_path_isolation(config: RiftXConfig) -> None:
    validate_audit_storage_isolation(
        audit=config.audit,
        workspace_root=config.workspace.root,
        runner_state_path=config.runner.state_path,
        runner_credential_path=config.runner.credential_path,
        models_secrets_path=config.models.secrets_path,
        local_principal_path=config.security.local_principal_path,
        database_url=config.database.url,
        temporal_tls_server_root_ca_path=config.temporal.tls_server_root_ca_path,
        temporal_tls_client_cert_path=config.temporal.tls_client_cert_path,
        temporal_tls_client_private_key_path=config.temporal.tls_client_private_key_path,
    )


async def _signal_execution_completion(
    workflow_client: TemporalRunClient,
    run_repository: SQLAlchemyRunRepository,
    *,
    run_id: str,
    execution_id: str,
) -> None:
    run = await run_repository.get(run_id)
    if run is None or run.kind is not RunKind.GENERAL:
        # Audit execution completion requires the kind-aware workflow router
        # introduced by AUD-106. Physical cleanup remains complete; only the
        # unsafe generic Temporal signal is suppressed here.
        return
    for attempt in range(3):
        try:
            await workflow_client.execution_completed(run_id, execution_id)
            return
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)
