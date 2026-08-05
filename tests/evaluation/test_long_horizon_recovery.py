from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select

from riftx.application.services import (
    ApprovalApplicationService,
    ArtifactApplicationService,
    DecideApproval,
    RuntimeApprovalRequestRecorder,
)
from riftx.application.services.artifacts import RegisterArtifact, RegisterArtifactContent
from riftx.browser.service import ActBrowser, BrowserApplicationService, OpenBrowser
from riftx.config import SubagentConfig
from riftx.context import (
    AttemptRecord,
    AttemptStatus,
    ContextApplicationService,
    ContextCompiler,
    CurrentFocus,
    TranscriptContextSource,
    UserDecision,
    WorkingMemory,
)
from riftx.context.compaction import (
    CompactContextCommand,
    ContextCompactionManager,
    SwitchModelCommand,
)
from riftx.domain import (
    BrowserActionStatus,
    BrowserActionType,
    BrowserOwner,
    Engagement,
    ExecutionStatus,
    ExecutorType,
    MessageRole,
    MessageType,
    MessageVisibility,
    Objective,
    Run,
    RunStatus,
    Scope,
    TranscriptMessageDraft,
)
from riftx.domain.base import utc_now
from riftx.evaluation import (
    InjectedRecoveryFault,
    LongHorizonEvaluator,
    LongHorizonEvidence,
    OneShotFaultInjector,
    RecoveryBoundary,
)
from riftx.execution import DeferredExecutionDispatcher, ExecutionService
from riftx.hooks import (
    HookBus,
    HookDecision,
    HookFailurePolicy,
    HookPoint,
    HookRegistration,
    HookRequest,
    HookResult,
    PythonHook,
)
from riftx.observability import RuntimeMetricName, RuntimeObservabilityService
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyBrowserRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunLeaseRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyRuntimeApprovalRepository,
    SQLAlchemyTerminalRepository,
    SQLAlchemyToolCallIntentRepository,
    SQLAlchemyTranscriptRepository,
)
from riftx.persistence.checkpoint_repositories import SQLAlchemyContextCheckpointRepository
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.observability_repository import (
    SQLAlchemyRuntimeObservabilityRepository,
)
from riftx.persistence.web_repositories import SQLAlchemyWebSourceRepository
from riftx.persistence.web_research_repositories import SQLAlchemyWebResearchRepository
from riftx.persistence.workflow_signals import WorkflowSignalIntentRecord
from riftx.persistence.working_memory_repositories import SQLAlchemyWorkingMemoryRepository
from riftx.runner import RunnerBrowserManager, RunnerPaths
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import AgentEngineEvent, AgentEngineEventType
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import RunCycleRequest
from riftx.runtime.session import SessionManager
from riftx.runtime.types import (
    AgentCycle,
    AgentStep,
    AgentStepType,
    ToolCallStatus,
)
from riftx.subagents import DelegationPacket, SubagentManager, SubagentResult, SubagentStatus
from riftx.temporal import RunAgentCycleActivityInput
from riftx.tools import ToolContextManager, ToolRegistry
from riftx.web import (
    EvidenceSpan,
    ExtractionStatus,
    SourceReference,
    SourceType,
    WebDocument,
    WebDocumentChunk,
    WebResearchPacket,
)
from riftx.web.research import ResearchClaim

from .support import (
    DurableEvaluationRunner,
    EvaluationBrowserEngine,
    EvaluationEngine,
    FaultingBrowserRunner,
    FaultingExecutionRunner,
    digest_json,
)


class FaultingCheckpointRepository:
    def __init__(
        self,
        delegate: SQLAlchemyContextCheckpointRepository,
        injector: OneShotFaultInjector,
    ) -> None:
        self._delegate = delegate
        self._injector = injector

    async def get(self, checkpoint_id: str):
        return await self._delegate.get(checkpoint_id)

    async def create(self, checkpoint):
        persisted = await self._delegate.create(checkpoint)
        self._injector.trip(RecoveryBoundary.DURING_COMPACTION)
        return persisted


async def _fault_hook(
    injector: OneShotFaultInjector,
    boundary: RecoveryBoundary,
    _: HookRequest,
) -> HookResult:
    injector.trip(boundary)
    return HookResult(decision=HookDecision.CONTINUE)


def _delegation(index: int, workspace: Path) -> DelegationPacket:
    return DelegationPacket(
        task_id=f"qa-subagent-task-{index}",
        subagent_type="recon",
        task=f"Inspect authorized endpoint slice {index}",
        run_contract_summary="Authorized example.com assessment",
        relevant_scope=["example.com"],
        available_tool_ids=["qa-probe"],
        workspace=str(workspace),
    )


def _execution_service(
    *,
    runs: SQLAlchemyRunRepository,
    executions: SQLAlchemyExecutionRepository,
    sessions: SQLAlchemyAgentSessionRepository,
    intents: SQLAlchemyToolCallIntentRepository,
    events: SQLAlchemyRunEventRepository,
    runner: object,
) -> ExecutionService:
    return ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=intents,
        runner=runner,  # type: ignore[arg-type]
        event_repository=events,
        run_repository=runs,
    )


async def test_qa_01_long_horizon_and_recovery_gate(tmp_path: Path) -> None:
    database_path = tmp_path / "qa-01.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_paths = RunnerPaths(tmp_path / "runner")
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    cycles = SQLAlchemyAgentCycleRepository(database.session_factory)
    steps = SQLAlchemyAgentStepRepository(database.session_factory)
    providers = SQLAlchemyProviderStateRepository(database.session_factory)
    leases = SQLAlchemyRunLeaseRepository(database.session_factory)
    transcript = SQLAlchemyTranscriptRepository(database.session_factory)
    compilations = SQLAlchemyContextCompilationRepository(database.session_factory)
    checkpoints = SQLAlchemyContextCheckpointRepository(database.session_factory)
    intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    runtime_approvals = SQLAlchemyRuntimeApprovalRepository(database.session_factory)
    approvals = SQLAlchemyApprovalRepository(database.session_factory)
    artifacts_repository = SQLAlchemyArtifactRepository(database.session_factory)
    working_memory_repository = SQLAlchemyWorkingMemoryRepository(database.session_factory)

    await engagements.create(Engagement(id="qa-engagement", name="QA-01"))
    run = Run(
        kind="general",
        id="qa-run",
        engagement_id="qa-engagement",
        node_id="local",
        objective=Objective(description="Assess example.com without leaving authorized scope"),
        scope=Scope(
            domains=["example.com"],
            url_prefixes=["https://example.com/"],
            exclusions=["admin.example.com"],
        ),
        model_profile="model-a",
        workspace_path=str(workspace),
        temporal_workflow_id="riftx-run-qa-run",
    )
    await runs.create(run)
    session_manager = SessionManager(
        run_repository=runs,
        session_repository=sessions,
        transcript_repository=transcript,
        provider_state_repository=providers,
    )
    await session_manager.create_session(
        run_id=run.id,
        model_profile="model-a",
        session_id="qa-primary",
    )
    objective_before = run.objective.description
    scope_digest_before = digest_json(run.scope.model_dump(mode="json"))
    injector = OneShotFaultInjector()

    # Context compile and model-call failures execute through the real Coordinator
    # and audited Hook boundary. The next cycle repairs from durable DB state.
    compiler = ContextCompiler(
        sources=[TranscriptContextSource(transcript)],
        context_service=ContextApplicationService(compilations),
    )
    hook_bus = HookBus()
    for point, boundary in (
        (HookPoint.AFTER_CONTEXT_COMPILE, RecoveryBoundary.AFTER_CONTEXT_COMPILE),
        (HookPoint.AFTER_MODEL_CALL, RecoveryBoundary.AFTER_MODEL_CALL),
    ):
        hook_bus.register(
            HookRegistration(
                f"qa-fault-{boundary.value}",
                point,
                PythonHook(
                    lambda request, boundary=boundary: _fault_hook(injector, boundary, request)
                ),
                failure_policy=HookFailurePolicy.BLOCK,
            )
        )
    engine = EvaluationEngine()
    coordinator = RuntimeCoordinator(
        run_repository=runs,
        session_repository=sessions,
        cycle_repository=cycles,
        step_repository=steps,
        provider_state_repository=providers,
        event_repository=events,
        lease_manager=DatabaseRunLeaseManager(leases),
        context_compiler=compiler,
        agent_engine=engine,
        transcript_repository=transcript,
        hooks=hook_bus,
    )
    with pytest.raises(Exception, match="after_context_compile"):
        await coordinator.run_cycle(
            RunCycleRequest(
                run_id=run.id,
                session_id="qa-primary",
                worker_id="worker-before-restart",
                cycle_id="qa-context-crash",
            )
        )
    with pytest.raises(Exception, match="after_model_call"):
        await coordinator.run_cycle(
            RunCycleRequest(
                run_id=run.id,
                session_id="qa-primary",
                worker_id="worker-before-restart",
                cycle_id="qa-model-crash",
            )
        )
    recovered_cycle = await coordinator.run_cycle(
        RunCycleRequest(
            run_id=run.id,
            session_id="qa-primary",
            worker_id="worker-before-restart",
            cycle_id="qa-model-recovered",
        )
    )
    assert recovered_cycle.yield_reason.value == "tool_running"
    assert engine.model_calls == 2

    tool_cycle = AgentCycle(
        id="qa-tool-cycle",
        run_id=run.id,
        session_id="qa-primary",
        sequence=len(await cycles.list_by_session("qa-primary")) + 1,
    )
    await cycles.create(tool_cycle)
    launch_counts: dict[str, int] = {}
    durable_runner = DurableEvaluationRunner(executions, launch_counts)
    faulting_runner = FaultingExecutionRunner(durable_runner, injector)
    execution_service = _execution_service(
        runs=runs,
        executions=executions,
        sessions=sessions,
        intents=intents,
        events=events,
        runner=faulting_runner,
    )
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=intents,
        execution_service=execution_service,
    )
    primary = await sessions.get("qa-primary")
    assert primary is not None
    approval_recorder = RuntimeApprovalRequestRecorder(
        approval_repository=approvals,
        runtime_repository=runtime_approvals,
        event_repository=events,
    )
    approval_service = ApprovalApplicationService(
        approval_repository=approvals,
        run_repository=runs,
        event_repository=events,
        runtime_approval_repository=runtime_approvals,
    )

    tool_call_ids: list[str] = []
    failed_tool_call_ids: list[str] = []
    processed_tool_call_ids: list[str] = []
    execution_by_tool_call: dict[str, str] = {}
    approval_ids: list[str] = []
    tool_messages: list[TranscriptMessageDraft] = []

    for index in range(100):
        step = AgentStep(
            id=f"qa-tool-step-{index:03d}",
            cycle_id=tool_cycle.id,
            sequence=index + 1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        )
        await steps.create(step)
        event = AgentEngineEvent(
            sequence=index + 1,
            event_type=AgentEngineEventType.TOOL_CALL_READY,
            data={
                "call_id": f"qa-call-{index:03d}",
                "tool_id": f"qa-tool-{index:03d}",
                "arguments": {"index": index},
                "approval_level": "sensitive",
                "execution": {
                    "node_id": "local",
                    "executor_type": ExecutorType.PROCESS,
                    "cwd": str(workspace),
                    "argv": [sys.executable, "-c", f"print({index})"],
                },
            },
        )
        approval_required = 20 <= index < 23
        intent = await dispatcher.prepare(
            session=primary,
            cycle=tool_cycle,
            step=step,
            event=event,
            status=(ToolCallStatus.WAITING_APPROVAL if approval_required else ToolCallStatus.READY),
        )
        if index == 0:
            with pytest.raises(InjectedRecoveryFault):
                injector.trip(RecoveryBoundary.AFTER_TOOL_INTENT_PERSISTED)
            replayed = await dispatcher.prepare(
                session=primary,
                cycle=tool_cycle,
                step=step,
                event=event,
            )
            assert replayed.id == intent.id
        tool_call_ids.append(intent.id)

        if approval_required:
            request = await approval_recorder.record(
                run,
                session=primary,
                cycle=tool_cycle,
                step=step,
                intent=intent,
                context_compilation_id=(await compilations.latest_for_session(primary.id)).id,
                working_memory_version=None,
            )
            if index == 20:
                with pytest.raises(InjectedRecoveryFault):
                    injector.trip(RecoveryBoundary.WHILE_WAITING_APPROVAL)
                recovered_request = await runtime_approvals.get_for_intent(intent.id)
                assert recovered_request is not None
                request = recovered_request
            await approval_service.approve(
                request.id,
                DecideApproval(decided_by="qa-operator"),
            )
            approval_ids.append(request.id)
            intent = await dispatcher.approve_intent(intent.id)

        try:
            execution = await dispatcher.execute_intent(intent)
        except InjectedRecoveryFault as fault:
            assert index == 0
            assert fault.boundary is RecoveryBoundary.AFTER_EXECUTION_STARTED
            # A fresh Worker-side service sees the durable execution key and does not launch again.
            execution_service = _execution_service(
                runs=runs,
                executions=executions,
                sessions=sessions,
                intents=intents,
                events=events,
                runner=faulting_runner,
            )
            dispatcher = DeferredExecutionDispatcher(
                tool_call_repository=intents,
                execution_service=execution_service,
            )
            execution = await dispatcher.execute_intent(intent)
        try:
            waited = await execution_service.wait(execution.id, timeout_seconds=1)
        except InjectedRecoveryFault as fault:
            assert index == 0
            assert fault.boundary is RecoveryBoundary.AFTER_EXECUTION_COMPLETED
            execution_service = _execution_service(
                runs=runs,
                executions=executions,
                sessions=sessions,
                intents=intents,
                events=events,
                runner=faulting_runner,
            )
            dispatcher = DeferredExecutionDispatcher(
                tool_call_repository=intents,
                execution_service=execution_service,
            )
            waited = await execution_service.wait(execution.id, timeout_seconds=1)
        execution = waited.execution
        execution_by_tool_call[intent.id] = execution.id
        processed_tool_call_ids.append(intent.id)
        if execution.status is ExecutionStatus.FAILED:
            failed_tool_call_ids.append(intent.id)
        tool_messages.append(
            TranscriptMessageDraft(
                agent_id="primary",
                role=MessageRole.TOOL,
                message_type=MessageType.TOOL_RESULT_REFERENCE,
                content=f"{execution.status.value}: {intent.tool_id}",
                execution_id=execution.id,
                visibility=MessageVisibility.AGENT_ONLY,
            )
        )

        if index == 49:
            # Runner restart: reconstruct the Runner adapter over durable Execution rows.
            durable_runner = DurableEvaluationRunner(executions, launch_counts)
            execution_service = _execution_service(
                runs=runs,
                executions=executions,
                sessions=sessions,
                intents=intents,
                events=events,
                runner=durable_runner,
            )
            dispatcher = DeferredExecutionDispatcher(
                tool_call_repository=intents,
                execution_service=execution_service,
            )
            await events.append(
                run.id,
                "execution.reconciled",
                {
                    "execution_id": execution.id,
                    "status": execution.status.value,
                    "outcome": "runner_adapter_reconstructed",
                },
            )

    await transcript.append_many("qa-primary", tool_messages)
    user_messages = await transcript.append_many(
        "qa-primary",
        [
            TranscriptMessageDraft(
                agent_id="primary",
                role=MessageRole.USER,
                message_type=MessageType.USER_MESSAGE,
                content=f"Operator supplement {index}: remain within example.com",
                visibility=MessageVisibility.USER_VISIBLE,
            )
            for index in range(5)
        ],
    )
    assert len(failed_tool_call_ids) == 10
    assert len(set(execution_by_tool_call.values())) == 100
    assert set(launch_counts.values()) == {1}

    memory = WorkingMemory(
        id="qa-working-memory",
        run_id=run.id,
        current_focus=CurrentFocus(
            phase="long_horizon_evaluation",
            objective=run.objective.description,
        ),
        attempts=[
            AttemptRecord(
                id=f"qa-attempt-{index:03d}",
                action_signature=f"qa-tool-{index:03d}:example.com",
                target="example.com",
                tool_id=f"qa-tool-{index:03d}",
                normalized_arguments={"index": index},
                result_status=(AttemptStatus.FAILED if index < 10 else AttemptStatus.SUCCEEDED),
                result_summary=("expected injected tool failure" if index < 10 else "completed"),
            )
            for index in range(100)
        ],
        user_decisions=[
            UserDecision(
                id=f"qa-decision-{index}",
                question=f"Supplement {index}",
                decision="Continue within example.com",
                source_ref=f"message://{user_messages[index].id}",
            )
            for index in range(5)
        ],
    )
    await working_memory_repository.create(memory)
    working_memory_digest_before = digest_json(memory.model_dump(mode="json"))

    artifact_service = ArtifactApplicationService(
        run_repository=runs,
        execution_repository=executions,
        artifact_repository=artifacts_repository,
        event_repository=events,
        paths=runner_paths,
    )
    artifact_ids: list[str] = []
    for index, execution_id in enumerate(list(execution_by_tool_call.values())[:10]):
        output = workspace / f"qa-output-{index:02d}.txt"
        output.write_text(f"traceable result {index}\n")
        artifact = await artifact_service.register(
            run.id,
            RegisterArtifact(
                source_path=str(output),
                name=f"qa-output-{index:02d}.txt",
                mime_type="text/plain",
                execution_id=execution_id,
                description="QA-01 immutable execution evidence",
            ),
        )
        artifact_ids.append(artifact.id)

    web_sources = SQLAlchemyWebSourceRepository(database.session_factory)
    source_ids: list[str] = []
    source_references: list[SourceReference] = []
    for index in range(20):
        body = f"Official source {index} for example.com".encode()
        raw = await artifact_service.register_content(
            run.id,
            RegisterArtifactContent(
                content=body,
                name=f"qa-web-source-{index:02d}.txt",
                mime_type="text/plain",
                description="QA-01 public source snapshot",
            ),
        )
        artifact_ids.append(raw.id)
        content_hash = hashlib.sha256(body).hexdigest()
        document = WebDocument(
            id=f"qa-document-{index:02d}",
            run_id=run.id,
            requested_url=f"https://docs.example.org/source-{index}",
            final_url=f"https://docs.example.org/source-{index}",
            title=f"Source {index}",
            mime_type="text/plain",
            raw_artifact_id=raw.id,
            content_hash=content_hash,
            text_length=len(body),
            extraction_status=ExtractionStatus.COMPLETE,
        )
        chunk = WebDocumentChunk(
            id=f"qa-chunk-{index:02d}",
            document_id=document.id,
            sequence=0,
            content=body.decode(),
            token_count=8,
            start_offset=0,
            end_offset=len(body),
        )
        source = SourceReference(
            id=f"qa-source-{index:02d}",
            document_id=document.id,
            url=document.final_url,
            title=document.title,
            domain="docs.example.org",
            fetched_at=document.fetched_at,
            source_type=SourceType.VENDOR_OFFICIAL,
            content_hash=content_hash,
        )
        await web_sources.save(
            document,
            [chunk],
            source,
            cache_expires_at=utc_now() + timedelta(days=1),
        )
        source_ids.append(source.id)
        source_references.append(source)

    await SQLAlchemyWebResearchRepository(database.session_factory).record_packet(
        WebResearchPacket(
            id="qa-research-packet",
            run_id=run.id,
            session_id="qa-primary",
            question="What durable evidence was collected?",
            summary="Twenty canonical public Sources were preserved.",
            key_claims=[
                ResearchClaim(
                    id="qa-research-claim",
                    statement="The first canonical Source was preserved.",
                    evidence=[
                        EvidenceSpan(
                            source_id=source_references[0].id,
                            quote="Official source 0 for example.com",
                        )
                    ],
                    confidence=1.0,
                )
            ],
            sources=source_references,
            document_ids=[f"qa-document-{index:02d}" for index in range(20)],
            artifact_ids=artifact_ids[10:],
        )
    )

    # Worker restart: close every DB-bound service and reconstruct repositories.
    await database.dispose()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    transcript = SQLAlchemyTranscriptRepository(database.session_factory)
    compilations = SQLAlchemyContextCompilationRepository(database.session_factory)
    checkpoints = SQLAlchemyContextCheckpointRepository(database.session_factory)
    intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    runtime_approvals = SQLAlchemyRuntimeApprovalRepository(database.session_factory)
    working_memory_repository = SQLAlchemyWorkingMemoryRepository(database.session_factory)
    recovered_memory = await working_memory_repository.get_for_run(run.id)
    assert recovered_memory is not None
    working_memory_digest_after = digest_json(recovered_memory.model_dump(mode="json"))
    assert working_memory_digest_after == working_memory_digest_before
    assert len(await executions.list(run.id)) == 100

    # Three isolated Subagents; the first crashes after its durable child Session exists.
    tools_path = tmp_path / "qa-tools.yaml"
    tools_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "qa-probe": {
                        "command": [sys.executable],
                        "capabilities": ["recon"],
                    }
                },
            }
        )
    )
    registry = ToolRegistry(tools_path, node_id="local")
    await registry.refresh()
    session_manager = SessionManager(
        run_repository=runs,
        session_repository=sessions,
        transcript_repository=transcript,
        provider_state_repository=SQLAlchemyProviderStateRepository(database.session_factory),
    )
    subagents = SubagentManager(
        sessions=session_manager,
        session_repository=sessions,
        tool_context=ToolContextManager(registry),
        limits=SubagentConfig(max_parallel_per_run=4, max_total_per_run=10),
        events=events,
    )
    subagent_ids: list[str] = []
    for index in range(3):
        handle = await subagents.start(
            parent_session_id="qa-primary",
            delegation=_delegation(index, workspace),
            session_id=f"qa-subagent-{index}",
        )
        if index == 0:
            with pytest.raises(InjectedRecoveryFault):
                injector.trip(RecoveryBoundary.DURING_SUBAGENT)
            handle = await subagents.recover(handle.session.id)
        await subagents.complete(
            handle.session.id,
            SubagentResult(
                task_id=handle.delegation.task_id,
                status=SubagentStatus.COMPLETED,
                summary=f"Completed slice {index}",
                evidence_refs=[f"source://{source_ids[index]}"],
            ),
        )
        subagent_ids.append(handle.session.id)

    compiler = ContextCompiler(
        sources=[TranscriptContextSource(transcript)],
        context_service=ContextApplicationService(compilations),
    )
    terminals = SQLAlchemyTerminalRepository(database.session_factory)
    first_compaction_command = CompactContextCommand(
        run_id=run.id,
        session_id="qa-primary",
        checkpoint_id="qa-compaction-1",
        max_history_items=20,
    )
    faulting_compaction = ContextCompactionManager(
        runs=runs,
        sessions=sessions,
        transcript=transcript,
        working_memory=working_memory_repository,
        compilations=compilations,
        checkpoints=FaultingCheckpointRepository(checkpoints, injector),  # type: ignore[arg-type]
        approvals=runtime_approvals,
        executions=executions,
        terminals=terminals,
        context_compiler=compiler,
    )
    with pytest.raises(InjectedRecoveryFault) as compaction_fault:
        await faulting_compaction.compact(first_compaction_command)
    assert compaction_fault.value.boundary is RecoveryBoundary.DURING_COMPACTION
    compaction = ContextCompactionManager(
        runs=runs,
        sessions=sessions,
        transcript=transcript,
        working_memory=working_memory_repository,
        compilations=compilations,
        checkpoints=checkpoints,
        approvals=runtime_approvals,
        executions=executions,
        terminals=terminals,
        context_compiler=compiler,
    )
    first_compaction = await compaction.compact(first_compaction_command)
    second_compaction = await compaction.compact(
        CompactContextCommand(
            run_id=run.id,
            session_id="qa-primary",
            checkpoint_id="qa-compaction-2",
            max_history_items=12,
        )
    )
    switched = await compaction.switch_model(
        SwitchModelCommand(
            run_id=run.id,
            session_id="qa-primary",
            checkpoint_id="qa-model-switch",
            model_profile="model-b",
            max_history_items=12,
        )
    )
    assert switched.previous_model_profile == "model-a"
    assert switched.model_profile == "model-b"

    browser_repository = SQLAlchemyBrowserRepository(database.session_factory)
    artifact_service = ArtifactApplicationService(
        run_repository=runs,
        execution_repository=executions,
        artifact_repository=SQLAlchemyArtifactRepository(database.session_factory),
        event_repository=events,
        paths=runner_paths,
    )
    action_counts: dict[str, int] = {}
    browser_runner = RunnerBrowserManager(
        node_id="local",
        paths=runner_paths,
        engine=EvaluationBrowserEngine(action_counts),
    )
    browser_service = BrowserApplicationService(
        runs=runs,
        agent_sessions=sessions,
        repository=browser_repository,
        runner=FaultingBrowserRunner(browser_runner, injector),
        artifacts=artifact_service,
        events=events,
    )
    opened = await browser_service.open(
        OpenBrowser(
            run_id=run.id,
            agent_session_id="qa-primary",
            url="https://example.com/",
        )
    )
    assert opened.observation is not None
    action_command = ActBrowser(
        page_id="qa-page-1",
        observation_version=opened.observation.observation_version,
        action=BrowserActionType.CLICK,
        action_key="qa-browser-action",
        element_ref="e-1",
    )
    with pytest.raises(InjectedRecoveryFault) as browser_fault:
        await browser_service.act(opened.session.id, action_command)
    assert browser_fault.value.boundary is RecoveryBoundary.DURING_BROWSER_ACTION
    acted = await browser_service.act(opened.session.id, action_command)
    assert acted.action is not None
    assert acted.action.status is BrowserActionStatus.COMPLETED
    assert action_counts == {"qa-browser-action": 1}
    taken = await browser_service.takeover(opened.session.id)
    assert taken.session.owner is BrowserOwner.USER
    released = await browser_service.release(opened.session.id)
    assert released.session.owner is BrowserOwner.AGENT
    assert released.takeover_summary is not None

    traced_artifact_ids: list[str] = []
    for artifact_id in artifact_ids:
        artifact = await artifact_service.get(artifact_id)
        content = await artifact_service.read_content_slice(
            artifact_id,
            expected_run_id=run.id,
            max_bytes=max(1, artifact.size),
        )
        assert content.artifact.id == artifact.id
        assert content.eof is True
        traced_artifact_ids.append(artifact.id)

    await runs.update_status(run.id, RunStatus.RUNNING)
    await runs.update_status(run.id, RunStatus.COMPLETING)
    await runs.update_status(run.id, RunStatus.COMPLETED)
    final_run = await runs.get(run.id)
    assert final_run is not None
    final_intents = [await intents.get(tool_call_id) for tool_call_id in tool_call_ids]
    assert all(intent is not None for intent in final_intents)
    processed_tool_call_ids = [
        intent.id
        for intent in final_intents
        if intent is not None and intent.status in {ToolCallStatus.COMPLETED, ToolCallStatus.FAILED}
    ]
    temporal_inputs = [
        RunAgentCycleActivityInput(
            run_id=run.id,
            session_id="qa-primary",
            cycle_id=f"temporal-cycle-{index:03d}",
            completed_execution_id=execution_by_tool_call[tool_call_ids[index]],
        )
        for index in range(100)
    ]
    temporal_payloads = [
        json.dumps(asdict(item), separators=(",", ":")).encode() for item in temporal_inputs
    ]
    large_sentinel = "x" * (1024 * 1024)
    assert all(large_sentinel.encode() not in payload for payload in temporal_payloads)
    assert set(injector.tripped) == set(RecoveryBoundary)

    metric_queries: list[str] = []

    def record_metric_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        metric_queries.append(statement)

    sqlalchemy_event.listen(
        database.engine.sync_engine,
        "before_cursor_execute",
        record_metric_query,
    )
    try:
        runtime_metrics = await RuntimeObservabilityService(
            SQLAlchemyRuntimeObservabilityRepository(database.session_factory)
        ).snapshot(run.id)
    finally:
        sqlalchemy_event.remove(
            database.engine.sync_engine,
            "before_cursor_execute",
            record_metric_query,
        )
    assert len(metric_queries) <= 11
    assert set(runtime_metrics.metrics) == set(RuntimeMetricName)
    assert runtime_metrics.metrics[RuntimeMetricName.TASK_COMPLETION_RATE].value == 1.0
    assert runtime_metrics.metrics[RuntimeMetricName.REPEATED_TOOL_CALL_RATE].value == 0.0
    assert runtime_metrics.metrics[RuntimeMetricName.INVALID_TOOL_CALL_RATE].value == 0.0
    assert runtime_metrics.metrics[RuntimeMetricName.RECOVERY_SUCCESS_RATE].value == 1.0
    assert runtime_metrics.metrics[RuntimeMetricName.EXECUTION_DUPLICATION_RATE].value == 0.0
    assert runtime_metrics.metrics[RuntimeMetricName.COMPACTION_FIDELITY].value == 1.0
    assert runtime_metrics.metrics[RuntimeMetricName.CONTEXT_TOKEN_EFFICIENCY].available
    assert runtime_metrics.metrics[RuntimeMetricName.SUBAGENT_UTILITY].value == 1.0
    assert runtime_metrics.metrics[RuntimeMetricName.APPROVAL_RESUME_SUCCESS_RATE].value == 1.0
    assert runtime_metrics.metrics[RuntimeMetricName.BROWSER_ACTION_FAILURE_RATE].value == 0.0
    assert runtime_metrics.metrics[RuntimeMetricName.CITATION_COVERAGE].value == 1.0

    report = LongHorizonEvaluator().evaluate(
        LongHorizonEvidence(
            tool_call_ids=tool_call_ids,
            failed_tool_call_ids=failed_tool_call_ids,
            processed_tool_call_ids=processed_tool_call_ids,
            execution_by_tool_call=execution_by_tool_call,
            execution_launch_counts=launch_counts,
            user_message_ids=[message.id for message in user_messages],
            approval_ids=approval_ids,
            subagent_session_ids=subagent_ids,
            compaction_checkpoint_ids=[
                first_compaction.checkpoint.id,
                second_compaction.checkpoint.id,
            ],
            model_switch_checkpoint_ids=[switched.checkpoint.id],
            worker_restart_count=1,
            runner_restart_count=1,
            web_source_ids=source_ids,
            browser_takeover_ids=[released.takeover_summary.id],
            recovery_boundaries=list(injector.tripped),
            objective_before=objective_before,
            objective_after=final_run.objective.description,
            scope_digest_before=scope_digest_before,
            scope_digest_after=digest_json(final_run.scope.model_dump(mode="json")),
            working_memory_digest_before_restart=working_memory_digest_before,
            working_memory_digest_after_restart=working_memory_digest_after,
            artifact_ids=artifact_ids,
            traced_artifact_ids=traced_artifact_ids,
            temporal_payload_sizes=[len(payload) for payload in temporal_payloads],
            temporal_contains_large_content=False,
        )
    )

    assert report.passed, report.model_dump(mode="json")
    assert report.observed == {
        "tool_calls": 100,
        "tool_failures": 10,
        "user_supplements": 5,
        "approvals": 3,
        "subagents": 3,
        "compactions": 2,
        "model_switches": 1,
        "worker_restarts": 1,
        "runner_restarts": 1,
        "web_sources": 20,
        "browser_takeovers": 1,
        "recovery_boundaries": 9,
        "artifacts": 30,
        "max_temporal_payload_bytes": max(len(payload) for payload in temporal_payloads),
    }
    async with database.session_factory() as session:
        approval_signals = list(
            await session.scalars(
                select(WorkflowSignalIntentRecord)
                .where(
                    WorkflowSignalIntentRecord.source_event_kind
                    == "approval_decision"
                )
                .order_by(WorkflowSignalIntentRecord.created_at)
            )
        )
    assert len(approval_signals) == len(approval_ids)
    assert {json.loads(record.payload_json)["approval_id"] for record in approval_signals} == set(
        approval_ids
    )
    assert all(record.signal_kind == "approve" for record in approval_signals)
    assert all(record.workflow_id == "riftx-run-qa-run" for record in approval_signals)
    assert all(record.delivery_state == "pending" for record in approval_signals)
    await database.dispose()
