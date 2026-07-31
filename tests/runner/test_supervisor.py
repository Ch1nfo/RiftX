from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from riftx.domain import Engagement, Execution, ExecutionStatus, ExecutorType, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import ExecutionLaunchRequest, ProcessSupervisor, RunnerPaths
from riftx.runner.supervisor import ProcessTerminationError

FIXTURE = Path(__file__).parent / "fixtures" / "fake_process.py"
PYTHON_EXECUTABLE = str(Path(sys.executable).resolve())


def wait_for_nonempty_file(path: Path, deadline_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {path}")


def wait_for_process_exit(pid: int, deadline_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for process {pid} to exit")


class AlwaysMatchingInspector:
    async def matches(self, execution: Execution) -> bool:
        return True


class NeverMatchingInspector:
    async def matches(self, execution: Execution) -> bool:
        return False


class MatchThenMissingInspector:
    def __init__(self) -> None:
        self.calls = 0

    async def matches(self, execution: Execution) -> bool:
        self.calls += 1
        return self.calls == 1


async def make_supervisor(
    tmp_path: Path,
    *,
    on_completed: Callable[[Execution], Awaitable[None]] | None = None,
) -> tuple[Database, SQLAlchemyExecutionRepository, ProcessSupervisor]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Runner tests"))
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local-node",
            objective=Objective(description="Exercise runner"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        termination_grace_seconds=0.1,
        on_completed=on_completed,
    )
    return database, executions, supervisor


def launch_request(
    tmp_path: Path,
    key: str,
    *fixture_args: str,
    timeout_seconds: float | None = None,
) -> ExecutionLaunchRequest:
    return ExecutionLaunchRequest(
        execution_key=key,
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        cwd=tmp_path,
        argv=[sys.executable, str(FIXTURE), *fixture_args],
        tool_id="fake-process",
        tool_version="1.2.3",
        env={"RIFTX_TEST_VALUE": "supervised"},
        timeout_seconds=timeout_seconds,
    )


async def test_supervisor_persists_lifecycle_and_reads_output_by_cursor(
    tmp_path: Path,
) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(launch_request(tmp_path, "success-key", "success"))
    completed = await supervisor.wait(started.id)

    first = await supervisor.read_output(completed.id, max_bytes=8)
    second = await supervisor.read_output(
        completed.id,
        stdout_cursor=first.stdout.next_cursor,
        stderr_cursor=first.stderr.next_cursor,
        max_bytes=1024,
    )

    assert completed.status is ExecutionStatus.EXITED
    assert completed.exit_code == 0
    assert completed.tool_id == "fake-process"
    assert completed.tool_version == "1.2.3"
    assert completed.executable_path == PYTHON_EXECUTABLE
    assert completed.platform_system
    assert completed.platform_release
    assert completed.platform_architecture
    assert completed.process_created_at == completed.started_at
    assert first.stdout.data + second.stdout.data == (
        b"stdout: \xe4\xbd\xa0\xe5\xa5\xbd RiftX\nenv: supervised\n"
    )
    assert first.stderr.data + second.stderr.data == b"stderr: diagnostic\n"
    assert second.stdout.eof is True
    assert second.stderr.eof is True
    await supervisor.close()
    await database.dispose()


async def test_supervisor_notifies_after_persisting_completion(tmp_path: Path) -> None:
    completed_ids: list[str] = []

    async def notify(execution: Execution) -> None:
        completed_ids.append(execution.id)

    database, executions, supervisor = await make_supervisor(
        tmp_path,
        on_completed=notify,
    )
    started = await supervisor.start(launch_request(tmp_path, "notify-key", "success"))
    completed = await supervisor.wait(started.id)

    assert completed_ids == [completed.id]
    persisted = await executions.get(completed.id)
    assert persisted is not None and persisted.status is ExecutionStatus.EXITED
    await supervisor.close()
    await database.dispose()


async def test_output_is_readable_while_process_is_running(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(launch_request(tmp_path, "stream-key", "stream"))
    await asyncio.to_thread(wait_for_nonempty_file, Path(started.stdout_path))

    first = await supervisor.read_output(started.id)
    completed = await supervisor.wait(started.id)
    second = await supervisor.read_output(started.id, stdout_cursor=first.stdout.next_cursor)

    assert completed.status is ExecutionStatus.EXITED
    assert first.stdout.data == b"first\n"
    assert second.stdout.data == b"second\n"
    await supervisor.close()
    await database.dispose()


async def test_execution_key_is_idempotent(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    request = launch_request(tmp_path, "same-key", "success")

    first = await supervisor.start(request)
    second = await supervisor.start(request)
    completed = await supervisor.wait(first.id)
    output = await supervisor.read_output(first.id)

    assert second.id == first.id
    assert completed.status is ExecutionStatus.EXITED
    assert output.stdout.data.count(b"stdout:") == 1
    await supervisor.close()
    await database.dispose()


async def test_supervisor_marks_timeout_failed(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(
        launch_request(
            tmp_path,
            "timeout-key",
            "sleep",
            "--seconds",
            "30",
            timeout_seconds=0.05,
        )
    )

    completed = await supervisor.wait(started.id)

    assert completed.status is ExecutionStatus.FAILED
    assert completed.finished_at is not None
    await supervisor.close()
    await database.dispose()


async def test_supervisor_cancels_running_process(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(
        launch_request(tmp_path, "cancel-key", "sleep", "--seconds", "30")
    )

    cancelled = await supervisor.cancel(started.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert cancelled.finished_at is not None
    await supervisor.close()
    await database.dispose()


@pytest.mark.parametrize("status", [ExecutionStatus.CREATED, ExecutionStatus.QUEUED])
async def test_supervisor_cancels_not_yet_started_execution(
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    execution = Execution(
        id=f"not-started-{status.value}",
        execution_key=f"not-started-{status.value}-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "raise AssertionError('must not start')"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"{status.value}.stdout"),
        stderr_path=str(tmp_path / f"{status.value}.stderr"),
        status=status,
    )
    await executions.create_if_absent(execution)

    cancelled = await supervisor.cancel(execution.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    assert persisted.finished_at is not None
    await supervisor.close()
    await database.dispose()


async def test_supervisor_reconciles_lost_execution_when_process_is_absent(
    tmp_path: Path,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id="lost-without-process",
        execution_key="lost-without-process-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "lost-without-process.stdout"),
        stderr_path=str(tmp_path / "lost-without-process.stderr"),
        status=ExecutionStatus.LOST,
        pid=424242,
        process_group_id=424242,
    )
    await executions.create_if_absent(execution)
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=NeverMatchingInspector(),  # type: ignore[arg-type]
    )

    cancelled = await supervisor.cancel(execution.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    await supervisor.close()
    await database.dispose()


async def test_supervisor_terminates_matching_process_when_reconciling_lost_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id="lost-with-matching-process",
        execution_key="lost-with-matching-process-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "lost-with-matching-process.stdout"),
        stderr_path=str(tmp_path / "lost-with-matching-process.stderr"),
        status=ExecutionStatus.LOST,
        pid=424243,
        process_group_id=424244,
    )
    await executions.create_if_absent(execution)
    terminated: list[int | None] = []

    async def record_termination(
        process_group_id: int | None,
        *,
        grace_seconds: float,
    ) -> None:
        terminated.append(process_group_id)

    monkeypatch.setattr(
        "riftx.runner.supervisor._terminate_detached_process",
        record_termination,
    )
    inspector = MatchThenMissingInspector()
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=inspector,  # type: ignore[arg-type]
        termination_grace_seconds=0.01,
    )

    cancelled = await supervisor.cancel(execution.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert terminated == [execution.process_group_id]
    assert inspector.calls == 2
    await supervisor.close()
    await database.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
async def test_detached_cancel_terminates_and_verifies_real_process_group(
    tmp_path: Path,
) -> None:
    database, executions, first_supervisor = await make_supervisor(tmp_path)
    heartbeat = tmp_path / "child-heartbeat"
    started = await first_supervisor.start(
        launch_request(
            tmp_path,
            "detached-group-key",
            "child",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
        )
    )
    await asyncio.to_thread(wait_for_nonempty_file, Path(started.stdout_path))
    await asyncio.to_thread(wait_for_nonempty_file, heartbeat)
    output = await first_supervisor.read_output(started.id)
    child_pid = int(output.stdout.data.decode().strip().splitlines()[0])
    assert started.pid is not None

    await first_supervisor.close()
    detached_supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        termination_grace_seconds=0.1,
    )

    cancelled = await detached_supervisor.cancel(started.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    await asyncio.to_thread(wait_for_process_exit, started.pid)
    await asyncio.to_thread(wait_for_process_exit, child_pid)
    await detached_supervisor.close()
    await database.dispose()


@pytest.mark.parametrize(
    "status",
    [ExecutionStatus.STARTING, ExecutionStatus.RUNNING, ExecutionStatus.LOST],
)
async def test_detached_cancel_does_not_claim_cancelled_when_process_still_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: ExecutionStatus,
) -> None:
    database, executions, _ = await make_supervisor(tmp_path)
    execution = Execution(
        id=f"unconfirmed-{status.value}",
        execution_key=f"unconfirmed-{status.value}-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / f"unconfirmed-{status.value}.stdout"),
        stderr_path=str(tmp_path / f"unconfirmed-{status.value}.stderr"),
        status=status,
        pid=424242,
        process_group_id=424242,
    )
    await executions.create_if_absent(execution)

    async def leave_process_running(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "riftx.runner.supervisor._terminate_detached_process",
        leave_process_running,
    )
    supervisor = ProcessSupervisor(
        executions,
        RunnerPaths(tmp_path / "state"),
        inspector=AlwaysMatchingInspector(),  # type: ignore[arg-type]
        termination_grace_seconds=0.01,
    )

    with pytest.raises(ProcessTerminationError, match=execution.id):
        await supervisor.cancel(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is status
    await supervisor.close()
    await database.dispose()


async def test_large_output_can_be_resumed_without_rereading(tmp_path: Path) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(launch_request(tmp_path, "large-key", "large"))
    await supervisor.wait(started.id)

    cursor = 0
    chunks: list[bytes] = []
    while True:
        output = await supervisor.read_output(started.id, stdout_cursor=cursor, max_bytes=16_384)
        chunks.append(output.stdout.data)
        cursor = output.stdout.next_cursor
        if output.stdout.eof:
            break

    assert cursor == 200_000
    assert sum(map(len, chunks)) == 200_000
    assert set(b"".join(chunks)) == {ord("x")}
    await supervisor.close()
    await database.dispose()


async def test_recovery_marks_unidentifiable_active_execution_lost(tmp_path: Path) -> None:
    database, executions, supervisor = await make_supervisor(tmp_path)
    execution = Execution(
        id="orphaned",
        execution_key="orphaned-key",
        run_id="run-1",
        node_id="local-node",
        executor_type=ExecutorType.PROCESS,
        argv=[sys.executable, "missing.py"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "orphaned.stdout"),
        stderr_path=str(tmp_path / "orphaned.stderr"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    await executions.create_if_absent(execution)

    recovered = await supervisor.recover()

    assert len(recovered) == 1
    assert recovered[0].status is ExecutionStatus.LOST
    persisted = await executions.get("orphaned")
    assert persisted is not None
    assert persisted.status is ExecutionStatus.LOST
    await supervisor.close()
    await database.dispose()


async def test_completed_output_survives_supervisor_and_database_restart(
    tmp_path: Path,
) -> None:
    database, _, supervisor = await make_supervisor(tmp_path)
    started = await supervisor.start(launch_request(tmp_path, "restart-key", "success"))
    completed = await supervisor.wait(started.id)
    await supervisor.close()
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    reopened_supervisor = ProcessSupervisor(
        SQLAlchemyExecutionRepository(reopened.session_factory),
        RunnerPaths(tmp_path / "state"),
    )
    restored = await reopened_supervisor.get(completed.id)
    output = await reopened_supervisor.read_output(completed.id)

    assert restored.status is ExecutionStatus.EXITED
    assert restored.exit_code == 0
    assert b"supervised" in output.stdout.data
    await reopened_supervisor.close()
    await reopened.dispose()
