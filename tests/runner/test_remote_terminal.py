from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services.runner_control import (
    ExecutionStatusReport,
    RunnerControlService,
)
from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    RunEvent,
    RunnerCommandKind,
    RunnerPrincipal,
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

from ._containment_support import FakeKernelContainmentManager

_OWNER = RunnerPrincipal(instance_id="runner-instance-windows-a", epoch=1)


class FakeControlService:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, RunnerCommandKind, str, dict[str, object]]] = []

    async def current_principal(self, node_id: str) -> RunnerPrincipal:
        assert node_id == "windows-a"
        return _OWNER

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
        target: RunnerPrincipal | None = None,
    ) -> tuple[object, bool]:
        assert target == _OWNER
        self.enqueued.append((node_id, kind, idempotency_key, payload))
        return object(), True


class FakeTerminalClient:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, ExecutionStatus]] = []
        self.status_details: list[tuple[str, ExecutionStatus, dict[str, object]]] = []
        self.output: dict[str, bytearray] = {}

    @property
    def principal(self) -> RunnerPrincipal:
        return _OWNER

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        **details: object,
    ) -> None:
        self.statuses.append((execution_id, status))
        self.status_details.append((execution_id, status, details))

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

    @property
    def process_group_id(self) -> int:
        return self.pid

    @property
    def containment_identifier(self) -> None:
        return None

    @property
    def activation_pending(self) -> bool:
        return False

    async def activate(self) -> None:
        return None

    async def abort_gated_start(
        self,
        *,
        confirmation_seconds: float = 0.5,
        cleanup_containment: bool = False,
    ) -> bool:
        return False

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        await asyncio.to_thread(_append, self.transcript, b"ECHO:" + data)

    async def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def terminate(
        self,
        _: float,
        *,
        cleanup_containment: bool = False,
    ) -> None:
        self.finish(130)

    async def wait(self, *, cleanup_containment: bool = False) -> int:
        await self._exited.wait()
        return self._exit_code

    async def cleanup_confirmed_containment(self) -> None:
        return None

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
                runner_principal=_OWNER,
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

    close_requested = await router.close("terminal-1")
    assert close_requested.status is TerminalStatus.OPEN
    persisted_execution = await router.get_execution("terminal-1")
    assert persisted_execution.status is ExecutionStatus.RUNNING
    _, kind, idempotency_key, cancel_payload = control.enqueued[-1]
    assert kind is RunnerCommandKind.CANCEL
    assert idempotency_key.startswith("cancel:execution-1:")
    assert cancel_payload == {
        "execution_id": "execution-1",
        "execution_key": "terminal:terminal-1",
    }
    close_events = await events.list_after("run-1")
    assert close_events[-1].event_type == "terminal.close_requested"
    assert close_events[-1].payload["operation_id"] == idempotency_key

    runner_control = RunnerControlService(
        credentials=object(),  # type: ignore[arg-type]
        commands=object(),  # type: ignore[arg-type]
        nodes=object(),  # type: ignore[arg-type]
        executions=executions,
        paths=RunnerPaths(tmp_path / "central-state"),
        registration_token=None,
        terminals=terminals,
        events=events,
    )
    runner_control.authenticate = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(principal=persisted_execution.owner)
    )
    acknowledged = await runner_control.report_execution(
        "windows-a",
        "runner-token",
        "execution-1",
        ExecutionStatusReport(
            status=ExecutionStatus.CANCELLED,
            exit_code=130,
            physical_stop_confirmed=True,
        ),
    )

    assert acknowledged.status is ExecutionStatus.CANCELLED
    assert acknowledged.exit_code == 130
    assert acknowledged.physical_stop_confirmed_at is not None
    persisted_terminal = await router.get("terminal-1")
    assert persisted_terminal.status is TerminalStatus.CLOSED
    acknowledged_events = await events.list_after("run-1")
    assert acknowledged_events[-1].event_type == "terminal.closed"
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_control_does_not_append_opened_after_closed_projection(
    tmp_path: Path,
) -> None:
    """Force the old OPEN recheck -> append TOCTOU and verify DB fencing."""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'projection-race.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-projection-race", name="Projection race")
    )
    run = Run(
        id="run-projection-race",
        engagement_id="engagement-projection-race",
        node_id="windows-a",
        objective=Objective(description="Terminal projection race"),
        workspace_path=str(tmp_path),
    )
    await SQLAlchemyRunRepository(database.session_factory).create(run)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    terminals = SQLAlchemyTerminalRepository(database.session_factory)
    durable_events = SQLAlchemyRunEventRepository(database.session_factory)

    execution = Execution(
        id="execution-projection-race",
        execution_key="terminal:terminal-projection-race",
        run_id=run.id,
        node_id="windows-a",
        owner=_OWNER,
        executor_type=ExecutorType.PTY,
        argv=["pwsh.exe"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "projection-race.log"),
        stderr_path=str(tmp_path / "projection-race.log"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    await executions.create_if_absent(execution)
    await terminals.create(
        TerminalSession(
            id="terminal-projection-race",
            run_id=run.id,
            execution_id=execution.id,
            runner_id="windows-a",
        )
    )

    class _BlockOpenedProjection:
        def __init__(self) -> None:
            self.opened_waiting = asyncio.Event()
            self.release_opened = asyncio.Event()

        async def append_terminal_projection_if_current(
            self,
            run_id: str,
            event_type: str,
            payload: dict[str, object] | None = None,
            *,
            event_id: str,
            session_id: str,
            expected_terminal_status: TerminalStatus,
            expected_execution_status: ExecutionStatus,
        ) -> RunEvent | None:
            if event_type == "terminal.opened":
                self.opened_waiting.set()
                await self.release_opened.wait()
            return await durable_events.append_terminal_projection_if_current(
                run_id,
                event_type,
                payload,
                event_id=event_id,
                session_id=session_id,
                expected_terminal_status=expected_terminal_status,
                expected_execution_status=expected_execution_status,
            )

    projection_events = _BlockOpenedProjection()
    runner_control = RunnerControlService(
        credentials=object(),  # type: ignore[arg-type]
        commands=object(),  # type: ignore[arg-type]
        nodes=object(),  # type: ignore[arg-type]
        executions=executions,
        paths=RunnerPaths(tmp_path / "projection-race-state"),
        registration_token=None,
        terminals=terminals,
        events=projection_events,  # type: ignore[arg-type]
    )
    runner_control.authenticate = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(principal=_OWNER)
    )

    results: dict[str, Execution] = {}

    async def report_running() -> None:
        results["running"] = await runner_control.report_execution(
            "windows-a",
            "runner-token",
            execution.id,
            ExecutionStatusReport(
                status=ExecutionStatus.RUNNING,
                pid=4242,
                process_group_id=4242,
            ),
        )

    async def cancel_after_opened_recheck() -> None:
        await projection_events.opened_waiting.wait()
        try:
            results["cancelled"] = await runner_control.report_execution(
                "windows-a",
                "runner-token",
                execution.id,
                ExecutionStatusReport(
                    status=ExecutionStatus.CANCELLED,
                    physical_stop_confirmed=True,
                ),
            )
        finally:
            projection_events.release_opened.set()

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(report_running())
        tasks.create_task(cancel_after_opened_recheck())

    persisted_execution = await executions.get(execution.id)
    persisted_terminal = await terminals.get("terminal-projection-race")
    events = [
        event
        for event in await durable_events.list_after(run.id)
        if event.payload.get("session_id") == "terminal-projection-race"
    ]
    assert results["running"].status is ExecutionStatus.RUNNING
    assert results["cancelled"].status is ExecutionStatus.CANCELLED
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.CANCELLED
    assert persisted_execution.physical_stop_confirmed_at is not None
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.CLOSED
    assert [event.event_type for event in events] == ["terminal.closed"]
    assert events[0].payload["status"] == "cancelled"
    await database.dispose()


@pytest.mark.asyncio
async def test_remote_terminal_start_accepts_runner_winning_running_status_race(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "race-executions.json")
    terminals = FileTerminalRepository(tmp_path / "race-terminals.json")

    class _RunnerWinsStartControl(FakeControlService):
        async def enqueue(
            self,
            node_id: str,
            *,
            kind: RunnerCommandKind,
            idempotency_key: str,
            payload: dict[str, object],
            target: RunnerPrincipal | None = None,
        ) -> tuple[object, bool]:
            result = await super().enqueue(
                node_id,
                kind=kind,
                idempotency_key=idempotency_key,
                payload=payload,
                target=target,
            )
            if kind is RunnerCommandKind.TERMINAL_START:
                execution_id = str(payload["execution_id"])
                current = await executions.get(execution_id)
                assert current is not None
                current.pid = 4242
                current.process_group_id = 4242
                current.transition_to(ExecutionStatus.RUNNING)
                _, saved = await executions.save_if_status(
                    current,
                    expected={ExecutionStatus.STARTING},
                )
                assert saved is True
                projected = await terminals.get(str(payload["session_id"]))
                assert projected is not None
                projected.transition_to(TerminalStatus.OPEN)
                await terminals.save(projected)
            return result

    control = _RunnerWinsStartControl()
    remote = RemoteTerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        control=control,  # type: ignore[arg-type]
        paths=RunnerPaths(tmp_path / "race-central-state"),
    )

    terminal = await remote.start(
        TerminalLaunchRequest(
            session_id="terminal-runner-wins",
            execution_id="execution-runner-wins",
            run_id="run-1",
            node_id="windows-a",
            runner_principal=_OWNER,
            cwd=tmp_path,
            argv=["pwsh.exe"],
        )
    )

    execution = await executions.get("execution-runner-wins")
    assert terminal.status is TerminalStatus.OPEN
    assert execution is not None and execution.status is ExecutionStatus.RUNNING
    assert [item[1] for item in control.enqueued] == [RunnerCommandKind.TERMINAL_START]


@pytest.mark.asyncio
async def test_remote_terminal_start_does_not_emit_opened_after_closed_cas_wins(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "closed-race-executions.json")

    class _CloseBeforeOpenRepository(FileTerminalRepository):
        injected = False

        async def save_if_status(self, terminal, *, expected):  # type: ignore[no-untyped-def]
            if terminal.status is TerminalStatus.OPEN and not self.injected:
                self.injected = True
                current = await self.get(terminal.id)
                assert current is not None
                closed = current.model_copy(deep=True)
                closed.transition_to(TerminalStatus.CLOSED)
                _, saved = await super().save_if_status(
                    closed,
                    expected={current.status},
                )
                assert saved is True
            return await super().save_if_status(terminal, expected=expected)

    terminals = _CloseBeforeOpenRepository(tmp_path / "closed-race-terminals.json")

    class _RunnerWinsExecutionControl(FakeControlService):
        async def enqueue(
            self,
            node_id: str,
            *,
            kind: RunnerCommandKind,
            idempotency_key: str,
            payload: dict[str, object],
            target: RunnerPrincipal | None = None,
        ) -> tuple[object, bool]:
            result = await super().enqueue(
                node_id,
                kind=kind,
                idempotency_key=idempotency_key,
                payload=payload,
                target=target,
            )
            if kind is RunnerCommandKind.TERMINAL_START:
                current = await executions.get(str(payload["execution_id"]))
                assert current is not None
                current.pid = 4343
                current.process_group_id = 4343
                current.transition_to(ExecutionStatus.RUNNING)
                _, saved = await executions.save_if_status(
                    current,
                    expected={ExecutionStatus.STARTING},
                )
                assert saved is True
            return result

    class _RecordingEvents(NullRunEventRepository):
        def __init__(self) -> None:
            self.event_types: list[str] = []

        async def append(self, run_id, event_type, payload):  # type: ignore[no-untyped-def]
            del run_id, payload
            self.event_types.append(event_type)

    control = _RunnerWinsExecutionControl()
    events = _RecordingEvents()
    remote = RemoteTerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=events,
        control=control,  # type: ignore[arg-type]
        paths=RunnerPaths(tmp_path / "closed-race-central-state"),
    )

    with pytest.raises(ApplicationConflictError, match="closed projection"):
        await remote.start(
            TerminalLaunchRequest(
                session_id="terminal-closed-race",
                execution_id="execution-closed-race",
                run_id="run-1",
                node_id="windows-a",
                runner_principal=_OWNER,
                cwd=tmp_path,
                argv=["pwsh.exe"],
            )
        )

    persisted = await terminals.get("terminal-closed-race")
    assert persisted is not None and persisted.status is TerminalStatus.CLOSED
    assert "terminal.opened" not in events.event_types
    assert events.event_types == ["terminal.close_requested"]
    assert [item[1] for item in control.enqueued] == [
        RunnerCommandKind.TERMINAL_START,
        RunnerCommandKind.CANCEL,
    ]


@pytest.mark.asyncio
async def test_remote_terminal_close_cancels_running_execution_despite_closed_projection(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "closed-executions.json")
    terminals = FileTerminalRepository(tmp_path / "closed-terminals.json")
    control = FakeControlService()
    execution = Execution(
        id="execution-closed-projection",
        execution_key="terminal:terminal-closed-projection",
        run_id="run-1",
        node_id="windows-a",
        owner=_OWNER,
        executor_type=ExecutorType.PTY,
        argv=["pwsh.exe"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "closed-projection.log"),
        stderr_path=str(tmp_path / "closed-projection.log"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.pid = 5252
    execution.process_group_id = 5252
    execution.transition_to(ExecutionStatus.RUNNING)
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id="terminal-closed-projection",
        run_id="run-1",
        execution_id=execution.id,
        runner_id="windows-a",
    )
    terminal.transition_to(TerminalStatus.CLOSED)
    await terminals.create(terminal)
    remote = RemoteTerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        control=control,  # type: ignore[arg-type]
        paths=RunnerPaths(tmp_path / "closed-central-state"),
    )

    returned = await remote.close(terminal.id)

    assert returned.status is TerminalStatus.CLOSED
    assert len(control.enqueued) == 1
    _, kind, idempotency_key, payload = control.enqueued[0]
    assert kind is RunnerCommandKind.CANCEL
    assert idempotency_key.startswith(f"cancel:{execution.id}:")
    assert payload == {
        "execution_id": execution.id,
        "execution_key": execution.execution_key,
    }


@pytest.mark.asyncio
async def test_remote_terminal_close_repairs_open_projection_with_durable_stop_proof(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "proof-executions.json")
    terminals = FileTerminalRepository(tmp_path / "proof-terminals.json")
    control = FakeControlService()
    execution = Execution(
        id="execution-proof-open",
        execution_key="terminal:terminal-proof-open",
        run_id="run-1",
        node_id="windows-a",
        owner=_OWNER,
        executor_type=ExecutorType.PTY,
        argv=["pwsh.exe"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "proof-open.log"),
        stderr_path=str(tmp_path / "proof-open.log"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(ExecutionStatus.EXITED, exit_code=0)
    execution.physical_stop_confirmed_at = datetime(2026, 8, 1, tzinfo=UTC)
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id="terminal-proof-open",
        run_id="run-1",
        execution_id=execution.id,
        runner_id="windows-a",
    )
    terminal.transition_to(TerminalStatus.OPEN)
    await terminals.create(terminal)
    remote = RemoteTerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        control=control,  # type: ignore[arg-type]
        paths=RunnerPaths(tmp_path / "proof-central-state"),
    )

    returned = await remote.close(terminal.id)

    assert returned.status is TerminalStatus.CLOSED
    persisted = await terminals.get(terminal.id)
    assert persisted is not None and persisted.status is TerminalStatus.CLOSED
    assert control.enqueued == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blocked_guard_call", "expected_starts"),
    [(1, 0), (2, 1)],
)
async def test_remote_terminal_manager_propagates_guard_before_and_after_spawn(
    tmp_path: Path,
    blocked_guard_call: int,
    expected_starts: int,
) -> None:
    executions = FileExecutionRepository(tmp_path / "guard-executions.json")
    terminals = FileTerminalRepository(tmp_path / "guard-terminals.json")
    backend = FakeNativeBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "guard-runner-state"),
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
        operation_journal=OperationJournal(tmp_path / "guard-operations.json"),
        output_poll_seconds=0.001,
    )
    request = TerminalLaunchRequest(
        session_id=f"guard-terminal-{blocked_guard_call}",
        execution_id=f"guard-execution-{blocked_guard_call}",
        run_id="run-1",
        node_id="windows-a",
        runner_principal=_OWNER,
        cwd=tmp_path,
        argv=["pwsh.exe"],
    )
    guard_calls = 0

    async def effect_guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == blocked_guard_call:
            raise ApplicationConflictError("terminal_cancelled", "durable tombstone won")

    with pytest.raises(ApplicationConflictError, match="durable tombstone won"):
        await manager.handle(
            RunnerCommandKind.TERMINAL_START,
            {
                "session_id": request.session_id,
                "execution_id": request.execution_id,
                "request": request.model_dump(mode="json"),
            },
            effect_guard=effect_guard,
        )

    execution = await executions.get(str(request.execution_id))
    terminal = await terminals.get(str(request.session_id))
    assert guard_calls == blocked_guard_call
    assert backend.starts == expected_starts
    assert execution is not None and execution.status is ExecutionStatus.CANCELLED
    assert terminal is not None and terminal.status is TerminalStatus.CLOSED
    assert client.statuses[-1] == (execution.id, ExecutionStatus.CANCELLED)
    assert client.status_details[-1][2]["physical_stop_confirmed"] is True
    await manager.close()


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
        runner_principal=_OWNER,
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
    exited_details = next(
        details
        for execution_id, status, details in client.status_details
        if execution_id == "execution-1" and status is ExecutionStatus.EXITED
    )
    assert exited_details["physical_stop_confirmed"] is True
    await _wait_for(
        lambda: b"ECHO:Get-Location" in bytes(client.output.get("execution-1", b"")),
    )
    terminal = await terminals.get("terminal-1")
    assert terminal is not None and terminal.status is TerminalStatus.CLOSED
    # A CANCEL command may arrive after the natural status upload was lost.
    # The durable local proof must still produce a stop ACK without rewriting
    # the actual EXITED outcome to CANCELLED.
    recovered = await manager.cancel_execution("execution-1")
    assert recovered.status is ExecutionStatus.EXITED
    assert recovered.physical_stop_confirmed_at is not None
    await manager.close()


@pytest.mark.asyncio
async def test_remote_terminal_manager_cancels_execution_through_native_supervisor(
    tmp_path: Path,
) -> None:
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
        session_id="terminal-cancel",
        execution_id="execution-cancel",
        run_id="run-1",
        node_id="windows-a",
        runner_principal=_OWNER,
        cwd=tmp_path,
        argv=["pwsh.exe"],
    )
    await manager.handle(
        RunnerCommandKind.TERMINAL_START,
        {
            "session_id": request.session_id,
            "execution_id": request.execution_id,
            "request": request.model_dump(mode="json"),
        },
    )

    cancelled = await manager.cancel_execution("execution-cancel")

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert cancelled.exit_code == 130
    terminal = await terminals.get("terminal-cancel")
    assert terminal is not None and terminal.status is TerminalStatus.CLOSED
    assert backend.handle is not None
    await _wait_for(
        lambda: ("execution-cancel", ExecutionStatus.CANCELLED) in client.statuses,
    )
    await manager.close()


@pytest.mark.asyncio
async def test_pty_durable_stop_row_blocks_same_key_spawn_after_runner_restart(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "restart-executions.json")
    terminals = FileTerminalRepository(tmp_path / "restart-terminals.json")
    paths = RunnerPaths(tmp_path / "restart-runner-state")
    first_backend = FakeNativeBackend()
    first_supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=paths,
        native_backend=first_backend,
        platform_name="nt",
        termination_grace_seconds=0.01,
    )
    request = TerminalLaunchRequest(
        session_id="terminal-row-restart",
        execution_id="execution-row-restart",
        run_id="run-1",
        node_id="windows-a",
        runner_principal=_OWNER,
        cwd=tmp_path,
        argv=["pwsh.exe"],
    )
    payload = {
        "session_id": request.session_id,
        "execution_id": request.execution_id,
        "request": request.model_dump(mode="json"),
    }
    first_manager = RemoteTerminalManager(
        node_id="windows-a",
        supervisor=first_supervisor,
        terminals=terminals,
        executions=executions,
        client=FakeTerminalClient(),
        operation_journal=OperationJournal(tmp_path / "restart-operations-1.json"),
        output_poll_seconds=0.001,
    )
    await first_manager.handle(RunnerCommandKind.TERMINAL_START, payload)
    stopped = await first_manager.cancel_execution(str(request.execution_id))
    assert stopped.status is ExecutionStatus.CANCELLED
    assert stopped.physical_stop_confirmed_at is not None
    await first_manager.close()

    reopened_backend = FakeNativeBackend()
    reopened_manager = RemoteTerminalManager(
        node_id="windows-a",
        supervisor=TerminalSupervisor(
            terminal_repository=FileTerminalRepository(terminals.path),
            execution_repository=FileExecutionRepository(executions.path),
            event_repository=NullRunEventRepository(),
            paths=paths,
            native_backend=reopened_backend,
            platform_name="nt",
            termination_grace_seconds=0.01,
        ),
        terminals=FileTerminalRepository(terminals.path),
        executions=FileExecutionRepository(executions.path),
        client=FakeTerminalClient(),
        operation_journal=OperationJournal(tmp_path / "restart-operations-2.json"),
        output_poll_seconds=0.001,
    )
    try:
        duplicate = await reopened_manager.handle(
            RunnerCommandKind.TERMINAL_START,
            payload,
        )

        assert duplicate["duplicate"] is True  # type: ignore[index]
        assert reopened_backend.starts == 0
        durable = await FileExecutionRepository(executions.path).get(
            str(request.execution_id)
        )
        assert durable is not None
        assert durable.status is ExecutionStatus.CANCELLED
        assert durable.physical_stop_confirmed_at is not None
    finally:
        await reopened_manager.close()


@pytest.mark.asyncio
async def test_remote_terminal_manager_cancels_from_execution_containment_when_row_missing(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    containment_manager = FakeKernelContainmentManager(tmp_path / "containment")
    containment = await containment_manager.prepare("terminal:terminal-state-lost")
    execution = Execution(
        id="execution-state-lost",
        execution_key="terminal:terminal-state-lost",
        run_id="run-1",
        node_id="windows-a",
        owner=_OWNER,
        executor_type=ExecutorType.PTY,
        argv=["shell"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "state-lost.log"),
        stderr_path=str(tmp_path / "state-lost.log"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.pid = 424242
    execution.process_group_id = 424242
    execution.containment_id = containment.identifier
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(ExecutionStatus.LOST)
    await executions.create_if_absent(execution)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "runner-state"),
        containment_manager=containment_manager,
        platform_name="posix",
        autodetect_containment=False,
    )
    manager = RemoteTerminalManager(
        node_id="windows-a",
        supervisor=supervisor,
        terminals=terminals,
        executions=executions,
        client=FakeTerminalClient(),
        operation_journal=OperationJournal(tmp_path / "operations.json"),
        output_poll_seconds=0.001,
    )

    cancelled = await manager.cancel_execution(execution.id)

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert await terminals.get_by_execution(execution.id) is None
    assert not containment.path.exists()
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
            owner=_OWNER,
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


@pytest.mark.asyncio
async def test_remote_terminal_shutdown_attempts_every_owned_terminal_after_failure(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "shutdown-executions.json")
    terminals = FileTerminalRepository(tmp_path / "shutdown-terminals.json")
    for index in (1, 2):
        transcript = tmp_path / f"shutdown-{index}.log"
        transcript.touch()
        execution = Execution(
            id=f"shutdown-execution-{index}",
            execution_key=f"terminal:shutdown-terminal-{index}",
            run_id="run-1",
            node_id="windows-a",
            owner=_OWNER,
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
            id=f"shutdown-terminal-{index}",
            run_id="run-1",
            execution_id=execution.id,
        )
        await terminals.create(terminal)
        terminal.transition_to(TerminalStatus.OPEN)
        await terminals.save(terminal)

    second_entered = asyncio.Event()

    class _PartiallyFailingSupervisor:
        def __init__(self) -> None:
            self.close_calls: list[str] = []

        async def close(self, session_id: str) -> TerminalSession:
            self.close_calls.append(session_id)
            if session_id == "shutdown-terminal-1":
                await second_entered.wait()
                raise RuntimeError("first terminal handle was lost")
            second_entered.set()
            terminal = await terminals.get(session_id)
            assert terminal is not None
            return terminal

    supervisor = _PartiallyFailingSupervisor()
    manager = RemoteTerminalManager(
        node_id="windows-a",
        supervisor=supervisor,  # type: ignore[arg-type]
        terminals=terminals,
        executions=executions,
        client=FakeTerminalClient(),
        operation_journal=OperationJournal(tmp_path / "shutdown-operations.json"),
        output_poll_seconds=0.001,
    )

    with pytest.raises(RuntimeError, match="first terminal handle was lost"):
        await asyncio.wait_for(manager.close(), timeout=1)

    assert set(supervisor.close_calls) == {
        "shutdown-terminal-1",
        "shutdown-terminal-2",
    }


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
