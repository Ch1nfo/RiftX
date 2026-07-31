from __future__ import annotations

from pathlib import Path

import pytest

from riftx.domain import Execution, ExecutionStatus, ExecutorType, RunnerCommandKind
from riftx.runner.paths import RunnerPaths
from riftx.runner.remote import RemoteExecutionSupervisor
from riftx.runner.state import FileExecutionRepository


class FakeControlService:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, RunnerCommandKind, str, dict[str, object]]] = []

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> tuple[object, bool]:
        self.enqueued.append((node_id, kind, idempotency_key, payload))
        return object(), True


def make_execution(tmp_path: Path, status: ExecutionStatus) -> Execution:
    execution = Execution(
        id=f"execution-{status.value}",
        execution_key=f"key-{status.value}",
        run_id="run-1",
        node_id="remote-node",
        executor_type=ExecutorType.PROCESS,
        argv=["sleep", "30"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{status.value}.stdout"),
        stderr_path=str(tmp_path / f"{status.value}.stderr"),
    )
    if status is ExecutionStatus.CANCELLED:
        execution.transition_to(status)
        return execution
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(status)
    return execution


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.LOST,
        ExecutionStatus.FAILED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.EXITED,
        ExecutionStatus.HARD_TIMEOUT,
    ],
)
async def test_terminal_remote_execution_still_gets_cancel_tombstone(
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    execution = make_execution(tmp_path, status)
    await repository.create_if_absent(execution)
    control = FakeControlService()
    supervisor = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        control,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    returned = await supervisor.cancel(execution.id)

    assert returned.status is status
    assert len(control.enqueued) == 1
    node_id, kind, idempotency_key, payload = control.enqueued[0]
    assert node_id == execution.node_id
    assert kind is RunnerCommandKind.CANCEL
    assert idempotency_key.startswith(f"cancel:{execution.id}:")
    assert payload == {
        "execution_id": execution.id,
        "execution_key": execution.execution_key,
    }


async def test_cancelled_remote_execution_short_circuits_idempotently(tmp_path: Path) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    execution = make_execution(tmp_path, ExecutionStatus.CANCELLED)
    await repository.create_if_absent(execution)
    control = FakeControlService()
    supervisor = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        control,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    first = await supervisor.cancel(execution.id)
    second = await supervisor.cancel(execution.id)

    assert first.status is ExecutionStatus.CANCELLED
    assert second.status is ExecutionStatus.CANCELLED
    assert control.enqueued == []
