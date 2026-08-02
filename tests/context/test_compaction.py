from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import update

from riftx.context import (
    CheckpointType,
    CompactionStage,
    ContextApplicationService,
    ContextCompiler,
    TranscriptContextSource,
)
from riftx.context.compaction import (
    CompactContextCommand,
    ContextCompactionManager,
    SwitchModelCommand,
)
from riftx.domain import (
    Engagement,
    MessageRole,
    MessageType,
    MessageVisibility,
    Objective,
    Run,
    TranscriptMessageDraft,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTranscriptRepository,
)
from riftx.persistence.checkpoint_repositories import (
    SQLAlchemyContextCheckpointRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.orm import AgentMessageRecord, AgentSessionRecord
from riftx.persistence.working_memory_repositories import SQLAlchemyWorkingMemoryRepository
from riftx.runtime.types import AgentSession, ProviderState


class _PendingApprovals:
    async def pending_for_run(self, run_id: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="approval-1", run_id=run_id)]


class _ActiveExecutions:
    async def list_active(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="execution-1", run_id="run-1")]


class _OpenTerminals:
    async def list_open(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="terminal-1", run_id="run-1")]


async def test_compaction_preserves_resume_state_and_repairs_crash_retry(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'compaction.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    transcript = SQLAlchemyTranscriptRepository(database.session_factory)
    checkpoints = SQLAlchemyContextCheckpointRepository(database.session_factory)
    compilations = SQLAlchemyContextCompilationRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Compaction")
    )
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Resume after context compaction"),
            workspace_path=str(tmp_path),
        )
    )
    await sessions.create(
        AgentSession(
            id="session-1",
            run_id="run-1",
            model_profile="model-a",
            provider_state_id="provider-state-1",
        )
    )
    await SQLAlchemyProviderStateRepository(database.session_factory).create(
        ProviderState(
            id="provider-state-1",
            session_id="session-1",
            provider="test",
            model="model-a",
            engine_type="fake",
            engine_version="1",
        )
    )
    messages = await transcript.append_many(
        "session-1",
        [
            TranscriptMessageDraft(
                agent_id="primary",
                role=MessageRole.USER,
                message_type=MessageType.USER_MESSAGE,
                content=f"message-{index}",
                visibility=MessageVisibility.USER_VISIBLE,
            )
            for index in range(3)
        ]
        + [
            TranscriptMessageDraft(
                agent_id="primary",
                role=MessageRole.TOOL,
                message_type=MessageType.TOOL_RESULT_REFERENCE,
                content="tool-result",
            )
        ],
    )
    manager = ContextCompactionManager(
        runs=runs,
        sessions=sessions,
        transcript=transcript,
        working_memory=SQLAlchemyWorkingMemoryRepository(database.session_factory),
        compilations=compilations,
        checkpoints=checkpoints,
        approvals=_PendingApprovals(),  # type: ignore[arg-type]
        executions=_ActiveExecutions(),  # type: ignore[arg-type]
        terminals=_OpenTerminals(),  # type: ignore[arg-type]
        context_compiler=ContextCompiler(
            sources=[TranscriptContextSource(transcript)],
            context_service=ContextApplicationService(compilations),
        ),
    )
    command = CompactContextCommand(
        run_id="run-1",
        session_id="session-1",
        checkpoint_id="checkpoint-1",
        max_history_items=2,
    )

    first = await manager.compact(command)

    assert first.compacted_messages == 2
    assert first.retained_messages == 2
    assert first.checkpoint.checkpoint_type is CheckpointType.CANONICAL
    assert first.checkpoint.compaction_stage is CompactionStage.CANONICAL_CHECKPOINT
    assert first.checkpoint.pending_approval_ids == ["approval-1"]
    assert first.checkpoint.active_execution_ids == ["execution-1"]
    assert first.checkpoint.active_terminal_ids == ["terminal-1"]
    assert first.checkpoint.provider_state_id == "provider-state-1"
    assert first.checkpoint.retained_message_ids == [messages[2].id, messages[3].id]
    assert first.checkpoint.retained_tool_result_ids == [messages[3].id]
    persisted = await transcript.list_by_session("session-1")
    assert len(persisted) == 4
    assert [item.compacted_by_checkpoint_id for item in persisted] == [
        "checkpoint-1",
        "checkpoint-1",
        None,
        None,
    ]

    # Recreate the precise crash window after checkpoint insertion but before the
    # transcript marker and session pointer commits, then retry the same Activity.
    async with database.session_factory() as sql_session, sql_session.begin():
        await sql_session.execute(
            update(AgentMessageRecord)
            .where(AgentMessageRecord.session_id == "session-1")
            .values(compacted_by_checkpoint_id=None)
        )
        await sql_session.execute(
            update(AgentSessionRecord)
            .where(AgentSessionRecord.id == "session-1")
            .values(latest_checkpoint_id=None)
        )

    retried = await manager.compact(command)

    assert retried.checkpoint == first.checkpoint
    assert retried.compacted_messages == 2
    assert len(await transcript.list_by_session("session-1")) == 4
    repaired = await transcript.list_by_session("session-1")
    assert [item.compacted_by_checkpoint_id for item in repaired] == [
        "checkpoint-1",
        "checkpoint-1",
        None,
        None,
    ]
    recovered_session = await sessions.get("session-1")
    assert recovered_session is not None
    assert recovered_session.latest_checkpoint_id == "checkpoint-1"

    switched = await manager.switch_model(
        SwitchModelCommand(
            run_id="run-1",
            session_id="session-1",
            checkpoint_id="checkpoint-model-switch",
            model_profile="model-b",
            max_history_items=2,
        )
    )
    assert switched.previous_model_profile == "model-a"
    assert switched.model_profile == "model-b"
    assert switched.checkpoint.checkpoint_type is CheckpointType.MODEL_SWITCH
    assert switched.checkpoint.provider_state_id == "provider-state-1"
    assert switched.compiled_context.compilation_id is not None
    compiled_refs = {
        str(ref)
        for item in switched.compiled_context.input_items
        for ref in item.get("source_refs", [])
    }
    assert f"message://{messages[0].id}" not in compiled_refs
    assert f"message://{messages[1].id}" not in compiled_refs
    assert f"message://{messages[2].id}" in compiled_refs
    assert f"message://{messages[3].id}" in compiled_refs
    switched_session = await sessions.get("session-1")
    assert switched_session is not None
    assert switched_session.model_profile == "model-b"
    assert switched_session.provider_state_id is None
    switched_run = await runs.get("run-1")
    assert switched_run is not None and switched_run.model_profile == "model-b"
    compilation = await compilations.latest_for_session("session-1")
    assert compilation is not None and compilation.model_profile == "model-b"

    # Provider-native resume state may expire independently. A model switch must
    # still preserve the stale ID in the neutral snapshot and recover by clearing it.
    async with database.session_factory() as sql_session, sql_session.begin():
        await sql_session.execute(
            update(AgentSessionRecord)
            .where(AgentSessionRecord.id == "session-1")
            .values(provider_state_id="expired-provider-state")
        )
    recovered = await manager.switch_model(
        SwitchModelCommand(
            run_id="run-1",
            session_id="session-1",
            checkpoint_id="checkpoint-invalid-provider",
            model_profile="model-c",
            max_history_items=2,
        )
    )
    assert recovered.checkpoint.provider_state_id == "expired-provider-state"
    final_session = await sessions.get("session-1")
    assert final_session is not None
    assert final_session.model_profile == "model-c"
    assert final_session.provider_state_id is None
    await database.dispose()
