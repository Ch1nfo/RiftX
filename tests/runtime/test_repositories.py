import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from riftx.application.errors import (
    EntityNotFoundError,
    PentestBudgetExceededError,
    RepositoryConflictError,
)
from riftx.context import ContextCompilation, ContextManifest
from riftx.domain import (
    ApprovalLevel,
    ApprovalStatus,
    Engagement,
    EntryPoint,
    EntryPointKind,
    Objective,
    PentestAdmission,
    PentestBudget,
    Run,
    RunKind,
    RunStatus,
    Scope,
)
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
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
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
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Map the authorized target"),
            workspace_path="/tmp/riftx/run-1",
        )
    )


async def create_pentest_run(
    database: Database,
    *,
    budget: PentestBudget,
    created_at: datetime,
) -> None:
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(
            id="engagement-1",
            name="Authorized Pentest",
            authorization_reference="ticket://runtime-budget",
        )
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind=RunKind.PENTEST,
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Assess the authorized target"),
            entry_points=[EntryPoint(kind=EntryPointKind.IP, value="127.0.0.1")],
            scope=Scope(ips=["127.0.0.1"]),
            pentest_admission=PentestAdmission(budget=budget),
            workspace_path="/tmp/riftx/run-1",
            created_at=created_at,
        )
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)


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


async def test_yield_usage_merge_rolls_back_session_when_cycle_update_fails(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'yield-atomic.db'}")
    await database.create_schema()
    await create_run(database)
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    cycles = SQLAlchemyAgentCycleRepository(database.session_factory)
    agent_session = AgentSession(
        id="session-1",
        run_id="run-1",
        model_profile="default",
    )
    cycle = AgentCycle(
        id="cycle-1",
        run_id="run-1",
        session_id="session-1",
        sequence=1,
        status=CycleStatus.RUNNING,
        model_call_count=1,
        tool_call_count=2,
    )
    await sessions.create(agent_session)
    await cycles.create(cycle)
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TRIGGER reject_cycle_yield
                BEFORE UPDATE ON agent_cycles
                WHEN NEW.status = 'yielded'
                BEGIN
                    SELECT RAISE(ABORT, 'injected cycle yield failure');
                END
                """
            )
        )

    agent_session.model_call_count = 1
    agent_session.tool_call_count = 2
    cycle.status = CycleStatus.YIELDED
    cycle.yield_reason = YieldReason.CYCLE_LIMIT_REACHED
    with pytest.raises(SQLAlchemyError, match="injected cycle yield failure"):
        await cycles.save_yield(agent_session, cycle)

    stored_session = await sessions.get(agent_session.id)
    stored_cycle = await cycles.get(cycle.id)
    assert stored_session is not None
    assert stored_session.model_call_count == 0
    assert stored_session.tool_call_count == 0
    assert stored_cycle is not None
    assert stored_cycle.status is CycleStatus.RUNNING
    assert stored_cycle.yield_reason is None
    await database.dispose()


async def _create_context_compilation(
    database: Database,
    *,
    compilation_id: str,
    session_id: str,
    actual_input_tokens: int | None = None,
    actual_output_tokens: int | None = None,
) -> None:
    await SQLAlchemyContextCompilationRepository(database.session_factory).create(
        ContextCompilation(
            id=compilation_id,
            run_id="run-1",
            session_id=session_id,
            agent_id="primary",
            model_profile="default",
            purpose="pentest-cycle",
            manifest=ContextManifest.empty(
                run_id="run-1",
                session_id=session_id,
                agent_id="primary",
                model_profile="default",
                purpose="pentest-cycle",
            ),
            actual_input_tokens=actual_input_tokens,
            actual_output_tokens=actual_output_tokens,
        )
    )


async def test_pentest_model_claim_requires_complete_prior_usage_and_counts_once(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'model-claim.db'}")
    await database.create_schema()
    await create_pentest_run(
        database,
        budget=PentestBudget(
            max_duration_seconds=600,
            max_model_calls=2,
            max_tokens=100,
            max_tool_calls=10,
            max_target_interactions=10,
            max_concurrent_target_interactions=1,
        ),
        created_at=now,
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    cycles = SQLAlchemyAgentCycleRepository(
        database.session_factory,
        clock=lambda: now + timedelta(seconds=1),
    )
    contexts = SQLAlchemyContextCompilationRepository(database.session_factory)
    await sessions.create(
        AgentSession(id="session-1", run_id="run-1", model_profile="default")
    )
    await cycles.create(
        AgentCycle(
            id="cycle-1",
            run_id="run-1",
            session_id="session-1",
            sequence=1,
            status=CycleStatus.RUNNING,
        )
    )
    await _create_context_compilation(
        database,
        compilation_id="context-1",
        session_id="session-1",
    )

    claimed = await cycles.claim_pentest_model_call(
        run_id="run-1",
        cycle_id="cycle-1",
        compilation_id="context-1",
    )
    assert claimed.model_call_count == 1
    await cycles.create(
        AgentCycle(
            id="cycle-2",
            run_id="run-1",
            session_id="session-1",
            sequence=2,
            status=CycleStatus.RUNNING,
        )
    )
    await _create_context_compilation(
        database,
        compilation_id="context-2",
        session_id="session-1",
    )
    with pytest.raises(PentestBudgetExceededError) as incomplete:
        await cycles.claim_pentest_model_call(
            run_id="run-1",
            cycle_id="cycle-2",
            compilation_id="context-2",
        )
    assert incomplete.value.budget_name == "max_tokens"
    assert incomplete.value.reason == "token_usage_incomplete"
    unclaimed = await cycles.get("cycle-2")
    assert unclaimed is not None and unclaimed.model_call_count == 0

    await contexts.update_usage(
        "context-1",
        actual_input_tokens=20,
        actual_output_tokens=5,
    )
    claimed = await cycles.claim_pentest_model_call(
        run_id="run-1",
        cycle_id="cycle-2",
        compilation_id="context-2",
    )
    assert claimed.model_call_count == 1

    await contexts.update_usage(
        "context-2",
        actual_input_tokens=10,
        actual_output_tokens=5,
    )
    await cycles.create(
        AgentCycle(
            id="cycle-3",
            run_id="run-1",
            session_id="session-1",
            sequence=3,
            status=CycleStatus.RUNNING,
        )
    )
    await _create_context_compilation(
        database,
        compilation_id="context-3",
        session_id="session-1",
    )
    restarted_cycles = SQLAlchemyAgentCycleRepository(
        database.session_factory,
        clock=lambda: now + timedelta(seconds=2),
    )
    with pytest.raises(PentestBudgetExceededError) as exhausted:
        await restarted_cycles.claim_pentest_model_call(
            run_id="run-1",
            cycle_id="cycle-3",
            compilation_id="context-3",
        )
    assert exhausted.value.budget_name == "max_model_calls"
    assert exhausted.value.used == 2
    await database.dispose()


async def test_pentest_model_claim_enforces_tokens_duration_and_concurrency(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'model-bounds.db'}")
    await database.create_schema()
    await create_pentest_run(
        database,
        budget=PentestBudget(
            max_duration_seconds=60,
            max_model_calls=1,
            max_tokens=10,
            max_tool_calls=10,
            max_target_interactions=10,
            max_concurrent_target_interactions=1,
        ),
        created_at=now,
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(
        AgentSession(id="session-1", run_id="run-1", model_profile="default")
    )
    cycles = SQLAlchemyAgentCycleRepository(
        database.session_factory,
        clock=lambda: now + timedelta(seconds=1),
    )
    for index in (1, 2):
        await cycles.create(
            AgentCycle(
                id=f"cycle-{index}",
                run_id="run-1",
                session_id="session-1",
                sequence=index,
                status=CycleStatus.RUNNING,
            )
        )
        await _create_context_compilation(
            database,
            compilation_id=f"context-{index}",
            session_id="session-1",
        )

    results = await asyncio.gather(
        *(
            cycles.claim_pentest_model_call(
                run_id="run-1",
                cycle_id=f"cycle-{index}",
                compilation_id=f"context-{index}",
            )
            for index in (1, 2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, AgentCycle) for result in results) == 1
    conflicts = [
        result for result in results if isinstance(result, PentestBudgetExceededError)
    ]
    assert len(conflicts) == 1
    assert conflicts[0].budget_name == "max_model_calls"

    await SQLAlchemyContextCompilationRepository(
        database.session_factory
    ).update_usage(
        "context-1" if isinstance(results[0], AgentCycle) else "context-2",
        actual_input_tokens=8,
        actual_output_tokens=2,
    )
    duration_cycles = SQLAlchemyAgentCycleRepository(
        database.session_factory,
        clock=lambda: now + timedelta(seconds=60),
    )
    losing_index = 2 if isinstance(results[0], AgentCycle) else 1
    with pytest.raises(PentestBudgetExceededError) as duration:
        await duration_cycles.claim_pentest_model_call(
            run_id="run-1",
            cycle_id=f"cycle-{losing_index}",
            compilation_id=f"context-{losing_index}",
        )
    assert duration.value.budget_name == "max_duration_seconds"
    assert duration.value.used == 60
    await database.dispose()


async def test_pentest_model_claim_rejects_exhausted_token_budget(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'token-bound.db'}")
    await database.create_schema()
    await create_pentest_run(
        database,
        budget=PentestBudget(
            max_duration_seconds=60,
            max_model_calls=2,
            max_tokens=10,
            max_tool_calls=10,
            max_target_interactions=10,
            max_concurrent_target_interactions=1,
        ),
        created_at=now,
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    cycles = SQLAlchemyAgentCycleRepository(
        database.session_factory,
        clock=lambda: now + timedelta(seconds=1),
    )
    await sessions.create(
        AgentSession(id="session-1", run_id="run-1", model_profile="default")
    )
    for index in (1, 2):
        await cycles.create(
            AgentCycle(
                id=f"cycle-{index}",
                run_id="run-1",
                session_id="session-1",
                sequence=index,
                status=CycleStatus.RUNNING,
            )
        )
        await _create_context_compilation(
            database,
            compilation_id=f"context-{index}",
            session_id="session-1",
        )

    await cycles.claim_pentest_model_call(
        run_id="run-1",
        cycle_id="cycle-1",
        compilation_id="context-1",
    )
    await SQLAlchemyContextCompilationRepository(
        database.session_factory
    ).update_usage(
        "context-1",
        actual_input_tokens=8,
        actual_output_tokens=2,
    )

    with pytest.raises(PentestBudgetExceededError) as exhausted:
        await cycles.claim_pentest_model_call(
            run_id="run-1",
            cycle_id="cycle-2",
            compilation_id="context-2",
        )
    assert exhausted.value.budget_name == "max_tokens"
    assert exhausted.value.used == 10
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


async def test_tool_intent_recent_history_is_bounded_ordered_and_session_scoped(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'intent-history.db'}")
    await database.create_schema()
    await create_run(database)
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    cycles = SQLAlchemyAgentCycleRepository(database.session_factory)
    steps = SQLAlchemyAgentStepRepository(database.session_factory)
    for suffix in ("1", "2"):
        await sessions.create(
            AgentSession(
                id=f"session-{suffix}",
                run_id="run-1",
                model_profile="default",
            )
        )
        await cycles.create(
            AgentCycle(
                id=f"cycle-{suffix}",
                run_id="run-1",
                session_id=f"session-{suffix}",
                sequence=int(suffix),
            )
        )
        await steps.create(
            AgentStep(
                id=f"step-{suffix}",
                cycle_id=f"cycle-{suffix}",
                sequence=1,
                step_type=AgentStepType.TOOL_PROPOSAL,
            )
        )

    intents = SQLAlchemyToolCallIntentRepository(database.session_factory)
    base_time = datetime(2026, 8, 6, tzinfo=UTC)
    try:
        for index in range(4):
            await intents.create(
                ToolCallIntent(
                    id=f"intent-{index}",
                    run_id="run-1",
                    session_id="session-1",
                    cycle_id="cycle-1",
                    step_id="step-1",
                    tool_id=f"tool-{index}",
                    created_at=base_time + timedelta(seconds=index),
                )
            )
        await intents.create(
            ToolCallIntent(
                id="other-session-intent",
                run_id="run-1",
                session_id="session-2",
                cycle_id="cycle-2",
                step_id="step-2",
                tool_id="other-tool",
                created_at=base_time + timedelta(minutes=1),
            )
        )

        recent = await intents.recent_for_session("session-1", limit=2)
        assert [intent.id for intent in recent] == ["intent-2", "intent-3"]
        assert await intents.recent_for_session("missing-session") == []
        with pytest.raises(ValueError, match="between 1 and 1000"):
            await intents.recent_for_session("session-1", limit=0)
        with pytest.raises(ValueError, match="between 1 and 1000"):
            await intents.recent_for_session("session-1", limit=1001)
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
