from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from riftx.persistence import (
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunLeaseRepository,
    SQLAlchemyRunRepository,
)
from riftx.runtime.coordinator import RuntimeCoordinator
from riftx.runtime.engine import AgentEngineEvent, AgentEngineEventType
from riftx.runtime.leases import DatabaseRunLeaseManager
from riftx.runtime.lifecycle import MinimalContextCompiler
from riftx.subagents import DurableSubagentTaskRunner, SubagentStatus

from .test_manager import build_manager, delegation


class _CompletedEngineRun:
    async def events(self) -> AsyncIterator[AgentEngineEvent]:
        result = {
            "status": "completed",
            "summary": "The delegated endpoint was inspected.",
            "evidence_refs": ["execution://probe-1"],
        }
        yield AgentEngineEvent(
            sequence=1,
            event_type=AgentEngineEventType.FINAL_OUTPUT,
            data={"content": json.dumps(result)},
        )
        yield AgentEngineEvent(
            sequence=2,
            event_type=AgentEngineEventType.RUN_COMPLETED,
            data={},
        )


class _CompletedEngine:
    async def start(self, _request: object) -> _CompletedEngineRun:
        return _CompletedEngineRun()

    async def resume(self, _request: object) -> _CompletedEngineRun:
        return _CompletedEngineRun()


async def test_durable_runner_uses_child_cycle_without_completing_parent_run(
    tmp_path: Path,
) -> None:
    database, sessions, manager, transcript = await build_manager(tmp_path)
    await sessions.create_session(
        run_id="run-1", model_profile="test-model", session_id="primary"
    )
    try:
        handle = await manager.start(
            parent_session_id="primary",
            delegation=delegation("task-1").model_copy(
                update={"workspace": str(tmp_path / "workspace")}
            ),
            session_id="subagent-1",
        )
        run_repository = SQLAlchemyRunRepository(database.session_factory)
        session_repository = SQLAlchemyAgentSessionRepository(database.session_factory)
        lease_repository = SQLAlchemyRunLeaseRepository(database.session_factory)
        coordinator = RuntimeCoordinator(
            run_repository=run_repository,
            session_repository=session_repository,
            cycle_repository=SQLAlchemyAgentCycleRepository(database.session_factory),
            step_repository=SQLAlchemyAgentStepRepository(database.session_factory),
            provider_state_repository=SQLAlchemyProviderStateRepository(
                database.session_factory
            ),
            event_repository=SQLAlchemyRunEventRepository(database.session_factory),
            lease_manager=DatabaseRunLeaseManager(lease_repository),
            context_compiler=MinimalContextCompiler(),
            agent_engine=_CompletedEngine(),
            transcript_repository=transcript,
        )

        result = await asyncio.wait_for(
            DurableSubagentTaskRunner(
                coordinator=coordinator,
                sessions=sessions,
            ).run(handle),
            timeout=5,
        )

        run = await run_repository.get("run-1")
        assert result.status is SubagentStatus.COMPLETED
        assert result.task_id == "task-1"
        assert result.evidence_refs == ["execution://probe-1"]
        assert run is not None and run.status.value == "running"
        assert await lease_repository.get("run-1") is None
    finally:
        await database.dispose()
