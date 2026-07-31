from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from riftx.domain import Execution, ExecutionStatus, ExecutorType, RunnerCommandKind
from riftx.runner.control_client import LeasedRunnerCommand
from riftx.runner.daemon import RunnerDaemon, RunnerDaemonConfig


class _FailingCancellationJournal:
    async def add(self, operation_id: str) -> None:
        raise OSError(f"cannot persist {operation_id}")

    async def contains(self, operation_id: str) -> bool:
        return False


class _ExecutionRepository:
    def __init__(self, execution: Execution | None) -> None:
        self.execution = execution

    async def get_by_key(self, execution_key: str) -> Execution | None:
        if self.execution is None or self.execution.execution_key != execution_key:
            return None
        return self.execution

    async def list_active(self) -> list[Execution]:
        return []


class _Supervisor:
    def __init__(self, execution: Execution) -> None:
        self.execution = execution
        self.cancel_calls: list[str] = []
        self.cancelled = asyncio.Event()

    async def cancel(self, execution_id: str) -> Execution:
        self.cancel_calls.append(execution_id)
        if self.execution.status is ExecutionStatus.RUNNING:
            self.execution.transition_to(ExecutionStatus.CANCELLED)
        self.cancelled.set()
        return self.execution

    async def close(self, *, cancel_running: bool = False) -> None:
        return None


class _FailingSupervisor(_Supervisor):
    async def cancel(self, execution_id: str) -> Execution:
        self.cancel_calls.append(execution_id)
        raise RuntimeError("process termination failed")


class _RunnerClient:
    def __init__(self) -> None:
        self.finished: list[tuple[str, bool, dict[str, object], str]] = []
        self.statuses: list[tuple[str, ExecutionStatus]] = []

    async def finish(
        self,
        command: LeasedRunnerCommand,
        *,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        self.finished.append((command.id, succeeded, result or {}, error))

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        **_: object,
    ) -> None:
        self.statuses.append((execution_id, status))

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_cancel_still_stops_process_when_cancellation_journal_write_fails(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=_FailingCancellationJournal(),
    )
    command = _command(
        "cancel-1",
        RunnerCommandKind.CANCEL,
        {"execution_id": "server-execution-1", "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert client.statuses == [("server-execution-1", ExecutionStatus.CANCELLED)]
    assert client.finished[0][0:2] == (command.id, False)
    assert "was stopped locally" in client.finished[0][3]
    assert "tombstone could not be persisted" in client.finished[0][3]


@pytest.mark.asyncio
async def test_cancel_without_local_execution_fails_when_tombstone_cannot_be_written(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=_FailingCancellationJournal(),
    )
    command = _command(
        "cancel-before-execute",
        RunnerCommandKind.CANCEL,
        {"execution_id": "server-execution-1", "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == []
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "cannot be guaranteed" in client.finished[0][3]


@pytest.mark.asyncio
async def test_cancel_reports_both_failures_when_journal_and_process_stop_fail(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(execution)
    supervisor = _FailingSupervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=_FailingCancellationJournal(),
    )
    command = _command(
        "cancel-1",
        RunnerCommandKind.CANCEL,
        {"execution_id": "server-execution-1", "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.RUNNING
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "tombstone could not be persisted" in client.finished[0][3]
    assert "process termination also failed" in client.finished[0][3]


def _daemon(
    tmp_path: Path,
    *,
    client: object,
    supervisor: _Supervisor,
    repository: _ExecutionRepository,
    journal: object | None = None,
) -> RunnerDaemon:
    return RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
            poll_wait_seconds=0.01,
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=supervisor,  # type: ignore[arg-type]
        executions=repository,  # type: ignore[arg-type]
        execution_cancellation_journal=journal,  # type: ignore[arg-type]
    )


def _execution(tmp_path: Path) -> Execution:
    return Execution(
        id="local-execution-1",
        execution_key="execution-key-1",
        run_id="run-1",
        node_id="runner-a",
        executor_type=ExecutorType.PROCESS,
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "stdout"),
        stderr_path=str(tmp_path / "stderr"),
        status=ExecutionStatus.RUNNING,
    )


def _command(
    command_id: str,
    kind: RunnerCommandKind,
    payload: dict[str, object],
) -> LeasedRunnerCommand:
    return LeasedRunnerCommand(
        id=command_id,
        kind=kind,
        payload=payload,
        lease_id=f"lease-{command_id}",
        attempts=1,
    )
