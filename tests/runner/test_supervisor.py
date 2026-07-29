from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from riftx.domain import Engagement, Execution, ExecutionStatus, ExecutorType, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import ExecutionLaunchRequest, ProcessSupervisor, RunnerPaths

FIXTURE = Path(__file__).parent / "fixtures" / "fake_process.py"


def wait_for_nonempty_file(path: Path, deadline_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {path}")


async def make_supervisor(
    tmp_path: Path,
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
    assert first.stdout.data + second.stdout.data == (
        b"stdout: \xe4\xbd\xa0\xe5\xa5\xbd RiftX\nenv: supervised\n"
    )
    assert first.stderr.data + second.stderr.data == b"stderr: diagnostic\n"
    assert second.stdout.eof is True
    assert second.stderr.eof is True
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
