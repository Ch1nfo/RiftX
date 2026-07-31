from datetime import UTC, datetime
from pathlib import Path

import pytest

from riftx.domain import Execution, ExecutionStatus, ExecutorType, RunnerPrincipal
from riftx.persistence.mappers import (
    apply_execution_to_record,
    execution_from_record,
    execution_to_record,
)


def test_execution_mapper_round_trip() -> None:
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
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
    )

    assert execution_from_record(execution_to_record(execution)) == execution


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
    record = execution_to_record(execution)
    stale = execution.model_copy(update={"physical_stop_confirmed_at": None})

    with pytest.raises(ValueError, match="physical_stop_confirmed_at.*immutable"):
        apply_execution_to_record(stale, record)
