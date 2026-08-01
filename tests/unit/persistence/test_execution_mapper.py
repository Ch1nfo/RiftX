from datetime import UTC, datetime
from pathlib import Path

import pytest

from riftx.domain import Execution, ExecutionStatus, ExecutorType, RunnerPrincipal
from riftx.persistence.mappers import (
    apply_execution_to_record,
    execution_from_record,
    execution_to_record,
)

UPDATED_AT = datetime(2026, 8, 1, 0, 0, 1, tzinfo=UTC)


def test_execution_mapper_round_trip() -> None:
    created_at = datetime(2026, 7, 31, 23, 59, tzinfo=UTC)
    execution = Execution(
        id="execution-1",
        execution_key="key-1",
        run_id="run-1",
        node_id="node-1",
        owner=RunnerPrincipal(instance_id="runner-instance-1", epoch=7),
        executor_type=ExecutorType.PROCESS,
        argv=["printf", "ok"],
        cwd=str(Path("/tmp")),
        env_diff={"VALUE": "1", "REMOVED": None},
        status=ExecutionStatus.EXITED,
        physical_stop_confirmed_at=datetime(2026, 8, 1, tzinfo=UTC),
        created_at=created_at,
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
    )

    assert execution_from_record(execution_to_record(execution, updated_at=UPDATED_AT)) == execution
    assert execution_to_record(execution, updated_at=UPDATED_AT).created_at == created_at


def test_execution_mapper_preserves_unknown_legacy_creation_time() -> None:
    execution = Execution(
        execution_key="legacy-order",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        cwd="/tmp",
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
    )
    record = execution_to_record(execution, updated_at=UPDATED_AT)
    record.created_at = None

    restored = execution_from_record(record)

    assert restored.created_at is None


def test_execution_mapper_rejects_creation_time_replacement() -> None:
    execution = Execution(
        execution_key="immutable-order",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        cwd="/tmp",
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    record = execution_to_record(execution, updated_at=UPDATED_AT)
    replaced = execution.model_copy(update={"created_at": datetime(2026, 8, 2, tzinfo=UTC)})

    with pytest.raises(ValueError, match="created_at.*immutable"):
        apply_execution_to_record(replaced, record)


def test_execution_mapper_cannot_remove_durable_physical_stop_proof() -> None:
    execution = Execution(
        execution_key="immutable-proof",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        cwd="/tmp",
        status=ExecutionStatus.EXITED,
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
        physical_stop_confirmed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    record = execution_to_record(execution, updated_at=UPDATED_AT)
    stale = execution.model_copy(update={"physical_stop_confirmed_at": None})

    with pytest.raises(ValueError, match="physical_stop_confirmed_at.*immutable"):
        apply_execution_to_record(stale, record)


def test_execution_apply_mapper_does_not_mutate_store_clock() -> None:
    execution = Execution(
        execution_key="mapper-clock",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        cwd="/tmp",
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    record = execution_to_record(execution, updated_at=UPDATED_AT)

    execution.exit_code = 7
    apply_execution_to_record(execution, record)

    assert record.exit_code == 7
    assert record.updated_at == UPDATED_AT
