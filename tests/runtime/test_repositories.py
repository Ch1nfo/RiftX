import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain import ApprovalLevel, ApprovalStatus, Engagement, Objective, Run
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
    intent, changed = await intents.compare_and_set_status(
        intent.id,
        expected={ToolCallStatus.PROPOSED},
        target=ToolCallStatus.WAITING_APPROVAL,
    )
    assert changed is True

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
    approval, changed = await approvals.decide_if_pending(
        approval.id,
        ApprovalDecision.APPROVE_ONCE,
        decided_by="operator",
        decided_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    assert changed is True

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


async def test_tool_intent_run_enumeration_and_status_cas_are_terminal_wins(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'intent-stop.db'}")
    await database.create_schema()
    await create_run(database)
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="session-1", run_id="run-1", model_profile="default")
    )
    await SQLAlchemyAgentCycleRepository(database.session_factory).create(
        AgentCycle(id="cycle-1", run_id="run-1", session_id="session-1", sequence=1)
    )
    await SQLAlchemyAgentStepRepository(database.session_factory).create(
        AgentStep(
            id="step-1",
            cycle_id="cycle-1",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        )
    )
    intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    try:
        for intent_id, tool_id, status in [
            ("ready-http", "request_target_url", ToolCallStatus.READY),
            ("executing-http", "request_target_url", ToolCallStatus.EXECUTING),
            ("completed-http", "request_target_url", ToolCallStatus.COMPLETED),
            ("ready-shell", "run_shell", ToolCallStatus.READY),
        ]:
            await intents.create(
                ToolCallIntent(
                    id=intent_id,
                    run_id="run-1",
                    session_id="session-1",
                    cycle_id="cycle-1",
                    step_id="step-1",
                    tool_id=tool_id,
                    status=status,
                )
            )

        active = await intents.active_for_run(
            "run-1",
            tool_ids={"request_target_url"},
        )
        assert {item.id for item in active} == {"ready-http", "executing-http"}

        cancelled, changed = await intents.compare_and_set_status(
            "ready-http",
            expected={ToolCallStatus.READY},
            target=ToolCallStatus.CANCELLED,
        )
        assert changed is True
        assert cancelled.status is ToolCallStatus.CANCELLED

        terminal, changed = await intents.compare_and_set_status(
            "ready-http",
            expected={ToolCallStatus.EXECUTING},
            target=ToolCallStatus.COMPLETED,
        )
        assert changed is False
        assert terminal.status is ToolCallStatus.CANCELLED
    finally:
        await database.dispose()


async def _runtime_approval_fixture(
    tmp_path: Path,
    database_name: str,
) -> tuple[
    Database,
    SQLAlchemyRuntimeApprovalRepository,
    SQLAlchemyProviderStateRepository,
    RuntimeApprovalRequest,
]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / database_name}")
    await database.create_schema()
    await create_run(database)
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="session-1", run_id="run-1", model_profile="default")
    )
    await SQLAlchemyAgentCycleRepository(database.session_factory).create(
        AgentCycle(id="cycle-1", run_id="run-1", session_id="session-1", sequence=1)
    )
    await SQLAlchemyAgentStepRepository(database.session_factory).create(
        AgentStep(
            id="step-1",
            cycle_id="cycle-1",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        )
    )
    await SQLAlchemyToolCallIntentRepository(database.session_factory).create(
        ToolCallIntent(
            id="intent-1",
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            step_id="step-1",
            tool_id="python",
        )
    )
    approval = RuntimeApprovalRequest(
        id="approval-1",
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        tool_call_intent_id="intent-1",
    )
    approvals = SQLAlchemyRuntimeApprovalRepository(database.session_factory)
    await approvals.create(approval)
    return (
        database,
        approvals,
        SQLAlchemyProviderStateRepository(database.session_factory),
        approval,
    )


def _provider_state(provider_id: str) -> ProviderState:
    return ProviderState(
        id=provider_id,
        session_id="session-1",
        provider="openai",
        model="gpt-5.6",
        engine_type="openai-agents",
        engine_version="0.19",
        state={"provider_id": provider_id},
    )


@pytest.mark.parametrize("decision_first", [False, True])
async def test_runtime_approval_partial_writers_preserve_each_other(
    tmp_path: Path,
    decision_first: bool,
) -> None:
    database, approvals, providers, approval = await _runtime_approval_fixture(
        tmp_path,
        f"approval-order-{decision_first}.db",
    )
    await providers.create(_provider_state("provider-1"))
    decided_at = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)

    async def decide() -> None:
        decided, changed = await approvals.decide_if_pending(
            approval.id,
            ApprovalDecision.REJECT_WITH_FEEDBACK,
            decided_by="operator",
            feedback="Not authorized",
            decided_at=decided_at,
        )
        assert changed is True
        assert decided.provider_state_id == (None if decision_first else "provider-1")

    async def attach_provider() -> None:
        attached = await approvals.set_provider_state_id(approval.id, "provider-1")
        assert attached.provider_state_id == "provider-1"

    if decision_first:
        await decide()
        await attach_provider()
    else:
        await attach_provider()
        await decide()

    persisted = await approvals.get(approval.id)
    assert persisted is not None
    assert persisted.provider_state_id == "provider-1"
    assert persisted.status is ApprovalStatus.REJECTED
    assert persisted.decision is ApprovalDecision.REJECT_WITH_FEEDBACK
    assert persisted.feedback == "Not authorized"
    assert persisted.decided_by == "operator"
    assert persisted.decided_at == decided_at
    assert await approvals.set_provider_state_id(approval.id, "provider-1") == persisted
    await database.dispose()


async def test_runtime_approval_concurrent_partial_writers_converge(
    tmp_path: Path,
) -> None:
    database, approvals, providers, approval = await _runtime_approval_fixture(
        tmp_path,
        "approval-concurrent-partial.db",
    )
    await providers.create(_provider_state("provider-1"))
    decided_at = datetime(2026, 7, 30, 11, 0, tzinfo=UTC)

    await asyncio.gather(
        approvals.set_provider_state_id(approval.id, "provider-1"),
        approvals.decide_if_pending(
            approval.id,
            ApprovalDecision.APPROVE_ONCE,
            decided_by="operator",
            decided_at=decided_at,
        ),
    )

    persisted = await approvals.get(approval.id)
    assert persisted is not None
    assert persisted.provider_state_id == "provider-1"
    assert persisted.status is ApprovalStatus.APPROVED
    assert persisted.decision is ApprovalDecision.APPROVE_ONCE
    assert persisted.decided_by == "operator"
    assert persisted.decided_at == decided_at
    await database.dispose()


async def test_runtime_approval_decision_cas_is_first_terminal_writer_wins(
    tmp_path: Path,
) -> None:
    database, approvals, _, approval = await _runtime_approval_fixture(
        tmp_path,
        "approval-decision-cas.db",
    )
    first_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=1)

    results = await asyncio.gather(
        approvals.decide_if_pending(
            approval.id,
            ApprovalDecision.APPROVE_TOOL_FOR_RUN,
            decided_by="operator-a",
            decided_at=first_at,
        ),
        approvals.decide_if_pending(
            approval.id,
            ApprovalDecision.REJECT_WITH_FEEDBACK,
            decided_by="operator-b",
            feedback="Denied",
            decided_at=second_at,
        ),
    )

    assert sum(changed for _, changed in results) == 1
    persisted = await approvals.get(approval.id)
    assert persisted is not None
    assert all(authoritative == persisted for authoritative, _ in results)
    if persisted.status is ApprovalStatus.APPROVED:
        assert persisted.decision is ApprovalDecision.APPROVE_TOOL_FOR_RUN
        assert persisted.decided_by == "operator-a"
        assert persisted.feedback is None
        assert persisted.decided_at == first_at
    else:
        assert persisted.status is ApprovalStatus.REJECTED
        assert persisted.decision is ApprovalDecision.REJECT_WITH_FEEDBACK
        assert persisted.decided_by == "operator-b"
        assert persisted.feedback == "Denied"
        assert persisted.decided_at == second_at
    assert persisted.decision is not None
    assert persisted.decided_by is not None
    retried, changed = await approvals.decide_if_pending(
        approval.id,
        persisted.decision,
        decided_by=persisted.decided_by,
        feedback=persisted.feedback,
        decided_at=second_at + timedelta(seconds=1),
    )
    assert changed is False
    assert retried == persisted
    await database.dispose()


async def test_runtime_approval_partial_write_validation_is_fail_closed(
    tmp_path: Path,
) -> None:
    database, approvals, _, approval = await _runtime_approval_fixture(
        tmp_path,
        "approval-validation.db",
    )
    decided_at = datetime(2026, 7, 30, 13, 0, tzinfo=UTC)

    with pytest.raises(EntityNotFoundError):
        await approvals.set_provider_state_id("missing", None)
    with pytest.raises(EntityNotFoundError):
        await approvals.decide_if_pending(
            "missing",
            ApprovalDecision.REJECT,
            decided_by="operator",
            decided_at=decided_at,
        )
    with pytest.raises(ValueError, match="reject_with_feedback"):
        await approvals.decide_if_pending(
            approval.id,
            ApprovalDecision.REJECT_WITH_FEEDBACK,
            decided_by="operator",
            feedback="",
            decided_at=decided_at,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await approvals.decide_if_pending(
            approval.id,
            ApprovalDecision.APPROVE_ONCE,
            decided_by="operator",
            decided_at=datetime(2026, 7, 30, 13, 0),
        )
    with pytest.raises(RepositoryConflictError):
        await approvals.set_provider_state_id(approval.id, "missing-provider")

    persisted = await approvals.get(approval.id)
    assert persisted == approval
    await database.dispose()
