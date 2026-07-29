from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from riftx.agent import (
    AgentCycleOutput,
    AgentCycleResult,
    AgentCycleStatus,
    AgentInterruption,
    RiftXAgentContext,
    RiftXDatabaseSession,
)
from riftx.domain import Engagement, Objective, Run, RunStatus
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import ProcessSupervisor, RunnerPaths
from riftx.temporal import (
    AgentCycleActivityInput,
    AgentCycleActivityStatus,
    CleanupRunInput,
    CompactContextInput,
    GenerateReportInput,
    PrepareRunInput,
)
from riftx.temporal.activities import RiftXActivities
from riftx.tools import ToolRegistry

FIXTURE = Path(__file__).parents[2] / "tools" / "fixtures" / "fake_tool.py"


@dataclass
class FakeAgentCycle:
    results: deque[AgentCycleResult]
    calls: list[dict[str, object]] = field(default_factory=list)

    async def run(
        self,
        context: RiftXAgentContext,
        *,
        input_text: str | None = None,
        checkpoint_id: str | None = None,
        approval_decisions: dict[str, bool] | None = None,
    ) -> AgentCycleResult:
        self.calls.append(
            {
                "context": context,
                "input_text": input_text,
                "checkpoint_id": checkpoint_id,
                "approval_decisions": approval_decisions,
            }
        )
        return self.results.popleft()


async def _runtime(
    tmp_path: Path,
    cycle: FakeAgentCycle,
) -> tuple[
    Database,
    SQLAlchemyRunRepository,
    SQLAlchemyRunEventRepository,
    RiftXActivities,
    ProcessSupervisor,
]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Temporal activities")
    )
    run_repository = SQLAlchemyRunRepository(database.session_factory)
    await run_repository.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Temporal activity test"),
            workspace_path=str(tmp_path / "workspace" / "run-1"),
        )
    )
    config_path = tmp_path / "tools.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "registered_only",
                "tools": {
                    "custom": {
                        "command": [sys.executable, str(FIXTURE)],
                        "capabilities": ["verify"],
                    }
                },
            }
        )
    )
    registry = ToolRegistry(config_path, node_id="node-1")
    execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = ProcessSupervisor(
        execution_repository,
        RunnerPaths(tmp_path / "state"),
        termination_grace_seconds=0.1,
    )
    event_repository = SQLAlchemyRunEventRepository(database.session_factory)
    activities = RiftXActivities(
        run_repository=run_repository,
        event_repository=event_repository,
        execution_repository=execution_repository,
        tool_registry=registry,
        supervisor=supervisor,
        agent_cycle=cycle,
        session_factory=database.session_factory,
    )
    return database, run_repository, event_repository, activities, supervisor


async def test_temporal_activities_prepare_interrupt_resume_and_complete(tmp_path: Path) -> None:
    cycle = FakeAgentCycle(
        deque(
            [
                AgentCycleResult(
                    status=AgentCycleStatus.INTERRUPTED,
                    checkpoint_id="checkpoint-1",
                    interruptions=[AgentInterruption(call_id="call-1", tool_name="run_shell")],
                ),
                AgentCycleResult(
                    status=AgentCycleStatus.COMPLETED,
                    output=AgentCycleOutput(
                        assistant_message="Done",
                        plan_summary="Complete after approval",
                        run_summary="Verified",
                        completed=True,
                    ),
                ),
            ]
        )
    )
    database, run_repository, events, activities, supervisor = await _runtime(tmp_path, cycle)

    prepared = await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    first = await activities.agent_cycle_activity(
        AgentCycleActivityInput(run_id="run-1", agent_step_id="step-1")
    )
    waiting_run = await run_repository.get("run-1")
    second = await activities.agent_cycle_activity(
        AgentCycleActivityInput(
            run_id="run-1",
            agent_step_id="step-2",
            checkpoint_id="checkpoint-1",
            approval_decisions={"call-1": True},
            user_messages=["continue"],
        )
    )
    completed_run = await run_repository.get("run-1")

    assert prepared.prepared is True
    assert (tmp_path / "workspace" / "run-1").is_dir()
    assert first.status is AgentCycleActivityStatus.WAITING_APPROVAL
    assert first.pending_approvals[0].call_id == "call-1"
    assert waiting_run is not None and waiting_run.status is RunStatus.WAITING_APPROVAL
    assert second.status is AgentCycleActivityStatus.COMPLETED
    assert completed_run is not None and completed_run.status is RunStatus.COMPLETED
    assert cycle.calls[1]["checkpoint_id"] == "checkpoint-1"
    assert cycle.calls[1]["approval_decisions"] == {"call-1": True}
    assert cycle.calls[1]["input_text"] == "continue"
    event_types = [event.event_type for event in await events.list_after("run-1")]
    assert "run.prepared" in event_types

    await activities.generate_report_activity(GenerateReportInput(run_id="run-1"))
    await activities.cleanup_run_activity(CleanupRunInput(run_id="run-1", final_status="completed"))
    await supervisor.close()
    await database.dispose()


async def test_compact_context_activity_keeps_latest_messages(tmp_path: Path) -> None:
    cycle = FakeAgentCycle(deque())
    database, _, events, activities, supervisor = await _runtime(tmp_path, cycle)
    await activities.prepare_run_activity(PrepareRunInput(run_id="run-1"))
    session = RiftXDatabaseSession("run-1", database.session_factory)
    await session.add_items([{"role": "user", "content": f"message-{index}"} for index in range(5)])

    result = await activities.compact_context_activity(
        CompactContextInput(run_id="run-1", max_history_items=2)
    )

    assert result.compacted is True
    assert result.retained_items == 2
    assert await session.get_items() == [
        {"role": "user", "content": "message-3"},
        {"role": "user", "content": "message-4"},
    ]
    event_types = [event.event_type for event in await events.list_after("run-1")]
    assert "agent.context_compacted" in event_types
    await supervisor.close()
    await database.dispose()
