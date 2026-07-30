from __future__ import annotations

import asyncio
import base64
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
    RunnerCommandKind,
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
from riftx.runner.remote_terminal import NodeTerminalRouter, RemoteTerminalSupervisor
from riftx.runner.state import FileExecutionRepository, FileTerminalRepository
from riftx.runner.terminal_manager import (
    NullRunEventRepository,
    OperationJournal,
    RemoteTerminalManager,
)


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


class FakeTerminalClient:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, ExecutionStatus]] = []
        self.output: dict[str, bytearray] = {}

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        **_: object,
    ) -> None:
        self.statuses.append((execution_id, status))

    async def report_output(
        self,
        execution_id: str,
        *,
        stream: str,
        offset: int,
        data: bytes,
    ) -> int:
        assert stream == "stdout"
        target = self.output.setdefault(execution_id, bytearray())
        assert len(target) == offset
        target.extend(data)
        return len(target)


class FakeNativeHandle:
    def __init__(self, transcript: Path) -> None:
        self.pid = 4242
        self.transcript = transcript
        self.writes: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.interrupts = 0
        self._exit_code = 0
        self._exited = asyncio.Event()

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        await asyncio.to_thread(_append, self.transcript, b"ECHO:" + data)

    async def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def terminate(self, _: float) -> None:
        self.finish(130)

    async def wait(self) -> int:
        await self._exited.wait()
        return self._exit_code

    async def close_output(self) -> None:
        return None

    def finish(self, exit_code: int = 0) -> None:
        self._exit_code = exit_code
        self._exited.set()


class FakeNativeBackend:
    def __init__(self) -> None:
        self.starts = 0
        self.handle: FakeNativeHandle | None = None

    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        transcript_path: Path,
        environment: dict[str, str],
    ) -> FakeNativeHandle:
        self.starts += 1
        self.handle = FakeNativeHandle(transcript_path)
        await asyncio.to_thread(_append, transcript_path, b"READY\n")
        return self.handle


@pytest.mark.asyncio
async def test_remote_terminal_dispatch_preserves_ids_ownership_and_operations(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'central.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Remote terminal")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="windows-a",
            objective=Objective(description="Remote ConPTY"),
            workspace_path=str(tmp_path),
        )
    )
    terminals = SQLAlchemyTerminalRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    control = FakeControlService()
    remote = RemoteTerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=events,
        control=control,  # type: ignore[arg-type]
        paths=RunnerPaths(tmp_path / "central-state"),
    )
    router = NodeTerminalRouter(
        local_node_id="local",
        terminal_repository=terminals,
        execution_repository=executions,
        local=remote,
        remote=remote,
    )

    terminal = await router.start(
        TerminalLaunchRequest(
            session_id="terminal-1",
            execution_id="execution-1",
            run_id="run-1",
            node_id="windows-a",
            cwd=tmp_path,
            argv=["pwsh.exe"],
        )
    )
    assert terminal.status is TerminalStatus.OPEN
    assert control.enqueued[0][1] is RunnerCommandKind.TERMINAL_START
    start_payload = control.enqueued[0][3]
    assert start_payload["session_id"] == "terminal-1"
    assert start_payload["execution_id"] == "execution-1"
    assert start_payload["request"]["session_id"] == "terminal-1"  # type: ignore[index]

    with pytest.raises(ApplicationConflictError, match="belongs to 'agent'"):
        await router.write("terminal-1", b"blocked", actor=TerminalOwner.USER)
    await router.take_over("terminal-1")
    await router.write("terminal-1", b"Get-Location\r\n", actor=TerminalOwner.USER)
    await router.resize("terminal-1", cols=160, rows=50)
    await router.interrupt("terminal-1", actor=TerminalOwner.USER)
    assert [item[1] for item in control.enqueued[1:]] == [
        RunnerCommandKind.TERMINAL_WRITE,
        RunnerCommandKind.TERMINAL_RESIZE,
        RunnerCommandKind.TERMINAL_INTERRUPT,
    ]
    for _, _, idempotency_key, payload in control.enqueued[1:]:
        assert payload["operation_id"] == idempotency_key
        assert payload["execution_id"] == "execution-1"

    closed = await router.close("terminal-1")
    assert closed.status is TerminalStatus.CLOSED
    assert control.enqueued[-1][1] is RunnerCommandKind.TERMINAL_CLOSE
    await database.dispose()


@pytest.mark.asyncio
async def test_remote_terminal_manager_streams_and_deduplicates_commands(tmp_path: Path) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    backend = FakeNativeBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "runner-state"),
        native_backend=backend,
        platform_name="nt",
        termination_grace_seconds=0.01,
    )
    client = FakeTerminalClient()
    manager = RemoteTerminalManager(
        node_id="windows-a",
        supervisor=supervisor,
        terminals=terminals,
        executions=executions,
        client=client,
        operation_journal=OperationJournal(tmp_path / "operations.json"),
        output_poll_seconds=0.001,
    )
    request = TerminalLaunchRequest(
        session_id="terminal-1",
        execution_id="execution-1",
        run_id="run-1",
        node_id="windows-a",
        cwd=tmp_path,
        argv=["pwsh.exe"],
    )
    payload = {
        "session_id": "terminal-1",
        "execution_id": "execution-1",
        "request": request.model_dump(mode="json"),
    }
    await manager.handle(RunnerCommandKind.TERMINAL_START, payload)
    duplicate = await manager.handle(RunnerCommandKind.TERMINAL_START, payload)
    assert duplicate["duplicate"] is True  # type: ignore[index]
    assert backend.starts == 1
    await _wait_for(lambda: bytes(client.output.get("execution-1", b"")) == b"READY\n")

    write_payload = {
        "session_id": "terminal-1",
        "execution_id": "execution-1",
        "operation_id": "write-1",
        "data": base64.b64encode(b"Get-Location\r\n").decode(),
    }
    await manager.handle(RunnerCommandKind.TERMINAL_WRITE, write_payload)
    replay = await manager.handle(RunnerCommandKind.TERMINAL_WRITE, write_payload)
    assert replay["duplicate"] is True  # type: ignore[index]
    assert await OperationJournal(tmp_path / "operations.json").contains("write-1")
    assert backend.handle is not None
    assert backend.handle.writes == [b"Get-Location\r\n"]

    await manager.handle(
        RunnerCommandKind.TERMINAL_RESIZE,
        {
            "session_id": "terminal-1",
            "execution_id": "execution-1",
            "operation_id": "resize-1",
            "cols": 132,
            "rows": 48,
        },
    )
    await manager.handle(
        RunnerCommandKind.TERMINAL_INTERRUPT,
        {
            "session_id": "terminal-1",
            "execution_id": "execution-1",
            "operation_id": "interrupt-1",
        },
    )
    assert backend.handle.sizes == [(132, 48)]
    assert backend.handle.interrupts == 1

    backend.handle.finish(0)
    await _wait_for(
        lambda: ("execution-1", ExecutionStatus.EXITED) in client.statuses,
    )
    await _wait_for(
        lambda: b"ECHO:Get-Location" in bytes(client.output.get("execution-1", b"")),
    )
    terminal = await terminals.get("terminal-1")
    assert terminal is not None and terminal.status is TerminalStatus.CLOSED
    await manager.close()


@pytest.mark.asyncio
async def test_remote_terminal_manager_marks_unattachable_sessions_lost(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    transcript = tmp_path / "transcript.log"
    transcript.touch()
    execution = Execution(
        id="execution-lost",
        execution_key="terminal:terminal-lost",
        run_id="run-1",
        node_id="windows-a",
        executor_type=ExecutorType.PTY,
        argv=["pwsh.exe"],
        cwd=str(tmp_path),
        stdout_path=str(transcript),
        stderr_path=str(transcript),
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

    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "runner-state"),
        native_backend=FakeNativeBackend(),
        platform_name="nt",
    )
    client = FakeTerminalClient()
    manager = RemoteTerminalManager(
        node_id="windows-a",
        supervisor=supervisor,
        terminals=terminals,
        executions=executions,
        client=client,
        operation_journal=OperationJournal(tmp_path / "operations.json"),
        output_poll_seconds=0.001,
    )
    await manager.resume_active()

    restored_terminal = await terminals.get("terminal-lost")
    restored_execution = await executions.get("execution-lost")
    assert restored_terminal is not None and restored_terminal.status is TerminalStatus.LOST
    assert restored_execution is not None and restored_execution.status is ExecutionStatus.LOST
    assert client.statuses[-1] == ("execution-lost", ExecutionStatus.LOST)
    await manager.close()


async def _wait_for(predicate: object) -> None:
    for _ in range(200):
        if callable(predicate) and predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(data)


@pytest.mark.asyncio
async def test_local_terminal_recovery_does_not_mark_remote_sessions_lost(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "recover-executions.json")
    terminals = FileTerminalRepository(tmp_path / "recover-terminals.json")
    for suffix, node_id in (("local", "local"), ("remote", "windows-a")):
        transcript = tmp_path / f"{suffix}.log"
        transcript.touch()
        execution = Execution(
            id=f"execution-{suffix}",
            execution_key=f"terminal:terminal-{suffix}",
            run_id="run-1",
            node_id=node_id,
            executor_type=ExecutorType.PTY,
            argv=["shell"],
            cwd=str(tmp_path),
            stdout_path=str(transcript),
            stderr_path=str(transcript),
        )
        await executions.create_if_absent(execution)
        execution.transition_to(ExecutionStatus.STARTING)
        execution.transition_to(ExecutionStatus.RUNNING)
        await executions.save(execution)
        terminal = TerminalSession(
            id=f"terminal-{suffix}",
            run_id="run-1",
            execution_id=execution.id,
        )
        await terminals.create(terminal)
        terminal.transition_to(TerminalStatus.OPEN)
        await terminals.save(terminal)

    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "recover-state"),
        native_backend=FakeNativeBackend(),
    )
    recovered = await supervisor.recover(node_id="local")

    assert [item.id for item in recovered] == ["terminal-local"]
    local = await terminals.get("terminal-local")
    remote = await terminals.get("terminal-remote")
    assert local is not None and local.status is TerminalStatus.LOST
    assert remote is not None and remote.status is TerminalStatus.OPEN
