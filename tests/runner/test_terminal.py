from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

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
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.runner import RunnerPaths, TerminalLaunchRequest, TerminalSupervisor
from riftx.runner.supervisor import ProcessTerminationError
from riftx.runtime.types import AgentSession

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

_STUBBORN_GROUP_CHILD = """\
import os
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
Path(sys.argv[1]).write_text(f"{os.getpid()}:{os.getpgrp()}")
print("STUBBORN_CHILD_READY", flush=True)
while True:
    time.sleep(1)
"""

_STUBBORN_GROUP_LEADER = """\
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
subprocess.Popen([sys.executable, "-u", "-c", sys.argv[1], sys.argv[2]])
while True:
    time.sleep(1)
"""


class RecordingTerminalHandle:
    def __init__(self, pid: int = 424242) -> None:
        self._pid = pid
        self.terminated = asyncio.Event()
        self.output_closed = asyncio.Event()
        self.containment_cleaned = asyncio.Event()

    @property
    def pid(self) -> int:
        return self._pid

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
        return None

    async def resize(self, cols: int, rows: int) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def terminate(
        self,
        grace_seconds: float,
        *,
        cleanup_containment: bool = False,
    ) -> None:
        self.terminated.set()

    async def wait(self, *, cleanup_containment: bool = False) -> int:
        await self.terminated.wait()
        return 0

    async def cleanup_confirmed_containment(self) -> None:
        self.containment_cleaned.set()

    async def close_output(self) -> None:
        self.output_closed.set()


class FailingTerminationTerminalHandle(RecordingTerminalHandle):
    def __init__(self, pid: int = 424242) -> None:
        super().__init__(pid)
        self.fail_termination = True

    async def terminate(
        self,
        grace_seconds: float,
        *,
        cleanup_containment: bool = False,
    ) -> None:
        if self.fail_termination:
            raise RuntimeError("native terminal termination failed")
        await super().terminate(
            grace_seconds,
            cleanup_containment=cleanup_containment,
        )


class LeaderExitedFailingTerminationTerminalHandle(FailingTerminationTerminalHandle):
    async def terminate(
        self,
        grace_seconds: float,
        *,
        cleanup_containment: bool = False,
    ) -> None:
        # Model a leader that exits immediately while tree/group termination
        # confirmation fails. The monitor must not translate this into CANCELLED.
        self.terminated.set()
        if self.fail_termination:
            raise RuntimeError("native terminal tree confirmation failed")


class ContainedRecordingTerminalHandle(RecordingTerminalHandle):
    def __init__(self, pid: int = 424242) -> None:
        super().__init__(pid)
        self.boundary_exists = True

    @property
    def containment_identifier(self) -> str:
        return "recording-terminal-containment"

    async def cleanup_confirmed_containment(self) -> None:
        self.boundary_exists = False
        await super().cleanup_confirmed_containment()


class RecordingTerminalBackend:
    def __init__(self, handle: RecordingTerminalHandle | None = None) -> None:
        self.calls = 0
        self.handle = handle or RecordingTerminalHandle()

    async def start(self, request, *, transcript_path, environment):
        self.calls += 1
        return self.handle


class MissingConfirmedTerminalContainment:
    def __init__(self) -> None:
        self.cleanup_calls = 0

    @property
    def identifier(self) -> str:
        raise AssertionError("missing confirmed containment must not be re-identified")

    def boundary_exists(self) -> bool:
        return False

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        raise AssertionError("missing confirmed containment must not be cleaned again")


class MissingConfirmedTerminalContainmentManager:
    def __init__(self, containment: MissingConfirmedTerminalContainment) -> None:
        self.containment = containment

    def containment_for(self, execution_key: str) -> MissingConfirmedTerminalContainment:
        del execution_key
        return self.containment


class RecordingDetachedTerminalContainment:
    def __init__(self, identifier: str) -> None:
        self._identifier = identifier
        self._boundary_exists = True
        self.terminate_calls = 0
        self.cleanup_calls = 0

    @property
    def identifier(self) -> str:
        return self._identifier

    def boundary_exists(self) -> bool:
        return self._boundary_exists

    async def terminate(self, *, grace_seconds: float) -> None:
        del grace_seconds
        self.terminate_calls += 1

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        self._boundary_exists = False


class RecordingDetachedTerminalContainmentManager:
    def __init__(self, containment: RecordingDetachedTerminalContainment) -> None:
        self.containment = containment

    def containment_for(self, execution_key: str) -> RecordingDetachedTerminalContainment:
        del execution_key
        return self.containment


class BlockingStartTerminalBackend(RecordingTerminalBackend):
    def __init__(self, handle: RecordingTerminalHandle | None = None) -> None:
        super().__init__(handle)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self, request, *, transcript_path, environment):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return self.handle


class BlockFirstTerminalExecutionSave:
    def __init__(
        self,
        delegate: SQLAlchemyExecutionRepository,
        *,
        block_created_claim: bool = False,
    ) -> None:
        self._delegate = delegate
        self._block_created_claim = block_created_claim
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._block_next = True

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def save_if_status(self, execution: Execution, *, expected):
        should_block = self._block_created_claim or execution.pid is not None
        if self._block_next and should_block:
            self._block_next = False
            self.entered.set()
            await self.release.wait()
        return await self._delegate.save_if_status(execution, expected=expected)


class FailOnceTerminalCreateRepository:
    def __init__(
        self,
        delegate: SQLAlchemyTerminalRepository,
        *,
        after_commit: bool = False,
    ) -> None:
        self._delegate = delegate
        self._after_commit = after_commit
        self._failed = False

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def create(self, terminal: TerminalSession) -> TerminalSession:
        if self._failed:
            return await self._delegate.create(terminal)
        self._failed = True
        if self._after_commit:
            await self._delegate.create(terminal)
        raise RuntimeError("injected terminal projection create failure")


class BarrierTerminalCreateRepository:
    def __init__(self, delegate: SQLAlchemyTerminalRepository, parties: int = 2) -> None:
        self._delegate = delegate
        self._parties = parties
        self._calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def create(self, terminal: TerminalSession) -> TerminalSession:
        self._calls += 1
        if self._calls >= self._parties:
            self.entered.set()
        await self.release.wait()
        return await self._delegate.create(terminal)


class RaceTerminalCancellationRepository:
    """Inject RUNNING -> FAILED exactly before the monitor's cancellation CAS."""

    def __init__(self, delegate: SQLAlchemyExecutionRepository) -> None:
        self._delegate = delegate
        self.race_injected = False

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def save(self, execution: Execution) -> Execution:
        if execution.status in {ExecutionStatus.CANCELLED, ExecutionStatus.EXITED}:
            raise AssertionError("terminal monitor must use save_if_status for final state")
        return await self._delegate.save(execution)

    async def save_if_status(self, execution: Execution, *, expected):
        if execution.status is ExecutionStatus.CANCELLED and not self.race_injected:
            current = await self._delegate.get(execution.id)
            assert current is not None
            assert current.status is ExecutionStatus.RUNNING
            current.transition_to(ExecutionStatus.FAILED)
            await self._delegate.save(current)
            self.race_injected = True
        return await self._delegate.save_if_status(execution, expected=expected)


class RaceTerminalRecoveryRepository:
    """Inject a confirmed cancellation before recovery can persist LOST."""

    def __init__(self, delegate: SQLAlchemyExecutionRepository) -> None:
        self._delegate = delegate
        self.race_injected = False

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def save(self, execution: Execution) -> Execution:
        if execution.status is ExecutionStatus.LOST:
            raise AssertionError("terminal recovery must use save_if_status for LOST")
        return await self._delegate.save(execution)

    async def save_if_status(self, execution: Execution, *, expected):
        if execution.status is ExecutionStatus.LOST and not self.race_injected:
            current = await self._delegate.get(execution.id)
            assert current is not None
            assert current.status in {
                ExecutionStatus.STARTING,
                ExecutionStatus.RUNNING,
            }
            current.transition_to(ExecutionStatus.CANCELLED)
            await self._delegate.save(current)
            self.race_injected = True
        return await self._delegate.save_if_status(execution, expected=expected)


class RejectTerminalPhysicalStopProofSave:
    def __init__(self, delegate: SQLAlchemyExecutionRepository) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def save_if_status(self, execution: Execution, *, expected):
        if execution.physical_stop_confirmed_at is not None:
            raise RuntimeError("injected terminal stop-proof persistence failure")
        return await self._delegate.save_if_status(execution, expected=expected)


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
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="agent-session-1", run_id="run-1", model_profile="test")
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


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_terminal_launch_request_rejects_partial_identity_pairs(tmp_path: Path) -> None:
    base: dict[str, object] = {
        "run_id": "run-1",
        "node_id": "local",
        "cwd": tmp_path,
        "argv": ["fake-shell"],
    }
    invalid = (
        {"session_id": "terminal-only"},
        {"execution_id": "execution-only"},
        {
            "tool_call_id": "tool-call-1",
            "execution_key": "tool-key",
            "attempt_group": "initial",
        },
        {
            "tool_call_id": "tool-call-1",
            "agent_session_id": "agent-session-1",
            "attempt_group": "initial",
        },
        {
            "tool_call_id": "tool-call-1",
            "agent_session_id": "agent-session-1",
            "execution_key": "tool-key",
        },
        {"attempt_group": "initial"},
    )
    for partial in invalid:
        with pytest.raises(ValidationError):
            TerminalLaunchRequest(**base, **partial)

    standalone = TerminalLaunchRequest(**base, execution_key="standalone-key")
    tool_bound = TerminalLaunchRequest(
        **base,
        session_id="terminal-1",
        execution_id="execution-1",
        execution_key="tool-key",
        agent_session_id="agent-session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
    )
    assert standalone.tool_call_id is None and standalone.attempt_group is None
    assert tool_bound.agent_session_id == "agent-session-1"


async def test_terminal_projection_create_failure_persists_predispatch_stop_proof(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=FailOnceTerminalCreateRepository(terminals),  # type: ignore[arg-type]
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "projection-create-failure-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )
    request = TerminalLaunchRequest(
        session_id="projection-create-failure",
        execution_id="projection-create-failure-execution",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
    )

    with pytest.raises(RuntimeError, match="projection create failure"):
        await supervisor.start(request)

    execution = await executions.get(str(request.execution_id))
    assert execution is not None
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.physical_stop_confirmed_at is not None
    assert await terminals.get(str(request.session_id)) is None
    assert backend.calls == 0
    await database.dispose()


async def test_terminal_projection_post_commit_failure_closes_without_dispatch(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=FailOnceTerminalCreateRepository(
            terminals,
            after_commit=True,
        ),  # type: ignore[arg-type]
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "projection-post-commit-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )
    request = TerminalLaunchRequest(
        session_id="projection-post-commit",
        execution_id="projection-post-commit-execution",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
    )

    with pytest.raises(RuntimeError, match="projection create failure"):
        await supervisor.start(request)

    execution = await executions.get(str(request.execution_id))
    terminal = await terminals.get(str(request.session_id))
    assert execution is not None
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.physical_stop_confirmed_at is not None
    assert terminal is not None and terminal.status is TerminalStatus.CLOSED
    assert backend.calls == 0
    await database.dispose()


async def test_terminal_exact_retry_closes_legacy_starting_without_projection(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    request = TerminalLaunchRequest(
        session_id="legacy-orphan-terminal",
        execution_id="legacy-orphan-execution",
        execution_key="legacy-orphan-key",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
    )
    orphan = Execution(
        id=str(request.execution_id),
        execution_key=str(request.execution_key),
        launch_fingerprint=request.launch_fingerprint,
        run_id=request.run_id,
        node_id=request.node_id,
        owner=request.runner_principal,
        executor_type=ExecutorType.PTY,
        argv=request.argv,
        cwd=str(request.cwd),
        env_diff=request.env,
        status=ExecutionStatus.STARTING,
        stdout_path=str(tmp_path / "legacy-orphan.log"),
        stderr_path=str(tmp_path / "legacy-orphan.log"),
    )
    await executions.create_if_absent(orphan)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "legacy-orphan-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )

    repaired = await supervisor.start(request)

    execution = await executions.get(orphan.id)
    assert repaired.id == request.session_id
    assert repaired.execution_id == orphan.id
    assert repaired.status is TerminalStatus.CLOSED
    assert execution is not None
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.physical_stop_confirmed_at is not None
    assert backend.calls == 0
    await database.dispose()


async def test_terminal_exact_retry_adopts_created_projection_and_dispatches_once(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    request = TerminalLaunchRequest(
        session_id="created-projection-terminal",
        execution_id="created-projection-execution",
        execution_key="created-projection-key",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
    )
    reserved = Execution(
        id=str(request.execution_id),
        execution_key=str(request.execution_key),
        launch_fingerprint=request.launch_fingerprint,
        run_id=request.run_id,
        node_id=request.node_id,
        executor_type=ExecutorType.PTY,
        argv=request.argv,
        cwd=str(request.cwd),
        env_diff=request.env,
        status=ExecutionStatus.CREATED,
        stdout_path=str(tmp_path / "created-projection.log"),
        stderr_path=str(tmp_path / "created-projection.log"),
    )
    await executions.create_if_absent(reserved)
    await terminals.create(
        TerminalSession(
            id=str(request.session_id),
            run_id=request.run_id,
            execution_id=reserved.id,
            runner_id=request.node_id,
            shell=request.argv[0],
            cwd=str(request.cwd),
            owner=request.owner,
            cols=request.cols,
            rows=request.rows,
        )
    )
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "created-projection-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )

    opened = await supervisor.start(request)

    execution = await executions.get(reserved.id)
    assert opened.id == request.session_id
    assert opened.status is TerminalStatus.OPEN
    assert execution is not None and execution.status is ExecutionStatus.RUNNING
    assert execution.physical_stop_confirmed_at is None
    assert backend.calls == 1
    await supervisor.close(opened.id)
    await database.dispose()


async def test_terminal_missing_projection_rejects_immutable_launch_mismatch(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    request = TerminalLaunchRequest(
        session_id="missing-identity-terminal",
        execution_id="missing-identity-execution",
        execution_key="missing-identity-key",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
    )
    reserved = Execution(
        id=str(request.execution_id),
        execution_key=str(request.execution_key),
        launch_fingerprint=request.launch_fingerprint,
        run_id=request.run_id,
        node_id=request.node_id,
        executor_type=ExecutorType.PTY,
        argv=request.argv,
        cwd=str(request.cwd),
        env_diff=request.env,
        status=ExecutionStatus.CREATED,
        stdout_path=str(tmp_path / "missing-identity.log"),
        stderr_path=str(tmp_path / "missing-identity.log"),
    )
    await executions.create_if_absent(reserved)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "missing-identity-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await supervisor.start(request.model_copy(update={"cols": request.cols + 1}))

    persisted = await executions.get(reserved.id)
    assert captured.value.code == "execution_idempotency_conflict"
    assert persisted is not None and persisted.status is ExecutionStatus.CREATED
    assert persisted.physical_stop_confirmed_at is None
    assert await terminals.get(str(request.session_id)) is None
    assert backend.calls == 0
    await database.dispose()


async def test_terminal_created_without_fingerprint_cancels_without_dispatch(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    request = TerminalLaunchRequest(
        session_id="legacy-created-terminal",
        execution_id="legacy-created-execution",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
    )
    reserved = Execution(
        id=str(request.execution_id),
        execution_key=f"terminal:{request.session_id}",
        launch_fingerprint=None,
        run_id=request.run_id,
        node_id=request.node_id,
        executor_type=ExecutorType.PTY,
        argv=request.argv,
        cwd=str(request.cwd),
        env_diff=request.env,
        status=ExecutionStatus.CREATED,
        stdout_path=str(tmp_path / "legacy-created.log"),
        stderr_path=str(tmp_path / "legacy-created.log"),
    )
    await executions.create_if_absent(reserved)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "legacy-created-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await supervisor.start(request)

    persisted = await executions.get(reserved.id)
    assert captured.value.code == "execution_idempotency_conflict"
    assert persisted is not None and persisted.status is ExecutionStatus.CANCELLED
    assert persisted.physical_stop_confirmed_at is not None
    assert await terminals.get(str(request.session_id)) is None
    assert backend.calls == 0
    await database.dispose()


async def test_terminal_concurrent_exact_admission_dispatches_once(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    barrier = BarrierTerminalCreateRepository(terminals)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=barrier,  # type: ignore[arg-type]
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "concurrent-admission-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )
    request = TerminalLaunchRequest(
        session_id="concurrent-admission",
        execution_id="concurrent-admission-execution",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
    )
    first = asyncio.create_task(supervisor.start(request))
    second = asyncio.create_task(supervisor.start(request))
    await barrier.entered.wait()
    barrier.release.set()

    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    successes = [item for item in outcomes if isinstance(item, TerminalSession)]
    conflicts = [item for item in outcomes if isinstance(item, ApplicationConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "terminal_start_cancelled"
    assert backend.calls == 1
    execution = await executions.get(str(request.execution_id))
    terminal = await terminals.get(str(request.session_id))
    assert execution is not None and execution.status is ExecutionStatus.RUNNING
    assert execution.physical_stop_confirmed_at is None
    assert terminal is not None and terminal.status is TerminalStatus.OPEN
    await supervisor.close(str(request.session_id))
    await database.dispose()


async def test_terminal_stop_wins_created_admission_cas_without_dispatch(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    blocked_claim = BlockFirstTerminalExecutionSave(
        executions,
        block_created_claim=True,
    )
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=blocked_claim,  # type: ignore[arg-type]
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "created-stop-race-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )
    request = TerminalLaunchRequest(
        session_id="created-stop-race",
        execution_id="created-stop-race-execution",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
    )
    start_task = asyncio.create_task(supervisor.start(request))
    await blocked_claim.entered.wait()
    created = await executions.get(str(request.execution_id))
    projection = await terminals.get(str(request.session_id))
    assert created is not None and created.status is ExecutionStatus.CREATED
    assert projection is not None and projection.status is TerminalStatus.CREATED

    stopped = await supervisor.close_execution(str(request.execution_id))
    blocked_claim.release.set()
    with pytest.raises(ApplicationConflictError) as captured:
        await start_task

    assert captured.value.code == "terminal_start_cancelled"
    assert stopped.status is ExecutionStatus.CANCELLED
    assert stopped.physical_stop_confirmed_at is not None
    assert backend.calls == 0
    projection = await terminals.get(str(request.session_id))
    assert projection is not None and projection.status is TerminalStatus.CLOSED
    await database.dispose()


async def test_terminal_cancelled_admission_claim_is_settled_before_propagation(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    blocked_claim = BlockFirstTerminalExecutionSave(
        executions,
        block_created_claim=True,
    )
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=blocked_claim,  # type: ignore[arg-type]
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "cancelled-claim-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )
    request = TerminalLaunchRequest(
        session_id="cancelled-claim",
        execution_id="cancelled-claim-execution",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
    )
    start_task = asyncio.create_task(supervisor.start(request))
    await blocked_claim.entered.wait()
    start_task.cancel()
    blocked_claim.release.set()

    with pytest.raises(asyncio.CancelledError):
        await start_task

    execution = await executions.get(str(request.execution_id))
    terminal = await terminals.get(str(request.session_id))
    assert execution is not None and execution.status is ExecutionStatus.CANCELLED
    assert execution.physical_stop_confirmed_at is not None
    assert terminal is not None and terminal.status is TerminalStatus.CLOSED
    assert backend.calls == 0
    await database.dispose()


async def test_pty_launch_fingerprint_rejects_same_key_stable_field_replay(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "pty-identity-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )
    request = TerminalLaunchRequest(
        session_id="pty-identity-session",
        execution_id="pty-identity-execution",
        execution_key="pty-identity-key",
        agent_session_id="agent-session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
        tool_id="terminal.exec",
        tool_version="1",
        env={"TERM": "xterm"},
    )

    first = await supervisor.start(request)
    await supervisor.resize(first.id, cols=request.cols + 1, rows=request.rows + 1)
    await supervisor.take_over(first.id)
    exact = await supervisor.start(request)

    assert exact.id == first.id == request.session_id
    assert exact.owner is TerminalOwner.USER
    assert (exact.cols, exact.rows) == (request.cols + 1, request.rows + 1)
    assert backend.calls == 1
    persisted = await executions.get(str(request.execution_id))
    assert persisted is not None
    assert persisted.launch_fingerprint == request.launch_fingerprint
    for foreign in (
        request.model_copy(update={"argv": ["foreign-shell"]}),
        request.model_copy(update={"tool_call_id": "foreign-tool-call"}),
        request.model_copy(update={"attempt_group": "retry-1"}),
        request.model_copy(update={"tool_version": "2"}),
        request.model_copy(update={"env": {"TERM": "foreign"}}),
        request.model_copy(update={"cols": request.cols + 1}),
    ):
        with pytest.raises(ApplicationConflictError) as captured:
            await supervisor.start(foreign)
        assert captured.value.code == "execution_idempotency_conflict"
    assert backend.calls == 1
    await supervisor.close(str(request.session_id))
    await database.dispose()


async def test_legacy_pty_replay_binds_explicit_execution_and_session_ids(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    request = TerminalLaunchRequest(
        session_id="legacy-pty-session",
        execution_id="legacy-pty-execution",
        execution_key="legacy-pty-key",
        agent_session_id="agent-session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
        run_id="run-1",
        node_id="local",
        cwd=tmp_path,
        argv=["fake-shell"],
        tool_id="terminal.exec",
        tool_version="1",
        cols=132,
        rows=48,
    )
    legacy_execution = Execution(
        id=str(request.execution_id),
        execution_key=str(request.execution_key),
        launch_fingerprint=None,
        run_id=request.run_id,
        session_id=request.agent_session_id,
        tool_call_id=request.tool_call_id,
        attempt_group=request.attempt_group,
        node_id=request.node_id,
        executor_type=ExecutorType.PTY,
        argv=request.argv,
        tool_id=request.tool_id,
        tool_version=request.tool_version,
        cwd=str(request.cwd),
        env_diff=request.env,
        status=ExecutionStatus.CANCELLED,
        stdout_path=str(tmp_path / "legacy-pty.log"),
        stderr_path=str(tmp_path / "legacy-pty.log"),
    )
    await executions.create_if_absent(legacy_execution)
    await terminals.create(
        TerminalSession(
            id=str(request.session_id),
            run_id=request.run_id,
            execution_id=legacy_execution.id,
            runner_id=request.node_id,
            shell=request.argv[0],
            cwd=str(request.cwd),
            status=TerminalStatus.CLOSED,
            owner=request.owner,
            cols=request.cols,
            rows=request.rows,
        )
    )
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "legacy-pty-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )

    exact = await supervisor.start(request)
    assert exact.id == request.session_id
    for foreign in (
        request.model_copy(
            update={
                "session_id": "foreign-session",
                "execution_id": "foreign-execution",
            }
        ),
        request.model_copy(update={"session_id": "foreign-session"}),
    ):
        with pytest.raises(ApplicationConflictError) as captured:
            await supervisor.start(foreign)
        assert captured.value.code == "execution_idempotency_conflict"
    for mismatch, foreign in (
        ("terminal_owner", request.model_copy(update={"owner": TerminalOwner.USER})),
        ("terminal_cols", request.model_copy(update={"cols": request.cols + 1})),
        ("terminal_rows", request.model_copy(update={"rows": request.rows + 1})),
    ):
        guard = AsyncMock()
        with pytest.raises(ApplicationConflictError, match=mismatch) as captured:
            await supervisor.start(foreign, effect_guard=guard)
        assert captured.value.code == "execution_idempotency_conflict"
        guard.assert_not_awaited()
    assert backend.calls == 0
    await database.dispose()


async def test_terminal_registers_starting_before_guard_and_does_not_open_when_blocked(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "guarded-terminal-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )
    guard_entered = asyncio.Event()
    guard_release = asyncio.Event()

    async def blocked_guard() -> None:
        guard_entered.set()
        await guard_release.wait()
        raise ApplicationConflictError("run_execution_blocked", "Run is cancelling")

    start_task = asyncio.create_task(
        supervisor.start(
            TerminalLaunchRequest(
                session_id="guarded-terminal",
                execution_id="guarded-terminal-execution",
                run_id="run-1",
                node_id="local",
                cwd=tmp_path,
                argv=["fake-shell"],
            ),
            effect_guard=blocked_guard,
        )
    )
    await guard_entered.wait()
    execution = (await executions.list("run-1"))[0]
    terminal = await terminals.get("guarded-terminal")
    assert execution.status is ExecutionStatus.STARTING
    assert terminal is not None and terminal.status is TerminalStatus.CREATED
    assert backend.calls == 0
    with pytest.raises(ProcessTerminationError):
        await supervisor.close_execution(execution.id)
    still_starting = await executions.get(execution.id)
    assert still_starting is not None
    assert still_starting.status is ExecutionStatus.STARTING
    assert still_starting.physical_stop_confirmed_at is None

    guard_release.set()
    with pytest.raises(ApplicationConflictError, match="Run is cancelling"):
        await start_task

    persisted_execution = await executions.get(execution.id)
    persisted_terminal = await terminals.get("guarded-terminal")
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.CANCELLED
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.CLOSED
    assert backend.calls == 0
    await database.dispose()


async def test_cancelled_terminal_start_terminates_spawned_handle_before_registration(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    barrier_repository = BlockFirstTerminalExecutionSave(executions)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=barrier_repository,  # type: ignore[arg-type]
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "cancelled-terminal-state"),
        native_backend=backend,  # type: ignore[arg-type]
    )
    start_task = asyncio.create_task(
        supervisor.start(
            TerminalLaunchRequest(
                session_id="cancelled-terminal",
                execution_id="cancelled-terminal-execution",
                run_id="run-1",
                node_id="local",
                cwd=tmp_path,
                argv=["fake-shell"],
            )
        )
    )
    await barrier_repository.entered.wait()
    assert backend.calls == 1

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert backend.handle.terminated.is_set()
    assert backend.handle.output_closed.is_set()
    execution = (await executions.list("run-1"))[0]
    terminal = await terminals.get("cancelled-terminal")
    assert execution.status is ExecutionStatus.CANCELLED
    assert terminal is not None and terminal.status is TerminalStatus.CLOSED
    await database.dispose()


async def test_cancelled_native_backend_start_collects_and_terminates_late_handle(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    backend = BlockingStartTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "cancelled-native-start-state"),
        native_backend=backend,  # type: ignore[arg-type]
        termination_grace_seconds=0.1,
    )
    start_task = asyncio.create_task(
        supervisor.start(
            TerminalLaunchRequest(
                session_id="cancelled-native-start",
                execution_id="cancelled-native-start-execution",
                run_id="run-1",
                node_id="local",
                cwd=tmp_path,
                argv=["fake-shell"],
            )
        )
    )
    await backend.entered.wait()

    start_task.cancel()
    await asyncio.sleep(0)
    assert not start_task.done()
    backend.release.set()

    with pytest.raises(asyncio.CancelledError):
        await start_task
    assert backend.handle.terminated.is_set()
    assert backend.handle.output_closed.is_set()
    execution = (await executions.list("run-1"))[0]
    terminal = await terminals.get("cancelled-native-start")
    assert execution.status is ExecutionStatus.CANCELLED
    assert terminal is not None and terminal.status is TerminalStatus.CLOSED
    await database.dispose()


async def test_post_spawn_guard_cleanup_failure_retains_handle_for_cancel_retry(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    handle = FailingTerminationTerminalHandle()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "guard-cleanup-retry-state"),
        native_backend=RecordingTerminalBackend(handle),  # type: ignore[arg-type]
        termination_grace_seconds=0.1,
    )
    guard_calls = 0

    async def post_spawn_cancel_guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            raise ApplicationConflictError("terminal_cancelled", "cancel tombstone won")

    with pytest.raises(ApplicationConflictError, match="cancel tombstone won"):
        await supervisor.start(
            TerminalLaunchRequest(
                session_id="guard-cleanup-retry",
                execution_id="guard-cleanup-retry-execution",
                run_id="run-1",
                node_id="local",
                cwd=tmp_path,
                argv=["fake-shell"],
            ),
            effect_guard=post_spawn_cancel_guard,
        )

    execution = (await executions.list("run-1"))[0]
    terminal = await terminals.get("guard-cleanup-retry")
    assert guard_calls == 2
    assert execution.status is ExecutionStatus.STARTING
    assert execution.pid == handle.pid
    assert terminal is not None and terminal.status is TerminalStatus.CREATED
    assert "guard-cleanup-retry" in supervisor._managed

    handle.fail_termination = False
    closed = await supervisor.close("guard-cleanup-retry")
    persisted = await executions.get(execution.id)

    assert closed.status is TerminalStatus.CLOSED
    assert persisted is not None and persisted.status is ExecutionStatus.CANCELLED
    assert handle.terminated.is_set()
    assert handle.output_closed.is_set()
    await database.dispose()


async def test_terminal_close_confirms_explicit_pre_spawn_failure(
    tmp_path: Path,
) -> None:
    database, supervisor, terminals, executions = await _runtime(tmp_path)
    execution = Execution(
        id="failed-before-terminal-spawn",
        execution_key="terminal:failed-before-terminal-spawn",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["missing-shell"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "failed-before-terminal-spawn.log"),
        stderr_path=str(tmp_path / "failed-before-terminal-spawn.log"),
        status=ExecutionStatus.FAILED,
    )
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id="failed-before-terminal-spawn",
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)
    terminal.transition_to(TerminalStatus.CLOSED)
    await terminals.save(terminal)

    closed = await supervisor.close(terminal.id)

    persisted = await executions.get(execution.id)
    assert closed.status is TerminalStatus.CLOSED
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    await database.dispose()


async def test_terminal_failed_with_containment_identity_must_terminate_before_proof(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    containment = RecordingDetachedTerminalContainment("failed-containment-only")
    execution = Execution(
        id="failed-containment-only",
        execution_key="terminal:failed-containment-only",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["fake-shell"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "failed-containment-only.log"),
        stderr_path=str(tmp_path / "failed-containment-only.log"),
        status=ExecutionStatus.FAILED,
        containment_id=containment.identifier,
    )
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id=execution.id,
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "failed-containment-only-state"),
        native_backend=RecordingTerminalBackend(),  # type: ignore[arg-type]
        platform_name="posix",
        containment_manager=RecordingDetachedTerminalContainmentManager(  # type: ignore[arg-type]
            containment
        ),
    )

    closed = await supervisor.close(terminal.id)

    persisted = await executions.get(execution.id)
    assert containment.terminate_calls == 1
    assert containment.cleanup_calls == 1
    assert closed.status is TerminalStatus.CLOSED
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    assert persisted.physical_stop_confirmed_at is not None
    await database.dispose()


async def test_terminal_cancelled_with_containment_identity_is_not_pre_spawn_absence(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    containment = RecordingDetachedTerminalContainment("cancelled-containment-only")
    execution = Execution(
        id="cancelled-containment-only",
        execution_key="terminal:cancelled-containment-only",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["fake-shell"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "cancelled-containment-only.log"),
        stderr_path=str(tmp_path / "cancelled-containment-only.log"),
        status=ExecutionStatus.CANCELLED,
        containment_id=containment.identifier,
    )
    await executions.create_if_absent(execution)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "cancelled-containment-only-state"),
        native_backend=RecordingTerminalBackend(),  # type: ignore[arg-type]
        platform_name="posix",
        containment_manager=RecordingDetachedTerminalContainmentManager(  # type: ignore[arg-type]
            containment
        ),
    )

    stopped = await supervisor.close_execution(execution.id)

    assert containment.terminate_calls == 1
    assert containment.cleanup_calls == 1
    assert stopped.status is ExecutionStatus.CANCELLED
    assert stopped.physical_stop_confirmed_at is not None
    await database.dispose()


async def test_terminal_close_terminates_live_handle_before_converging_failed_execution(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    backend = RecordingTerminalBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "failed-live-terminal-state"),
        native_backend=backend,  # type: ignore[arg-type]
        termination_grace_seconds=0.1,
    )
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            session_id="failed-live-terminal",
            execution_id="failed-live-terminal-execution",
            run_id="run-1",
            node_id="local",
            cwd=tmp_path,
            argv=["fake-shell"],
        )
    )
    execution = await executions.get(terminal.execution_id)
    assert execution is not None
    execution.transition_to(ExecutionStatus.FAILED)
    await executions.save(execution)

    closed = await supervisor.close(terminal.id)

    persisted = await executions.get(execution.id)
    assert backend.handle.terminated.is_set()
    assert backend.handle.output_closed.is_set()
    assert closed.status is TerminalStatus.CLOSED
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    await database.dispose()


async def test_terminal_close_keeps_failed_when_live_handle_termination_fails(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    handle = FailingTerminationTerminalHandle()
    backend = RecordingTerminalBackend(handle)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "failed-terminal-termination-state"),
        native_backend=backend,  # type: ignore[arg-type]
        termination_grace_seconds=0.1,
    )
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            session_id="failed-terminal-termination",
            execution_id="failed-terminal-termination-execution",
            run_id="run-1",
            node_id="local",
            cwd=tmp_path,
            argv=["fake-shell"],
        )
    )
    execution = await executions.get(terminal.execution_id)
    assert execution is not None
    execution.transition_to(ExecutionStatus.FAILED)
    await executions.save(execution)

    with pytest.raises(RuntimeError, match="termination failed"):
        await supervisor.close(terminal.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.FAILED
    handle.fail_termination = False
    await supervisor.close(terminal.id)
    await database.dispose()


async def test_terminal_monitor_does_not_cancel_when_leader_exits_but_confirmation_fails(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    handle = LeaderExitedFailingTerminationTerminalHandle()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "leader-exit-confirmation-failure"),
        native_backend=RecordingTerminalBackend(handle),  # type: ignore[arg-type]
        termination_grace_seconds=0.1,
    )
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            session_id="leader-exit-confirmation-failure",
            execution_id="leader-exit-confirmation-failure-execution",
            run_id="run-1",
            node_id="local",
            cwd=tmp_path,
            argv=["fake-shell"],
        )
    )

    with pytest.raises(RuntimeError, match="tree confirmation failed"):
        await supervisor.close(terminal.id)
    await asyncio.sleep(0.05)

    persisted_execution = await executions.get(terminal.execution_id)
    persisted_terminal = await terminals.get(terminal.id)
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.RUNNING
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.OPEN

    # The attached handle remains available for a later safety retry.
    handle.fail_termination = False
    closed = await supervisor.close(terminal.id)
    persisted_execution = await executions.get(terminal.execution_id)
    assert closed.status is TerminalStatus.CLOSED
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.CANCELLED
    await database.dispose()


async def test_terminal_monitor_uses_cas_and_retries_a_concurrent_failed_outcome(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    repository = RaceTerminalCancellationRepository(executions)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=repository,  # type: ignore[arg-type]
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "terminal-cancellation-cas"),
        native_backend=RecordingTerminalBackend(),  # type: ignore[arg-type]
        termination_grace_seconds=0.1,
    )
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            session_id="terminal-cancellation-cas",
            execution_id="terminal-cancellation-cas-execution",
            run_id="run-1",
            node_id="local",
            cwd=tmp_path,
            argv=["fake-shell"],
        )
    )

    closed = await supervisor.close(terminal.id)

    persisted = await executions.get(terminal.execution_id)
    assert repository.race_injected
    assert closed.status is TerminalStatus.CLOSED
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED
    assert persisted.physical_stop_confirmed_at is not None
    await database.dispose()


async def test_terminal_containment_survives_stop_proof_persistence_failure(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    handle = ContainedRecordingTerminalHandle()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=RejectTerminalPhysicalStopProofSave(executions),  # type: ignore[arg-type]
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "terminal-proof-failure-state"),
        native_backend=RecordingTerminalBackend(handle),  # type: ignore[arg-type]
        termination_grace_seconds=0.1,
    )
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            session_id="terminal-proof-failure",
            execution_id="terminal-proof-failure-execution",
            run_id="run-1",
            node_id="local",
            cwd=tmp_path,
            argv=["fake-shell"],
        )
    )

    with pytest.raises(RuntimeError, match="stop-proof persistence failure"):
        await supervisor.close(terminal.id)

    persisted_execution = await executions.get(terminal.execution_id)
    persisted_terminal = await terminals.get(terminal.id)
    assert handle.terminated.is_set()
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.RUNNING
    assert persisted_execution.physical_stop_confirmed_at is None
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.OPEN
    assert handle.boundary_exists is True
    assert not handle.containment_cleaned.is_set()
    await database.dispose()


async def test_terminal_close_does_not_confirm_failed_execution_without_native_handle(
    tmp_path: Path,
) -> None:
    database, supervisor, terminals, executions = await _runtime(tmp_path)
    execution = Execution(
        id="failed-terminal-without-handle",
        execution_key="terminal:failed-terminal-without-handle",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["fake-shell"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "failed-terminal-without-handle.log"),
        stderr_path=str(tmp_path / "failed-terminal-without-handle.log"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(ExecutionStatus.FAILED)
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id="failed-terminal-without-handle",
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)
    terminal.transition_to(TerminalStatus.OPEN)
    await terminals.save(terminal)

    with pytest.raises(ProcessTerminationError, match="handle is not attached"):
        await supervisor.close(terminal.id)

    persisted_execution = await executions.get(execution.id)
    persisted_terminal = await terminals.get(terminal.id)
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.FAILED
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.OPEN
    await database.dispose()


async def test_terminal_close_fails_closed_for_starting_created_without_native_handle(
    tmp_path: Path,
) -> None:
    database, supervisor, terminals, executions = await _runtime(tmp_path)
    execution = Execution(
        id="unattached-starting-terminal",
        execution_key="terminal:unattached-starting-terminal",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["fake-shell"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "unattached-starting-terminal.log"),
        stderr_path=str(tmp_path / "unattached-starting-terminal.log"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.pid = 424242
    execution.process_group_id = 424242
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id="unattached-starting-terminal",
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)

    with pytest.raises(ProcessTerminationError, match="handle is not attached"):
        await supervisor.close(terminal.id)

    persisted_execution = await executions.get(execution.id)
    persisted_terminal = await terminals.get(terminal.id)
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.STARTING
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.CREATED
    await database.dispose()


async def test_conpty_late_close_fails_closed_for_exited_closed_without_tree_handle(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "unattached-conpty-state"),
        native_backend=RecordingTerminalBackend(),  # type: ignore[arg-type]
        platform_name="nt",
    )
    execution = Execution(
        id="unattached-exited-conpty",
        execution_key="terminal:unattached-exited-conpty",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["pwsh.exe"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "unattached-exited-conpty.log"),
        stderr_path=str(tmp_path / "unattached-exited-conpty.log"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.pid = 515151
    execution.process_group_id = 515151
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(ExecutionStatus.EXITED, exit_code=0)
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id="unattached-exited-conpty",
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)
    terminal.transition_to(TerminalStatus.OPEN)
    terminal.transition_to(TerminalStatus.CLOSED)
    await terminals.save(terminal)

    with pytest.raises(ProcessTerminationError, match="handle is not attached"):
        await supervisor.close(terminal.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None and persisted.status is ExecutionStatus.EXITED
    await database.dispose()


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
async def test_terminal_close_kills_stubborn_same_group_child_before_cancelling(
    tmp_path: Path,
) -> None:
    database, supervisor, terminals, executions = await _runtime(tmp_path)
    identity_path = tmp_path / "stubborn-child.identity"
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            session_id="stubborn-pty-group",
            execution_id="stubborn-pty-group-execution",
            run_id="run-1",
            node_id="local",
            cwd=tmp_path,
            argv=[
                sys.executable,
                "-u",
                "-c",
                _STUBBORN_GROUP_LEADER,
                _STUBBORN_GROUP_CHILD,
                str(identity_path),
            ],
        )
    )
    await _wait_for_output(supervisor, terminal.id, "STUBBORN_CHILD_READY")
    child_pid, child_process_group = (
        int(value) for value in identity_path.read_text().split(":", maxsplit=1)
    )
    execution = await executions.get(terminal.execution_id)
    assert execution is not None
    assert execution.pid is not None
    assert execution.process_group_id == execution.pid
    assert child_pid != execution.pid
    assert child_process_group == execution.process_group_id
    assert child_process_group != os.getpgrp()

    try:
        closed = await supervisor.close(terminal.id)

        persisted_execution = await executions.get(terminal.execution_id)
        persisted_terminal = await terminals.get(terminal.id)
        assert not _process_group_exists(child_process_group)
        assert not _pid_exists(child_pid)
        assert closed.status is TerminalStatus.CLOSED
        assert persisted_terminal is not None
        assert persisted_terminal.status is TerminalStatus.CLOSED
        assert persisted_execution is not None
        assert persisted_execution.status is ExecutionStatus.CANCELLED
    finally:
        if _process_group_exists(child_process_group):
            os.killpg(child_process_group, signal.SIGKILL)
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


async def test_recovery_includes_created_terminal_with_starting_execution(
    tmp_path: Path,
) -> None:
    database, supervisor, terminals, executions = await _runtime(tmp_path)
    execution = Execution(
        id="execution-starting",
        execution_key="terminal:created-starting",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["/bin/sh"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "starting.log"),
        stderr_path=str(tmp_path / "starting.log"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id="created-starting",
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)

    recovered = await supervisor.recover()

    assert [item.id for item in recovered] == [terminal.id]
    persisted_terminal = await terminals.get(terminal.id)
    persisted_execution = await executions.get(execution.id)
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.LOST
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.LOST
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


async def test_recovery_closes_active_terminal_from_durable_proof_after_leaf_cleanup(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    execution = Execution(
        id="recovery-confirmed-stop",
        execution_key="terminal:recovery-confirmed-stop",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["/bin/sh"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "recovery-confirmed.log"),
        stderr_path=str(tmp_path / "recovery-confirmed.log"),
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(ExecutionStatus.CANCELLED)
    execution.containment_id = "already-cleaned-containment"
    execution.physical_stop_confirmed_at = datetime(2026, 8, 1, tzinfo=UTC)
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id="recovery-confirmed-stop",
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)
    terminal.transition_to(TerminalStatus.OPEN)
    await terminals.save(terminal)
    containment = MissingConfirmedTerminalContainment()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "recovery-confirmed-state"),
        native_backend=RecordingTerminalBackend(),  # type: ignore[arg-type]
        platform_name="posix",
        containment_manager=MissingConfirmedTerminalContainmentManager(  # type: ignore[arg-type]
            containment
        ),
    )

    recovered = await supervisor.recover()

    persisted_terminal = await terminals.get(terminal.id)
    assert [item.id for item in recovered] == [terminal.id]
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.CLOSED
    assert containment.cleanup_calls == 0
    await database.dispose()


async def test_close_execution_repairs_stale_terminal_projection_from_existing_proof(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    execution = Execution(
        id="close-existing-proof",
        execution_key="terminal:close-existing-proof",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["/bin/sh"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "close-existing-proof.log"),
        stderr_path=str(tmp_path / "close-existing-proof.log"),
        status=ExecutionStatus.EXITED,
        containment_id="already-cleaned-close-containment",
        physical_stop_confirmed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id=execution.id,
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)
    terminal.transition_to(TerminalStatus.OPEN)
    await terminals.save(terminal)
    containment = MissingConfirmedTerminalContainment()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "close-existing-proof-state"),
        native_backend=RecordingTerminalBackend(),  # type: ignore[arg-type]
        platform_name="posix",
        containment_manager=MissingConfirmedTerminalContainmentManager(  # type: ignore[arg-type]
            containment
        ),
    )

    stopped = await supervisor.close_execution(execution.id)

    persisted_terminal = await terminals.get(terminal.id)
    assert stopped.status is ExecutionStatus.EXITED
    assert stopped.physical_stop_confirmed_at is not None
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.CLOSED
    assert containment.cleanup_calls == 0
    await database.dispose()


async def test_terminal_recovery_cas_preserves_concurrent_confirmed_cancellation(
    tmp_path: Path,
) -> None:
    database, _, terminals, executions = await _runtime(tmp_path)
    execution = Execution(
        id="recovery-cancel-race",
        execution_key="terminal:recovery-cancel-race",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["/bin/sh"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "recovery-cancel-race.log"),
        stderr_path=str(tmp_path / "recovery-cancel-race.log"),
    )
    await executions.create_if_absent(execution)
    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    await executions.save(execution)
    terminal = TerminalSession(
        id="recovery-cancel-race",
        run_id="run-1",
        execution_id=execution.id,
    )
    await terminals.create(terminal)
    terminal.transition_to(TerminalStatus.OPEN)
    await terminals.save(terminal)
    racing_repository = RaceTerminalRecoveryRepository(executions)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=racing_repository,  # type: ignore[arg-type]
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "recovery-cancel-race-state"),
        native_backend=RecordingTerminalBackend(),  # type: ignore[arg-type]
    )

    recovered = await supervisor.recover()

    persisted_execution = await executions.get(execution.id)
    persisted_terminal = await terminals.get(terminal.id)
    assert racing_repository.race_injected
    assert recovered == []
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.CANCELLED
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.OPEN
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
    assert execution.created_at is not None
    assert execution.status is ExecutionStatus.EXITED
    assert execution.exit_code == 7
    await supervisor.close_all()
    await database.dispose()
