from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.domain import ApprovalLevel, Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunLeaseRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyRuntimeApprovalRepository,
    SQLAlchemyToolCallIntentRepository,
    SQLAlchemyUserInputRequestRepository,
)
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ApprovalDecision,
    CycleStatus,
    ProviderState,
    RunLease,
    RuntimeApprovalRequest,
    RuntimeStateMachine,
    SessionStatus,
    StepStatus,
    ToolCallIntent,
    ToolCallStatus,
    UserInputRequest,
    YieldReason,
)


async def create_run(database: Database) -> None:
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized engagement")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Map the authorized target"),
            workspace_path="/tmp/riftx/run-1",
        )
    )


async def test_runtime_repositories_restore_complete_state(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    await database.create_schema()
    await create_run(database)

    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    cycles = SQLAlchemyAgentCycleRepository(database.session_factory)
    steps = SQLAlchemyAgentStepRepository(database.session_factory)
    providers = SQLAlchemyProviderStateRepository(database.session_factory)
    intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    approvals = SQLAlchemyRuntimeApprovalRepository(database.session_factory)
    user_inputs = SQLAlchemyUserInputRequestRepository(database.session_factory)

    agent_session = AgentSession(id="session-1", run_id="run-1", model_profile="default")
    await sessions.create(agent_session)
    RuntimeStateMachine().transition_session(agent_session, SessionStatus.ACTIVE)
    agent_session.turn_count = 2
    await sessions.save(agent_session)

    cycle = AgentCycle(id="cycle-1", run_id="run-1", session_id="session-1", sequence=1)
    await cycles.create(cycle)
    RuntimeStateMachine().transition_cycle(cycle, CycleStatus.RUNNING)
    cycle.model_call_count = 2
    cycle.tool_call_count = 1
    RuntimeStateMachine().transition_cycle(
        cycle, CycleStatus.YIELDED, yield_reason=YieldReason.TOOL_RUNNING
    )
    cycle.waiting_object_id = "execution-1"
    cycle.checkpoint_id = "checkpoint-1"
    await cycles.save(cycle)

    step = AgentStep(
        id="step-1",
        cycle_id="cycle-1",
        sequence=1,
        step_type=AgentStepType.TOOL_PROPOSAL,
        input_refs=["message://1"],
    )
    await steps.create(step)
    RuntimeStateMachine().transition_step(step, StepStatus.RUNNING)
    step.output_refs = ["intent://intent-1"]
    RuntimeStateMachine().transition_step(step, StepStatus.COMPLETED)
    await steps.save(step)

    provider_state = ProviderState(
        id="provider-1",
        session_id="session-1",
        provider="openai",
        model="gpt-5.6",
        engine_type="openai-agents",
        engine_version="0.19",
        state={"conversation_id": "conversation-1", "opaque": {"cursor": 3}},
        previous_response_id="response-1",
    )
    await providers.create(provider_state)

    intent = ToolCallIntent(
        id="intent-1",
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        step_id="step-1",
        tool_id="nmap",
        arguments={"targets": ["192.0.2.1"]},
        command_preview="nmap 192.0.2.1",
        reason="Authorized discovery",
        approval_level=ApprovalLevel.SENSITIVE,
        execution_spec={"argv": ["nmap", "192.0.2.1"]},
    )
    await intents.create(intent)
    intent.status = ToolCallStatus.WAITING_APPROVAL
    await intents.save(intent)

    approval = RuntimeApprovalRequest(
        id="approval-1",
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        tool_call_intent_id="intent-1",
        working_memory_version=3,
        provider_state_id="provider-1",
    )
    await approvals.create(approval)
    assert await approvals.create(approval.model_copy(update={"id": "approval-race"})) == approval
    approval.decide(ApprovalDecision.APPROVE_ONCE, decided_by="operator")
    await approvals.save(approval)

    user_input = UserInputRequest(
        id="input-1",
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        prompt="Continue with the authorized target?",
        provider_state_id="provider-1",
    )
    await user_inputs.create(user_input)
    duplicate_input = user_input.model_copy(update={"id": "input-race"})
    assert await user_inputs.create(duplicate_input) == user_input

    assert await sessions.get("session-1") == agent_session
    assert list(await sessions.list_by_run("run-1")) == [agent_session]
    assert await cycles.get("cycle-1") == cycle
    assert list(await cycles.list_by_session("session-1")) == [cycle]
    assert await steps.get("step-1") == step
    assert list(await steps.list_by_cycle("cycle-1")) == [step]
    assert await providers.get("provider-1") == provider_state
    assert await providers.latest_for_session("session-1") == provider_state
    assert await intents.get("intent-1") == intent
    assert await approvals.get("approval-1") == approval
    assert await approvals.get_for_intent("intent-1") == approval
    assert await user_inputs.get("input-1") == user_input
    assert await user_inputs.get_for_cycle("cycle-1") == user_input
    assert await user_inputs.pending_for_session("run-1", "session-1") == user_input

    await database.dispose()


async def test_run_lease_uses_optimistic_versioning(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}")
    await database.create_schema()
    await create_run(database)
    leases = SQLAlchemyRunLeaseRepository(database.session_factory)
    now = datetime(2026, 7, 30, tzinfo=UTC)
    lease = RunLease(
        run_id="run-1",
        owner_id="worker-1",
        acquired_at=now,
        heartbeat_at=now,
        expires_at=now + timedelta(minutes=1),
    )

    await leases.acquire(lease)
    renewed = lease.model_copy(
        update={
            "heartbeat_at": now + timedelta(seconds=10),
            "expires_at": now + timedelta(minutes=2),
        }
    )
    renewed = await leases.save(renewed, expected_version=1)
    assert renewed.version == 2
    assert await leases.get("run-1") == renewed

    with pytest.raises(RepositoryConflictError, match="version conflict"):
        await leases.save(renewed, expected_version=1)

    with pytest.raises(RepositoryConflictError, match="release conflict"):
        await leases.release("run-1", owner_id="other-worker", expected_version=2)

    await leases.release("run-1", owner_id="worker-1", expected_version=2)
    assert await leases.get("run-1") is None
    await database.dispose()
