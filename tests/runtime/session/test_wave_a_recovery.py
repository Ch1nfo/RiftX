from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from riftx.domain import Engagement, MessageType, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunLeaseRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTranscriptRepository,
)
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import AgentEngineEvent, AgentEngineEventType, AgentEngineState
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import CycleLimits, MinimalContextCompiler, RunCycleRequest
from riftx.runtime.session import SessionManager
from riftx.runtime.types import YieldReason


class ThreeTurnEngineRun:
    async def events(self) -> AsyncIterator[AgentEngineEvent]:
        event_types = [
            (AgentEngineEventType.RUN_STARTED, {}),
            (AgentEngineEventType.ASSISTANT_MESSAGE, {"content": "turn one"}),
            (AgentEngineEventType.RUN_STARTED, {}),
            (AgentEngineEventType.ASSISTANT_MESSAGE, {"content": "turn two"}),
            (AgentEngineEventType.RUN_STARTED, {}),
            (AgentEngineEventType.ASSISTANT_MESSAGE, {"content": "turn three"}),
            (AgentEngineEventType.FINAL_OUTPUT, {"output": "turn three"}),
            (AgentEngineEventType.RUN_COMPLETED, {}),
        ]
        for sequence, (event_type, data) in enumerate(event_types, start=1):
            yield AgentEngineEvent(sequence=sequence, event_type=event_type, data=data)

    async def suspend(self) -> AgentEngineState:
        return AgentEngineState(
            engine_type="fake",
            engine_version="1",
            provider="fake",
            model="fake-model",
            serialized_state={"cursor": 8},
        )

    async def cancel(self) -> None:
        return None


class ThreeTurnEngine:
    async def start(self, request: object) -> ThreeTurnEngineRun:
        return ThreeTurnEngineRun()

    async def resume(self, request: object) -> ThreeTurnEngineRun:
        return ThreeTurnEngineRun()


async def test_wave_a_three_turn_transcript_survives_process_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "wave-a.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    providers = SQLAlchemyProviderStateRepository(database.session_factory)
    transcript = SQLAlchemyTranscriptRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Wave A")
    )
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Demonstrate durable recovery"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    manager = SessionManager(
        run_repository=runs,
        session_repository=sessions,
        transcript_repository=transcript,
        provider_state_repository=providers,
    )
    await manager.create_session(
        run_id="run-1", model_profile="fake-model", session_id="session-1"
    )
    coordinator = RuntimeCoordinator(
        run_repository=runs,
        session_repository=sessions,
        cycle_repository=SQLAlchemyAgentCycleRepository(database.session_factory),
        step_repository=SQLAlchemyAgentStepRepository(database.session_factory),
        provider_state_repository=providers,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        lease_manager=DatabaseRunLeaseManager(
            SQLAlchemyRunLeaseRepository(database.session_factory)
        ),
        context_compiler=MinimalContextCompiler(),
        agent_engine=ThreeTurnEngine(),
        transcript_repository=transcript,
        limits=CycleLimits(max_model_calls=4),
    )

    result = await coordinator.run_cycle(
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-1",
            input_text="begin the durable task",
        )
    )
    assert result.yield_reason is YieldReason.RUN_COMPLETED
    assert result.model_call_count == 3
    await database.dispose()

    # New Database and service objects simulate loading from another process.
    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    recovered = await SessionManager(
        run_repository=SQLAlchemyRunRepository(reopened.session_factory),
        session_repository=SQLAlchemyAgentSessionRepository(reopened.session_factory),
        transcript_repository=SQLAlchemyTranscriptRepository(reopened.session_factory),
        provider_state_repository=SQLAlchemyProviderStateRepository(reopened.session_factory),
    ).load_session("session-1")

    assert recovered.session.turn_count == 3
    assert recovered.session.model_call_count == 3
    assert [message.sequence for message in recovered.transcript] == [1, 2, 3, 4, 5]
    assert [message.message_type for message in recovered.transcript] == [
        MessageType.USER_MESSAGE,
        MessageType.ASSISTANT_MESSAGE,
        MessageType.ASSISTANT_MESSAGE,
        MessageType.ASSISTANT_MESSAGE,
        MessageType.CHECKPOINT_BOUNDARY,
    ]
    assert [message.content for message in recovered.transcript[1:4]] == [
        "turn one",
        "turn two",
        "turn three",
    ]
    assert recovered.transcript[-1].structured_content["yield_reason"] == "run_completed"
    await reopened.dispose()
