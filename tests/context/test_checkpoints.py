from pathlib import Path

from riftx.context import (
    CheckpointType,
    CompactionStage,
    ContextCheckpoint,
    compaction_stage_for_usage,
)
from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.checkpoint_repositories import SQLAlchemyContextCheckpointRepository
from riftx.runtime.types import AgentSession, ProviderState


def checkpoint() -> ContextCheckpoint:
    return ContextCheckpoint(
        id="checkpoint-1",
        run_id="run-1",
        session_id="session-1",
        checkpoint_type=CheckpointType.CANONICAL,
        compaction_stage=CompactionStage.CANONICAL_CHECKPOINT,
        model_profile="model-a",
        objective="Map the authorized staging target",
        success_criteria=[{"description": "Inventory services"}],
        scope={"domains": ["staging.example"]},
        current_phase="enumeration",
        plan={"items": [{"task": "Scan ports", "status": "completed"}]},
        completed_work=[{"summary": "Port scan completed"}],
        confirmed_facts=[{"id": "fact-1", "value": "443/tcp open"}],
        hypotheses=[{"id": "hypothesis-1", "statement": "HTTPS is in scope"}],
        failed_attempts=[{"id": "attempt-1", "summary": "UDP scan timed out"}],
        user_decisions=[{"decision": "Do not test production"}],
        pending_approval_ids=["approval-1"],
        active_execution_ids=["execution-1"],
        active_terminal_ids=["terminal-1"],
        unresolved_questions=[{"question": "Which staging credential?"}],
        next_action={"description": "Inspect HTTPS"},
        working_memory_version=3,
        provider_state_id="provider-1",
        retained_message_ids=["message-9"],
        retained_tool_result_ids=["message-tool-1"],
    )


def test_compaction_thresholds_match_runtime_contract() -> None:
    assert compaction_stage_for_usage(0.54) is None
    assert compaction_stage_for_usage(0.55) is CompactionStage.TOOL_PREVIEW_CLEANUP
    assert compaction_stage_for_usage(0.70) is CompactionStage.CONVERSATION_SUMMARY
    assert compaction_stage_for_usage(0.82) is CompactionStage.CANONICAL_CHECKPOINT
    assert compaction_stage_for_usage(0.90) is CompactionStage.EMERGENCY_COMPACTION


async def test_context_checkpoint_round_trips_complete_resume_state(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'checkpoint.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Checkpoint")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Map staging"),
            workspace_path=str(tmp_path),
        )
    )
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="session-1", run_id="run-1", model_profile="model-a")
    )
    await SQLAlchemyProviderStateRepository(database.session_factory).create(
        ProviderState(
            id="provider-1",
            session_id="session-1",
            provider="test",
            model="model-a",
            engine_type="fake",
            engine_version="1",
        )
    )
    repository = SQLAlchemyContextCheckpointRepository(database.session_factory)
    expected = checkpoint()

    assert await repository.create(expected) == expected
    assert await repository.create(expected) == expected
    assert await repository.get(expected.id) == expected
    assert await repository.latest_for_session("session-1") == expected
    assert await repository.list_for_run("run-1") == [expected]
    await database.dispose()
