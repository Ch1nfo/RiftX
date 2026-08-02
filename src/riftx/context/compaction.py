"""Canonical checkpoint creation without deleting authoritative Runtime state."""

from __future__ import annotations

from dataclasses import dataclass

from riftx.application.errors import EntityNotFoundError
from riftx.domain import MessageType
from riftx.persistence.checkpoint_repositories import (
    SQLAlchemyContextCheckpointRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.repositories import (
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.persistence.runtime_repositories import (
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyRuntimeApprovalRepository,
)
from riftx.persistence.transcript_repositories import SQLAlchemyTranscriptRepository
from riftx.persistence.working_memory_repositories import SQLAlchemyWorkingMemoryRepository
from riftx.runtime.lifecycle import (
    CompiledContext,
    ContextCompiler,
    ContextCompileRequest,
    ContextPurpose,
)

from .checkpoints import CheckpointType, CompactionStage, ContextCheckpoint
from .working_memory import AttemptStatus, PlanItemStatus


@dataclass(frozen=True, slots=True)
class CompactContextCommand:
    run_id: str
    session_id: str
    checkpoint_id: str
    max_history_items: int = 100
    checkpoint_type: CheckpointType = CheckpointType.CANONICAL
    compaction_stage: CompactionStage = CompactionStage.CANONICAL_CHECKPOINT
    model_profile: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionResult:
    checkpoint: ContextCheckpoint
    compacted_messages: int
    retained_messages: int


@dataclass(frozen=True, slots=True)
class SwitchModelCommand:
    run_id: str
    session_id: str
    checkpoint_id: str
    model_profile: str
    max_history_items: int = 100


@dataclass(frozen=True, slots=True)
class ModelSwitchResult:
    checkpoint: ContextCheckpoint
    previous_model_profile: str
    model_profile: str
    compiled_context: CompiledContext


class ContextCompactionManager:
    def __init__(
        self,
        *,
        runs: SQLAlchemyRunRepository,
        sessions: SQLAlchemyAgentSessionRepository,
        transcript: SQLAlchemyTranscriptRepository,
        working_memory: SQLAlchemyWorkingMemoryRepository,
        compilations: SQLAlchemyContextCompilationRepository,
        checkpoints: SQLAlchemyContextCheckpointRepository,
        approvals: SQLAlchemyRuntimeApprovalRepository,
        executions: SQLAlchemyExecutionRepository,
        terminals: SQLAlchemyTerminalRepository,
        context_compiler: ContextCompiler | None = None,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._transcript = transcript
        self._working_memory = working_memory
        self._compilations = compilations
        self._checkpoints = checkpoints
        self._approvals = approvals
        self._executions = executions
        self._terminals = terminals
        self._context_compiler = context_compiler

    async def compact(self, command: CompactContextCommand) -> CompactionResult:
        if command.max_history_items < 1:
            raise ValueError("max_history_items must be positive")
        existing = await self._checkpoints.get(command.checkpoint_id)
        run = await self._runs.get(command.run_id)
        if run is None:
            raise EntityNotFoundError("Run", command.run_id)
        session = await self._sessions.get(command.session_id)
        if session is None or session.run_id != run.id:
            raise EntityNotFoundError("AgentSession", command.session_id)

        memory = await self._working_memory.get_for_run(run.id)
        compilation = await self._compilations.latest_for_session(session.id)
        messages = await self._transcript.list_by_session(session.id)
        if existing is None:
            retained_messages = messages[-command.max_history_items :]
            compacted_messages = messages[: -command.max_history_items]
        else:
            retained_ids = set(existing.retained_message_ids)
            retained_messages = [item for item in messages if item.id in retained_ids]
            first_retained_sequence = min(
                (item.sequence for item in retained_messages),
                default=None,
            )
            compacted_messages = (
                [item for item in messages if item.sequence < first_retained_sequence]
                if first_retained_sequence is not None
                else []
            )
        pending = await self._approvals.pending_for_run(run.id)
        active_executions = [
            item for item in await self._executions.list_active() if item.run_id == run.id
        ]
        active_terminals = [
            item for item in await self._terminals.list_open() if item.run_id == run.id
        ]
        plan = (
            memory.run_plan.model_dump(mode="json")
            if memory is not None
            else {"items": []}
        )
        checkpoint = existing
        if checkpoint is None:
            checkpoint = ContextCheckpoint(
                id=command.checkpoint_id,
                run_id=run.id,
                session_id=session.id,
                checkpoint_type=command.checkpoint_type,
                compaction_stage=command.compaction_stage,
                model_profile=command.model_profile or session.model_profile,
                objective=run.objective.description,
                success_criteria=[
                    item.model_dump(mode="json") for item in run.success_criteria
                ],
                scope=run.scope.model_dump(mode="json"),
                current_phase=(
                    memory.current_focus.phase
                    if memory is not None and memory.current_focus is not None
                    else run.status.value
                ),
                plan=plan,
                completed_work=(
                    [
                        item.model_dump(mode="json")
                        for item in memory.run_plan.items
                        if item.status is PlanItemStatus.COMPLETED
                    ]
                    if memory is not None
                    else []
                ),
                confirmed_facts=(
                    [item.model_dump(mode="json") for item in memory.confirmed_facts]
                    if memory is not None
                    else []
                ),
                hypotheses=(
                    [item.model_dump(mode="json") for item in memory.hypotheses]
                    if memory is not None
                    else []
                ),
                failed_attempts=(
                    [
                        item.model_dump(mode="json")
                        for item in memory.attempts
                        if item.result_status is AttemptStatus.FAILED
                    ]
                    if memory is not None
                    else []
                ),
                user_decisions=(
                    [item.model_dump(mode="json") for item in memory.user_decisions]
                    if memory is not None
                    else []
                ),
                pending_approval_ids=list(
                    dict.fromkeys(
                        [item.id for item in pending]
                        + (memory.pending_approvals if memory is not None else [])
                    )
                ),
                active_execution_ids=list(
                    dict.fromkeys(
                        [item.id for item in active_executions]
                        + (
                            [item.execution_id for item in memory.active_executions]
                            if memory is not None
                            else []
                        )
                    )
                ),
                active_terminal_ids=list(
                    dict.fromkeys(
                        [item.id for item in active_terminals]
                        + (
                            [item.terminal_session_id for item in memory.active_terminals]
                            if memory is not None
                            else []
                        )
                    )
                ),
                unresolved_questions=(
                    [item.model_dump(mode="json") for item in memory.pending_questions]
                    if memory is not None
                    else []
                ),
                next_action=(
                    memory.next_action.model_dump(mode="json")
                    if memory is not None and memory.next_action is not None
                    else None
                ),
                context_compilation_id=compilation.id if compilation is not None else None,
                context_manifest_id=compilation.id if compilation is not None else None,
                working_memory_version=memory.version if memory is not None else None,
                provider_state_id=session.provider_state_id,
                retained_message_ids=[item.id for item in retained_messages],
                retained_tool_result_ids=[
                    item.id
                    for item in retained_messages
                    if item.message_type is MessageType.TOOL_RESULT_REFERENCE
                ],
            )
            checkpoint = await self._checkpoints.create(checkpoint)
        marked = 0
        if compacted_messages:
            marked = await self._transcript.mark_compacted(
                session.id,
                through_sequence=compacted_messages[-1].sequence,
                checkpoint_id=checkpoint.id,
            )
        session.latest_checkpoint_id = checkpoint.id
        await self._sessions.save(session)
        return CompactionResult(checkpoint, marked, len(retained_messages))

    async def switch_model(self, command: SwitchModelCommand) -> ModelSwitchResult:
        target_profile = command.model_profile.strip()
        if not target_profile:
            raise ValueError("model_profile must not be empty")
        if self._context_compiler is None:
            raise RuntimeError("model switching requires a Context Compiler")
        run = await self._runs.get(command.run_id)
        if run is None:
            raise EntityNotFoundError("Run", command.run_id)
        session = await self._sessions.get(command.session_id)
        if session is None or session.run_id != run.id:
            raise EntityNotFoundError("AgentSession", command.session_id)

        compaction = await self.compact(
            CompactContextCommand(
                run_id=run.id,
                session_id=session.id,
                checkpoint_id=command.checkpoint_id,
                max_history_items=command.max_history_items,
                checkpoint_type=CheckpointType.MODEL_SWITCH,
                compaction_stage=CompactionStage.CANONICAL_CHECKPOINT,
                model_profile=session.model_profile,
            )
        )
        previous_profile = compaction.checkpoint.model_profile
        session = await self._sessions.get(session.id)
        if session is None:
            raise EntityNotFoundError("AgentSession", command.session_id)
        session.model_profile = target_profile
        session.provider_state_id = None
        session.latest_checkpoint_id = compaction.checkpoint.id
        await self._sessions.save(session)
        await self._runs.update_model_profile(run.id, target_profile)

        compiled = await self._context_compiler.compile(
            ContextCompileRequest(
                run_id=run.id,
                session_id=session.id,
                agent_id=session.agent_type,
                purpose=ContextPurpose.PRIMARY_REASONING,
                model_profile=target_profile,
                objective=run.objective.description,
                run_contract={
                    "objective": run.objective.description,
                    "success_criteria": [
                        item.model_dump(mode="json") for item in run.success_criteria
                    ],
                    "entry_points": [
                        item.model_dump(mode="json") for item in run.entry_points
                    ],
                    "scope": run.scope.model_dump(mode="json"),
                    "approval_mode": run.approval_mode.value,
                    "node_id": run.node_id,
                    "engagement_id": run.engagement_id,
                    "workspace": run.workspace_path,
                    "current_path": run.workspace_path,
                },
                workspace_path=run.workspace_path,
                current_path=run.workspace_path,
            )
        )
        return ModelSwitchResult(
            checkpoint=compaction.checkpoint,
            previous_model_profile=previous_profile,
            model_profile=target_profile,
            compiled_context=compiled,
        )
