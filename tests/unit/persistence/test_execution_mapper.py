from pathlib import Path

from riftx.domain import Execution, ExecutionStatus, ExecutorType
from riftx.persistence.mappers import execution_from_record, execution_to_record


def test_execution_mapper_round_trip() -> None:
    execution = Execution(
        id="execution-1",
        execution_key="key-1",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        argv=["printf", "ok"],
        cwd=str(Path("/tmp")),
        env_diff={"VALUE": "1", "REMOVED": None},
        status=ExecutionStatus.CREATED,
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
    )

    assert execution_from_record(execution_to_record(execution)) == execution
