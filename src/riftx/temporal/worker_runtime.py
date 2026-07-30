"""Production dependency assembly for the RiftX Temporal Worker."""

from __future__ import annotations

import asyncio
import os
import platform
from dataclasses import dataclass
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
    NodeRegistration,
    ReportApplicationService,
    RunnerControlService,
    RuntimeApprovalRequestRecorder,
    TerminalApplicationService,
)
from riftx.config import RiftXConfig
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
    ExecutorType,
    MessageRole,
    MessageType,
    MessageVisibility,
    TranscriptMessageDraft,
)
from riftx.execution import (
    DeferredExecutionDispatcher,
    ExecutionService,
    RegistryDeferredExecutionResolver,
)
from riftx.memory import MemoryService
from riftx.memory.context_source import RetrievedMemoryContextSource
from riftx.models import RiftXModelProvider, load_models_config
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
from riftx.persistence.checkpoint_repositories import (
    SQLAlchemyContextCheckpointRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.memory_repositories import SQLAlchemyMemoryRepository
from riftx.persistence.working_memory_repositories import SQLAlchemyWorkingMemoryRepository
from riftx.runner import ProcessSupervisor, RunnerPaths, TerminalSupervisor
from riftx.runner.remote import NodeExecutionRouter, RemoteExecutionSupervisor
from riftx.runner.remote_terminal import NodeTerminalRouter, RemoteTerminalSupervisor
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import DeferredRuntimeAgentFactory, OpenAIAgentsEngine
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.types import AgentSession
from riftx.skills import create_default_skill_registry
from riftx.tools import RawToolDefinition, ToolContextManager, ToolDefinition, ToolRegistry

from .activities import RiftXActivities
from .runtime import TemporalRunClient, TemporalRuntimeConfig, create_worker
from .runtime_activity import RuntimeCycleActivities


class _PrimarySessionInitializer:
    def __init__(
        self,
        *,
        runs: SQLAlchemyRunRepository,
        sessions: SQLAlchemyAgentSessionRepository,
        default_model_profile: str,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._default_model_profile = default_model_profile

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
        session = AgentSession(
            id=session_id,
            run_id=run_id,
            model_profile=run.model_profile or self._default_model_profile,
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
        if execution.run_id != run_id:
            raise RepositoryConflictError(
                f"execution {execution_id!r} does not belong to Run {run_id!r}"
            )
        tool = self._tool_definition(execution.tool_id, execution.executor_type)
        item = processed_tool_result_context_item(
            await self._processor.process(execution, tool)
        )
        return {
            "id": item.id,
            "type": "tool_result",
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
        worker_config = TemporalRuntimeConfig(
            task_queue=config.temporal.task_queue,
            workflow_id_prefix=config.temporal.workflow_id_prefix,
            max_concurrent_activities=config.temporal.max_concurrent_activities,
            max_cached_workflows=config.temporal.max_cached_workflows,
        )
        client = temporal_client or await Client.connect(
            config.temporal.target,
            namespace=config.temporal.namespace,
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
        runner_credential_repository = SQLAlchemyRunnerCredentialRepository(
            database.session_factory
        )
        runner_command_repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
        agent_session_repository = SQLAlchemyAgentSessionRepository(database.session_factory)
        agent_cycle_repository = SQLAlchemyAgentCycleRepository(database.session_factory)
        agent_step_repository = SQLAlchemyAgentStepRepository(database.session_factory)
        provider_state_repository = SQLAlchemyProviderStateRepository(database.session_factory)
        run_lease_repository = SQLAlchemyRunLeaseRepository(database.session_factory)
        runtime_approval_repository = SQLAlchemyRuntimeApprovalRepository(
            database.session_factory
        )
        tool_call_intent_repository = SQLAlchemyToolCallIntentRepository(
            database.session_factory
        )
        transcript_repository = SQLAlchemyTranscriptRepository(database.session_factory)
        user_input_repository = SQLAlchemyUserInputRequestRepository(database.session_factory)
        context_compilation_repository = SQLAlchemyContextCompilationRepository(
            database.session_factory
        )
        working_memory_repository = SQLAlchemyWorkingMemoryRepository(database.session_factory)
        context_checkpoint_repository = SQLAlchemyContextCheckpointRepository(
            database.session_factory
        )
        memory_service = MemoryService(SQLAlchemyMemoryRepository(database.session_factory))

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
        process_supervisor = ProcessSupervisor(
            execution_repository,
            paths,
            on_completed=lambda execution: _signal_execution_completion(
                workflow_client,
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
            on_completed=lambda execution: _signal_execution_completion(
                workflow_client,
                run_id=execution.run_id,
                execution_id=execution.id,
            ),
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
        )
        deferred_dispatcher = DeferredExecutionDispatcher(
            tool_call_repository=tool_call_intent_repository,
            execution_service=execution_service,
            resolver=RegistryDeferredExecutionResolver(
                runs=run_repository,
                registry=registry,
            ),
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
                DeferredRuntimeAgentFactory(),
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
        )
        runtime_cycle_activities = RuntimeCycleActivities(
            runtime_coordinator,
            worker_id=config.runner.node_id,
            session_initializer=_PrimarySessionInitializer(
                runs=run_repository,
                sessions=agent_session_repository,
                default_model_profile=profile,
            ),
            user_input_resolver=_RunEventUserInputResolver(
                events=event_repository,
                sessions=agent_session_repository,
                transcript=transcript_repository,
                requests=user_input_repository,
            ),
            execution_input_resolver=_CompletedExecutionInputResolver(
                executions=execution_service,
                registry=registry,
                processor=tool_result_processor,
            ),
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


async def _signal_execution_completion(
    workflow_client: TemporalRunClient,
    *,
    run_id: str,
    execution_id: str,
) -> None:
    for attempt in range(3):
        try:
            await workflow_client.execution_completed(run_id, execution_id)
            return
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)
