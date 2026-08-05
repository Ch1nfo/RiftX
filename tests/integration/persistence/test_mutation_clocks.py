import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from riftx.domain import (
    ApprovalLevel,
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    RunnerPrincipal,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.persistence.orm import ExecutionRecord, ToolCallIntentRecord
from riftx.persistence.runtime_mappers import (
    apply_tool_call_intent_to_record,
    tool_call_intent_to_record,
)
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ToolCallIntent,
    ToolCallStatus,
)


@dataclass
class MutableClock:
    value: datetime
    calls: int = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.value


async def _create_runtime_graph(database: Database, tmp_path: Path) -> None:
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-clock", name="Mutation clock")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-clock",
            engagement_id="engagement-clock",
            node_id="node-clock",
            objective=Objective(description="Mutation clock"),
            workspace_path=str(tmp_path),
        )
    )
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="session-clock", run_id="run-clock", model_profile="default")
    )
    await SQLAlchemyAgentCycleRepository(database.session_factory).create(
        AgentCycle(
            id="cycle-clock",
            run_id="run-clock",
            session_id="session-clock",
            sequence=1,
        )
    )
    await SQLAlchemyAgentStepRepository(database.session_factory).create(
        AgentStep(
            id="step-clock",
            cycle_id="cycle-clock",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        )
    )


def _intent(*, intent_id: str = "intent-clock", created_at: datetime) -> ToolCallIntent:
    return ToolCallIntent(
        id=intent_id,
        run_id="run-clock",
        session_id="session-clock",
        cycle_id="cycle-clock",
        step_id="step-clock",
        tool_id="python",
        approval_level=ApprovalLevel.SENSITIVE,
        created_at=created_at,
    )


def _execution(
    tmp_path: Path,
    *,
    execution_id: str = "execution-clock",
    execution_key: str = "execution-clock-key",
    created_at: datetime,
) -> Execution:
    return Execution(
        id=execution_id,
        execution_key=execution_key,
        run_id="run-clock",
        node_id="node-clock",
        executor_type=ExecutorType.PROCESS,
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{execution_id}.stdout.log"),
        stderr_path=str(tmp_path / f"{execution_id}.stderr.log"),
        created_at=created_at,
    )


async def _intent_updated_at(database: Database, intent_id: str) -> datetime:
    async with database.session_factory() as session:
        value = await session.scalar(
            select(ToolCallIntentRecord.updated_at).where(ToolCallIntentRecord.id == intent_id)
        )
    assert value is not None
    return value


async def _execution_record(database: Database, execution_id: str) -> ExecutionRecord:
    async with database.session_factory() as session:
        record = await session.get(ExecutionRecord, execution_id)
        assert record is not None
        session.expunge(record)
    return record


def test_tool_call_intent_mapper_keeps_clock_store_owned() -> None:
    created_at = datetime(2026, 8, 1, tzinfo=UTC)
    updated_at = created_at + timedelta(seconds=1)
    intent = _intent(created_at=created_at)

    with pytest.raises(TypeError, match="updated_at"):
        tool_call_intent_to_record(intent)  # type: ignore[call-arg]

    record = tool_call_intent_to_record(intent, updated_at=updated_at)
    intent.reason = "metadata changed"
    intent.status = ToolCallStatus.CANCELLED
    apply_tool_call_intent_to_record(intent, record)

    assert "updated_at" not in ToolCallIntent.model_fields
    assert "claimed_execution_key" not in ToolCallIntent.model_fields
    assert "claimed_attempt_group" not in ToolCallIntent.model_fields
    assert record.reason == "metadata changed"
    assert record.status == ToolCallStatus.PROPOSED.value
    assert record.claimed_execution_key is None
    assert record.claimed_attempt_group is None
    assert record.updated_at == updated_at


async def test_intent_clock_advances_only_for_actual_mutations(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'intent-clock.db'}")
    await database.create_schema()
    await _create_runtime_graph(database, tmp_path)
    tick = datetime(2026, 8, 1, 12, tzinfo=UTC)
    clock = MutableClock(tick)
    repository = SQLAlchemyToolCallIntentRepository(
        database.session_factory,
        clock=clock,
    )
    intent = _intent(created_at=tick - timedelta(minutes=1))
    try:
        await repository.create(intent)
        created_stamp = await _intent_updated_at(database, intent.id)
        assert created_stamp == tick
        assert created_stamp.utcoffset() == timedelta(0)

        await repository.save(intent)
        assert await _intent_updated_at(database, intent.id) == created_stamp
        assert clock.calls == 1

        intent.status = ToolCallStatus.WAITING_APPROVAL
        authoritative = await repository.save(intent)
        assert authoritative.status is ToolCallStatus.PROPOSED
        assert await _intent_updated_at(database, intent.id) == created_stamp

        waiting, changed = await repository.compare_and_set_status(
            intent.id,
            expected={ToolCallStatus.PROPOSED},
            target=ToolCallStatus.WAITING_APPROVAL,
        )
        assert changed is True
        assert waiting.status is ToolCallStatus.WAITING_APPROVAL
        status_stamp = await _intent_updated_at(database, intent.id)
        assert status_stamp == created_stamp + timedelta(microseconds=1)

        intent.target_summary = "authorized target"
        await repository.save(intent)
        metadata_stamp = await _intent_updated_at(database, intent.id)
        assert metadata_stamp == status_stamp + timedelta(microseconds=1)

        ready, changed = await repository.compare_and_set_status(
            intent.id,
            expected={ToolCallStatus.WAITING_APPROVAL},
            target=ToolCallStatus.READY,
        )
        assert changed is True
        assert ready.status is ToolCallStatus.READY
        cas_stamp = await _intent_updated_at(database, intent.id)
        assert cas_stamp == metadata_stamp + timedelta(microseconds=1)

        failed, changed = await repository.compare_and_set_status(
            intent.id,
            expected={ToolCallStatus.EXECUTING},
            target=ToolCallStatus.COMPLETED,
        )
        assert changed is False
        assert failed.status is ToolCallStatus.READY
        assert await _intent_updated_at(database, intent.id) == cas_stamp

        same, changed = await repository.compare_and_set_status(
            intent.id,
            expected={ToolCallStatus.READY},
            target=ToolCallStatus.READY,
        )
        assert changed is True
        assert same.status is ToolCallStatus.READY
        assert await _intent_updated_at(database, intent.id) == cas_stamp

        clock.value = tick - timedelta(days=1)
        intent = await repository.get(intent.id)
        assert intent is not None
        intent.reason = "clock rolled back"
        await repository.save(intent)
        assert await _intent_updated_at(database, intent.id) == cas_stamp + timedelta(
            microseconds=1
        )
    finally:
        await database.dispose()


async def test_intent_cas_is_serialized_on_sqlite(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'intent-cas-clock.db'}")
    await database.create_schema()
    await _create_runtime_graph(database, tmp_path)
    tick = datetime(2026, 8, 1, 13, tzinfo=UTC)
    repository = SQLAlchemyToolCallIntentRepository(
        database.session_factory,
        clock=lambda: tick,
    )
    intent = _intent(created_at=tick - timedelta(seconds=1))
    try:
        await repository.create(intent)
        results = await asyncio.gather(
            repository.compare_and_set_status(
                intent.id,
                expected={ToolCallStatus.PROPOSED},
                target=ToolCallStatus.READY,
            ),
            repository.compare_and_set_status(
                intent.id,
                expected={ToolCallStatus.PROPOSED},
                target=ToolCallStatus.CANCELLED,
            ),
        )
        assert sorted(changed for _, changed in results) == [False, True]
        assert await _intent_updated_at(database, intent.id) == tick + timedelta(microseconds=1)
    finally:
        await database.dispose()


async def test_execution_clock_covers_all_writer_paths_and_metadata(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution-clock.db'}")
    await database.create_schema()
    await _create_runtime_graph(database, tmp_path)
    tick = datetime(2026, 8, 1, 14, tzinfo=UTC)
    clock = MutableClock(tick)
    repository = SQLAlchemyExecutionRepository(database.session_factory, clock=clock)
    execution = _execution(tmp_path, created_at=tick - timedelta(minutes=1))
    try:
        _, created = await repository.create_if_absent(execution)
        assert created is True
        created_record = await _execution_record(database, execution.id)
        assert created_record.updated_at == tick
        assert created_record.updated_at.utcoffset() == timedelta(0)
        assert "updated_at" not in Execution.model_fields

        duplicate = execution.model_copy(update={"id": "execution-duplicate"})
        existing, created = await repository.create_if_absent(duplicate)
        assert created is False
        assert existing.id == execution.id
        assert (await _execution_record(database, execution.id)).updated_at == tick
        assert clock.calls == 1

        await repository.save(execution)
        assert (await _execution_record(database, execution.id)).updated_at == tick
        assert clock.calls == 1

        execution.status = ExecutionStatus.STARTING
        await repository.save(execution)
        status_stamp = (await _execution_record(database, execution.id)).updated_at
        assert status_stamp == tick + timedelta(microseconds=1)

        execution.owner = RunnerPrincipal(instance_id="runner-clock", epoch=1)
        await repository.save(execution)
        owner_stamp = (await _execution_record(database, execution.id)).updated_at
        assert owner_stamp == status_stamp + timedelta(microseconds=1)

        future_process_time = tick + timedelta(seconds=10)
        execution.pid = 4242
        execution.process_group_id = 4242
        execution.containment_id = "containment-clock"
        execution.process_created_at = future_process_time
        await repository.save(execution)
        identity_stamp = (await _execution_record(database, execution.id)).updated_at
        assert identity_stamp == future_process_time

        execution.exit_code = 7
        _, changed = await repository.save_if_status(
            execution,
            expected={ExecutionStatus.STARTING},
        )
        assert changed is True
        conditional_stamp = (await _execution_record(database, execution.id)).updated_at
        assert conditional_stamp == identity_stamp + timedelta(microseconds=1)

        _, changed = await repository.save_if_status(
            execution,
            expected={ExecutionStatus.STARTING},
        )
        assert changed is True
        assert (await _execution_record(database, execution.id)).updated_at == conditional_stamp

        failed_update = execution.model_copy(update={"exit_code": 99})
        current, changed = await repository.save_if_status(
            failed_update,
            expected={ExecutionStatus.RUNNING},
        )
        failed_record = await _execution_record(database, execution.id)
        assert changed is False
        assert current.status is ExecutionStatus.STARTING
        assert failed_record.exit_code == 7
        assert failed_record.updated_at == conditional_stamp
    finally:
        await database.dispose()


async def test_naive_repository_clocks_fail_closed(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'naive-clock.db'}")
    await database.create_schema()
    await _create_runtime_graph(database, tmp_path)
    aware_tick = datetime(2026, 8, 1, 15, tzinfo=UTC)
    naive_tick = aware_tick.replace(tzinfo=None)
    naive_intents = SQLAlchemyToolCallIntentRepository(
        database.session_factory,
        clock=lambda: naive_tick,
    )
    naive_executions = SQLAlchemyExecutionRepository(
        database.session_factory,
        clock=lambda: naive_tick,
    )
    intent = _intent(intent_id="intent-naive", created_at=aware_tick)
    execution = _execution(
        tmp_path,
        execution_id="execution-naive",
        execution_key="execution-naive-key",
        created_at=aware_tick,
    )
    try:
        with pytest.raises(ValueError, match="clock.*timezone-aware"):
            await naive_intents.create(intent)
        with pytest.raises(ValueError, match="clock.*timezone-aware"):
            await naive_executions.create_if_absent(execution)
        assert await naive_intents.get(intent.id) is None
        assert await naive_executions.get(execution.id) is None

        aware_intents = SQLAlchemyToolCallIntentRepository(
            database.session_factory,
            clock=lambda: aware_tick,
        )
        aware_executions = SQLAlchemyExecutionRepository(
            database.session_factory,
            clock=lambda: aware_tick,
        )
        await aware_intents.create(intent)
        await aware_executions.create_if_absent(execution)
        intent_stamp = await _intent_updated_at(database, intent.id)
        execution_stamp = (await _execution_record(database, execution.id)).updated_at

        intent.reason = "rejected"
        execution.exit_code = 9
        with pytest.raises(ValueError, match="clock.*timezone-aware"):
            await naive_intents.save(intent)
        with pytest.raises(ValueError, match="clock.*timezone-aware"):
            await naive_executions.save(execution)
        assert await _intent_updated_at(database, intent.id) == intent_stamp
        persisted_execution = await _execution_record(database, execution.id)
        assert persisted_execution.updated_at == execution_stamp
        assert persisted_execution.exit_code is None
    finally:
        await database.dispose()
