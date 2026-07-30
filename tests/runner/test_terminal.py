from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    TerminalOwner,
    TerminalSession,
    TerminalStatus,
)
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.runner import RunnerPaths, TerminalLaunchRequest, TerminalSupervisor

_SCRIPT = """\
import os
import shutil
import signal
import sys

def interrupted(*_args):
    print("INTERRUPTED", flush=True)

def resized(*_args):
    size = shutil.get_terminal_size()
    print(f"SIZE:{size.columns}x{size.lines}", flush=True)

signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGWINCH, resized)
print(f"TTY:{os.isatty(0)}", flush=True)
print(f"CONTROLLING:{os.tcgetpgrp(0) == os.getpgrp()}", flush=True)
resized()
print("READY", flush=True)
for line in sys.stdin:
    print("ECHO:" + line.rstrip("\\r\\n"), flush=True)
"""


async def _runtime(
    tmp_path: Path,
) -> tuple[
    Database,
    TerminalSupervisor,
    SQLAlchemyTerminalRepository,
    SQLAlchemyExecutionRepository,
]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Terminal test")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Exercise PTY"),
            workspace_path=str(tmp_path),
        )
    )
    terminals = SQLAlchemyTerminalRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "state"),
        termination_grace_seconds=0.1,
    )
    return database, supervisor, terminals, executions


async def _wait_for_output(
    supervisor: TerminalSupervisor,
    session_id: str,
    expected: str,
    *,
    cursor: int = 0,
) -> tuple[str, int]:
    content = ""
    for _ in range(100):
        output = await supervisor.read(session_id, cursor=cursor)
        if output.data:
            content += output.data.decode(errors="replace")
            cursor = output.next_cursor
            if expected in content:
                return content, cursor
        await asyncio.sleep(0.02)
    raise AssertionError(f"did not observe {expected!r}; output={content!r}")


@pytest.mark.skipif(sys.platform == "win32", reason="Unix PTY implementation")
async def test_terminal_enforces_owner_and_persists_unicode_transcript(tmp_path: Path) -> None:
    database, supervisor, terminals, _ = await _runtime(tmp_path)
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            run_id="run-1",
            node_id="local",
            cwd=tmp_path,
            argv=[sys.executable, "-u", "-c", _SCRIPT],
            owner=TerminalOwner.AGENT,
            cols=100,
            rows=30,
        )
    )
    startup, cursor = await _wait_for_output(supervisor, terminal.id, "READY")
    assert "TTY:True" in startup
    assert "CONTROLLING:True" in startup
    assert "SIZE:100x30" in startup

    with pytest.raises(ApplicationConflictError, match="belongs to 'agent'"):
        await supervisor.write(terminal.id, b"blocked\n", actor=TerminalOwner.USER)

    taken = await supervisor.take_over(terminal.id)
    assert taken.owner is TerminalOwner.USER
    with pytest.raises(ApplicationConflictError, match="belongs to 'user'"):
        await supervisor.write(terminal.id, b"agent-blocked\n", actor=TerminalOwner.AGENT)
    await supervisor.write(terminal.id, "你好 RiftX\n".encode(), actor=TerminalOwner.USER)
    output, cursor = await _wait_for_output(
        supervisor,
        terminal.id,
        "ECHO:你好 RiftX",
        cursor=cursor,
    )
    assert "你好 RiftX" in output

    resized = await supervisor.resize(terminal.id, cols=132, rows=48)
    assert (resized.cols, resized.rows) == (132, 48)
    _, cursor = await _wait_for_output(supervisor, terminal.id, "SIZE:132x48", cursor=cursor)
    await supervisor.interrupt(terminal.id, actor=TerminalOwner.USER)
    _, cursor = await _wait_for_output(supervisor, terminal.id, "INTERRUPTED", cursor=cursor)

    released = await supervisor.release(terminal.id)
    assert released.owner is TerminalOwner.AGENT
    with pytest.raises(ApplicationConflictError, match="belongs to 'agent'"):
        await supervisor.write(terminal.id, b"blocked-again\n", actor=TerminalOwner.USER)

    closed = await supervisor.close(terminal.id)
    assert closed.status is TerminalStatus.CLOSED
    persisted = await terminals.get(terminal.id)
    assert persisted is not None and persisted.closed_at is not None
    transcript = (await supervisor.get_execution(terminal.id)).stdout_path
    transcript_text = await asyncio.to_thread(Path(transcript).read_text, errors="replace")
    assert "ECHO:你好 RiftX" in transcript_text

    await supervisor.close_all()
    await database.dispose()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix PTY implementation")
async def test_shared_read_only_terminal_rejects_all_writers(tmp_path: Path) -> None:
    database, supervisor, _, _ = await _runtime(tmp_path)
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            run_id="run-1",
            node_id="local",
            cwd=tmp_path,
            argv=[sys.executable, "-u", "-c", _SCRIPT],
            owner=TerminalOwner.SHARED_READ_ONLY,
        )
    )
    await _wait_for_output(supervisor, terminal.id, "READY")

    for actor in (TerminalOwner.AGENT, TerminalOwner.USER):
        with pytest.raises(ApplicationConflictError, match="belongs to 'shared_read_only'"):
            await supervisor.write(terminal.id, b"blocked\n", actor=actor)
        with pytest.raises(ApplicationConflictError, match="belongs to 'shared_read_only'"):
            await supervisor.interrupt(terminal.id, actor=actor)

    await supervisor.close(terminal.id)
    await database.dispose()


async def test_recovery_marks_unattached_native_pty_lost(tmp_path: Path) -> None:
    database, supervisor, terminals, executions = await _runtime(tmp_path)
    execution = Execution(
        id="execution-lost",
        execution_key="terminal:lost",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["/bin/sh"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "lost.log"),
        stderr_path=str(tmp_path / "lost.log"),
    )
    await executions.create_if_absent(execution)
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    await executions.save(execution)
    terminal = TerminalSession(
        id="terminal-lost",
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)
    terminal.transition_to(TerminalStatus.OPEN)
    await terminals.save(terminal)

    recovered = await supervisor.recover()

    assert [item.id for item in recovered] == ["terminal-lost"]
    persisted_terminal = await terminals.get("terminal-lost")
    persisted_execution = await executions.get("execution-lost")
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.LOST
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.LOST
    await database.dispose()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix PTY implementation")
async def test_terminal_natural_exit_persists_exit_code_and_closed_state(tmp_path: Path) -> None:
    database, supervisor, terminals, executions = await _runtime(tmp_path)
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            run_id="run-1",
            node_id="local",
            cwd=tmp_path,
            argv=[sys.executable, "-c", "print('DONE', flush=True); raise SystemExit(7)"],
        )
    )
    await _wait_for_output(supervisor, terminal.id, "DONE")

    for _ in range(100):
        persisted = await terminals.get(terminal.id)
        if persisted is not None and persisted.status is TerminalStatus.CLOSED:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("terminal did not persist natural close")

    execution = await executions.get(terminal.execution_id)
    assert execution is not None
    assert execution.status is ExecutionStatus.EXITED
    assert execution.exit_code == 7
    await supervisor.close_all()
    await database.dispose()
