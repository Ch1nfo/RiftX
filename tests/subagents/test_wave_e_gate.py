from __future__ import annotations

from pathlib import Path

from riftx.context import ContextApplicationService, ContextCompiler, TranscriptContextSource
from riftx.context.compaction import CompactContextCommand, ContextCompactionManager
from riftx.domain import MessageRole, MessageType, MessageVisibility, TranscriptMessageDraft
from riftx.persistence import (
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.persistence.checkpoint_repositories import (
    SQLAlchemyContextCheckpointRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.runtime_repositories import SQLAlchemyRuntimeApprovalRepository
from riftx.persistence.working_memory_repositories import (
    SQLAlchemyWorkingMemoryRepository,
)
from riftx.runtime.lifecycle import ContextCompileRequest
from riftx.subagents import SubagentOrchestrator

from .test_manager import build_manager, delegation
from .test_orchestrator import IndependentToolRunner


async def test_wave_e_three_subagents_compact_then_continue_primary_run(
    tmp_path: Path,
) -> None:
    database, session_manager, manager, transcript = await build_manager(tmp_path)
    await session_manager.create_session(
        run_id="run-1", model_profile="test-model", session_id="primary"
    )
    await SubagentOrchestrator(
        manager,
        IndependentToolRunner(session_manager),
    ).execute_many(
        parent_session_id="primary",
        delegations=[delegation(f"task-{index}") for index in range(1, 4)],
    )

    session_repository = SQLAlchemyAgentSessionRepository(database.session_factory)
    compilation_repository = SQLAlchemyContextCompilationRepository(
        database.session_factory
    )
    checkpoint_repository = SQLAlchemyContextCheckpointRepository(
        database.session_factory
    )
    compiler = ContextCompiler(
        sources=[TranscriptContextSource(transcript)],
        context_service=ContextApplicationService(compilation_repository),
    )
    compaction = await ContextCompactionManager(
        runs=SQLAlchemyRunRepository(database.session_factory),
        sessions=session_repository,
        transcript=transcript,
        working_memory=SQLAlchemyWorkingMemoryRepository(database.session_factory),
        compilations=compilation_repository,
        checkpoints=checkpoint_repository,
        approvals=SQLAlchemyRuntimeApprovalRepository(database.session_factory),
        executions=SQLAlchemyExecutionRepository(database.session_factory),
        terminals=SQLAlchemyTerminalRepository(database.session_factory),
        context_compiler=compiler,
    ).compact(
        CompactContextCommand(
            run_id="run-1",
            session_id="primary",
            checkpoint_id="wave-e-checkpoint",
            max_history_items=2,
        )
    )

    continued = await session_manager.append_message(
        "primary",
        TranscriptMessageDraft(
            agent_id="primary",
            role=MessageRole.USER,
            message_type=MessageType.USER_MESSAGE,
            content="Continue after compaction",
            visibility=MessageVisibility.USER_VISIBLE,
        ),
    )
    compiled = await compiler.compile(
        ContextCompileRequest(
            run_id="run-1",
            session_id="primary",
            agent_id="primary",
            model_profile="test-model",
            objective="Inspect the local service",
            latest_user_message_id=continued.id,
        )
    )

    subagent_items = [
        item for item in compiled.input_items if item.get("type") == "subagent_results"
    ]
    assert compaction.compacted_messages == 1
    assert compaction.retained_messages == 2
    assert len(subagent_items) == 2
    assert "probe completed" not in str(compiled.input_items)
    assert "Continue after compaction" in str(compiled.input_items)
    primary = await session_repository.get("primary")
    assert primary is not None and primary.latest_checkpoint_id == "wave-e-checkpoint"
    assert len(await manager.list_sessions("run-1")) == 3
    await database.dispose()
