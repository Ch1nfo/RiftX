from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

import riftx.runner._pty_child as pty_launcher_module
import riftx.runner.unix_pty as unix_pty_module
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    TerminalSession,
    TerminalStatus,
)
from riftx.executors import (
    LinuxCgroupV2Manager,
    ProcessStartError,
    UnverifiedProcessTreeTerminationError,
    merge_environment,
)
from riftx.runner import RunnerPaths, TerminalLaunchRequest, TerminalSupervisor
from riftx.runner.state import FileExecutionRepository, FileTerminalRepository
from riftx.runner.supervisor import ProcessTerminationError
from riftx.runner.terminal_manager import NullRunEventRepository
from riftx.runner.unix_pty import UnixPTYBackend

from ._containment_support import FakeKernelContainmentManager

FIXTURE = Path(__file__).parent / "fixtures" / "fake_process.py"


def _distinct_payload_uid() -> int:
    return 60000 if os.geteuid() != 60000 else 60001


def test_pty_launcher_rejects_payload_uid_equal_to_runner_uid() -> None:
    with pytest.raises(RuntimeError, match="must differ"):
        pty_launcher_module._drop_payload_identity(os.geteuid(), os.getegid())


def test_pty_launcher_fails_closed_when_payload_identity_drop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_setgroups(_groups: list[int]) -> None:
        raise PermissionError("setgroups denied")

    monkeypatch.setattr(pty_launcher_module.os, "setgroups", deny_setgroups)
    with pytest.raises(PermissionError, match="setgroups denied"):
        pty_launcher_module._drop_payload_identity(
            _distinct_payload_uid(),
            os.getegid(),
        )


def test_pty_launcher_rejects_writable_ancestor_cgroup(tmp_path: Path) -> None:
    writable = tmp_path / "cgroup.procs"
    writable.write_text("", encoding="ascii")

    with pytest.raises(RuntimeError, match="retains .* access"):
        pty_launcher_module._verify_ancestor_migration_denied((writable,))


def test_pty_launcher_rejects_residual_payload_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pty_launcher_module,
        "_read_self_status",
        lambda: (
            "CapInh:\t0000000000000000\n"
            "CapPrm:\t0000000000000000\n"
            "CapEff:\t0000000000000000\n"
            "CapAmb:\t0000000000000002\n"
        ),
    )

    with pytest.raises(RuntimeError, match="CapAmb"):
        pty_launcher_module._verify_capabilities_cleared()


def _request(tmp_path: Path, *argv: str, session_id: str) -> TerminalLaunchRequest:
    return TerminalLaunchRequest(
        session_id=session_id,
        execution_id=f"execution:{session_id}",
        run_id="run-pty-containment",
        node_id="local",
        cwd=tmp_path,
        argv=list(argv),
    )


async def _wait_for_file(path: Path) -> None:
    await asyncio.to_thread(_wait_for_file_sync, path)


def _wait_for_file_sync(path: Path) -> None:
    deadline = time.monotonic() + 3.0
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_pid_exit(pid: int) -> None:
    await asyncio.to_thread(_wait_for_pid_exit_sync, pid)


def _wait_for_pid_exit_sync(pid: int) -> None:
    deadline = time.monotonic() + 3.0
    while _pid_exists(pid):
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for pid {pid} to exit")
        time.sleep(0.01)


class _FailingPTYSpawnCleanupContainment:
    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.fail_force_terminate = False

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def force_terminate(self, *, confirmation_seconds: float) -> None:
        if self.fail_force_terminate:
            raise RuntimeError("injected PTY force_terminate failure")
        await self._delegate.force_terminate(
            confirmation_seconds=confirmation_seconds,
        )


class _FailingPTYSpawnCleanupManager:
    def __init__(self, root: Path) -> None:
        self._delegate = FakeKernelContainmentManager(root)
        self.containment: _FailingPTYSpawnCleanupContainment | None = None

    async def prepare(self, execution_key: str) -> _FailingPTYSpawnCleanupContainment:
        containment = _FailingPTYSpawnCleanupContainment(
            await self._delegate.prepare(execution_key)
        )
        self.containment = containment
        return containment

    def containment_for(self, execution_key: str) -> _FailingPTYSpawnCleanupContainment:
        resolved = self._delegate.containment_for(execution_key)
        if self.containment is None or self.containment.identifier != resolved.identifier:
            raise AssertionError("PTY spawn-cleanup containment was not prepared")
        return self.containment


@pytest.mark.skipif(os.name != "posix", reason="Unix PTY implementation")
async def test_unix_pty_target_stays_gated_until_activation(tmp_path: Path) -> None:
    marker = tmp_path / "target-ran"
    transcript = tmp_path / "terminal.log"
    manager = FakeKernelContainmentManager(tmp_path / "containment")
    backend = UnixPTYBackend(
        manager,
        autodetect_containment=False,
        require_containment=True,
    )
    handle = await backend.start(
        _request(
            tmp_path,
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            session_id="activation-gate",
        ),
        transcript_path=transcript,
        environment=merge_environment(),
    )

    assert handle.activation_pending
    assert handle.containment_identifier is not None
    assert not marker.exists()
    assert (
        str(handle.pid)
        in (manager.containment_for("terminal:activation-gate").path / "cgroup.procs")
        .read_text()
        .splitlines()
    )

    await handle.activate()
    assert not handle.activation_pending
    assert await handle.wait() == 0
    await handle.cleanup_confirmed_containment()
    await handle.close_output()

    assert marker.read_text() == "ran"
    assert not manager.containment_for("terminal:activation-gate").path.exists()


@pytest.mark.skipif(os.name != "posix", reason="Unix PTY implementation")
async def test_unix_pty_launcher_defers_target_python_environment_until_activation(
    tmp_path: Path,
) -> None:
    site_directory = tmp_path / "target-python-path"
    site_directory.mkdir()
    site_marker = tmp_path / "pty-sitecustomize-ran"
    target_marker = tmp_path / "pty-target-environment"
    (site_directory / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(site_marker)!r}).write_text('loaded')\n",
        encoding="utf-8",
    )
    manager = FakeKernelContainmentManager(tmp_path / "containment")
    handle = await UnixPTYBackend(
        manager,
        autodetect_containment=False,
        require_containment=True,
    ).start(
        _request(
            tmp_path,
            sys.executable,
            "-c",
            "import os; from pathlib import Path; "
            f"Path({str(target_marker)!r}).write_text(os.environ['PAYLOAD_ENV_VALUE'])",
            session_id="target-environment-gate",
        ),
        transcript_path=tmp_path / "target-environment.log",
        environment=merge_environment(
            {
                "PYTHONPATH": str(site_directory),
                "PAYLOAD_ENV_VALUE": "preserved-after-activation",
            }
        ),
    )

    try:
        await asyncio.sleep(0.05)
        assert not site_marker.exists()
        assert not target_marker.exists()

        await handle.activate()
        assert await handle.wait() == 0
        assert site_marker.read_text() == "loaded"
        assert target_marker.read_text() == "preserved-after-activation"
        await handle.cleanup_confirmed_containment()
    finally:
        if handle.process.returncode is None:
            handle.process.kill()
            await handle.process.wait()
        await handle.close_output()


@pytest.mark.skipif(os.name != "posix", reason="Unix PTY implementation")
async def test_optional_unsafe_real_manager_is_not_used_as_pty_containment(
    tmp_path: Path,
) -> None:
    backend = UnixPTYBackend(
        LinuxCgroupV2Manager(tmp_path, verify_filesystem=False),
        autodetect_containment=False,
        require_containment=False,
    )
    assert backend.containment_manager is None
    handle = await backend.start(
        _request(
            tmp_path,
            sys.executable,
            "-c",
            "raise SystemExit(0)",
            session_id="optional-unsafe-manager",
        ),
        transcript_path=tmp_path / "optional-unsafe-manager.log",
        environment=merge_environment(),
    )

    try:
        assert handle.containment_identifier is None
        await handle.activate()
        with pytest.raises(UnverifiedProcessTreeTerminationError, match="ended naturally"):
            await handle.wait()
    finally:
        if handle.process.returncode is None:
            handle.process.kill()
            await handle.process.wait()
        await handle.close_output()


@pytest.mark.skipif(os.name != "posix", reason="Unix PTY implementation")
async def test_required_unsafe_real_manager_rejects_pty_start(tmp_path: Path) -> None:
    backend = UnixPTYBackend(
        LinuxCgroupV2Manager(tmp_path, verify_filesystem=False),
        autodetect_containment=False,
        require_containment=True,
    )

    with pytest.raises(ProcessStartError, match="payload_uid and payload_gid"):
        await backend.start(
            _request(tmp_path, "/bin/sh", session_id="required-unsafe-manager"),
            transcript_path=tmp_path / "required-unsafe-manager.log",
            environment=merge_environment(),
        )


@pytest.mark.skipif(os.name != "posix", reason="Unix PTY implementation")
async def test_unix_pty_openpty_failure_cleans_prepared_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeKernelContainmentManager(tmp_path / "containment")

    def fail_openpty() -> tuple[int, int]:
        raise OSError("injected openpty failure")

    monkeypatch.setattr(unix_pty_module.pty, "openpty", fail_openpty)
    with pytest.raises(ProcessStartError, match="injected openpty failure"):
        await UnixPTYBackend(manager, autodetect_containment=False).start(
            _request(tmp_path, "/bin/sh", session_id="openpty-failure"),
            transcript_path=tmp_path / "openpty-failure.log",
            environment=merge_environment(),
        )

    assert not manager.containment_for("terminal:openpty-failure").path.exists()


@pytest.mark.skipif(os.name != "posix", reason="Unix PTY implementation")
async def test_unix_pty_socketpair_failure_closes_pty_fds_and_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeKernelContainmentManager(tmp_path / "containment")
    real_openpty = unix_pty_module.pty.openpty
    opened_fds: tuple[int, int] | None = None

    def recording_openpty() -> tuple[int, int]:
        nonlocal opened_fds
        opened_fds = real_openpty()
        return opened_fds

    def fail_socketpair():
        raise OSError("injected socketpair failure")

    monkeypatch.setattr(unix_pty_module.pty, "openpty", recording_openpty)
    monkeypatch.setattr(unix_pty_module, "_open_control_socketpair", fail_socketpair)
    with pytest.raises(ProcessStartError, match="injected socketpair failure"):
        await UnixPTYBackend(manager, autodetect_containment=False).start(
            _request(tmp_path, "/bin/sh", session_id="socketpair-failure"),
            transcript_path=tmp_path / "socketpair-failure.log",
            environment=merge_environment(),
        )

    assert opened_fds is not None
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert not manager.containment_for("terminal:socketpair-failure").path.exists()


@pytest.mark.skipif(os.name != "posix", reason="Unix PTY implementation")
@pytest.mark.parametrize("failure_mode", ["force_terminate", "process_wait"])
async def test_pty_spawn_cleanup_failure_stays_cancellable_without_false_stop_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    manager = _FailingPTYSpawnCleanupManager(tmp_path / "containment")
    backend = UnixPTYBackend(
        manager,  # type: ignore[arg-type]
        autodetect_containment=False,
        require_containment=True,
    )
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        native_backend=backend,
        platform_name="posix",
        containment_manager=manager,  # type: ignore[arg-type]
        autodetect_containment=False,
        termination_grace_seconds=0.1,
    )
    wait_failure_enabled = failure_mode == "process_wait"
    original_wait = asyncio.subprocess.Process.wait

    async def injected_wait(process: asyncio.subprocess.Process) -> int:
        if wait_failure_enabled:
            raise RuntimeError("injected PTY process.wait failure")
        return await original_wait(process)

    async def fail_readiness(*_args, **_kwargs) -> bytes:
        raise ProcessStartError("injected PTY launcher readiness failure")

    monkeypatch.setattr(asyncio.subprocess.Process, "wait", injected_wait)
    monkeypatch.setattr(unix_pty_module, "_read_launcher_readiness", fail_readiness)
    if failure_mode == "force_terminate":

        async def fail_after_prepare(*args, **kwargs) -> bytes:
            assert manager.containment is not None
            manager.containment.fail_force_terminate = True
            return await fail_readiness(*args, **kwargs)

        monkeypatch.setattr(
            unix_pty_module,
            "_read_launcher_readiness",
            fail_after_prepare,
        )

    session_id = f"spawn-cleanup-{failure_mode}"
    terminal = await supervisor.start(
        _request(
            tmp_path,
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            session_id=session_id,
        )
    )
    execution = await executions.get(terminal.execution_id)

    assert terminal.status is TerminalStatus.CREATED
    assert execution is not None
    assert execution.status is ExecutionStatus.STARTING
    assert execution.pid is not None
    assert execution.process_group_id is not None
    assert execution.containment_id is not None
    assert execution.process_created_at is not None
    assert execution.physical_stop_confirmed_at is None

    expected_error = (
        "force_terminate failure" if failure_mode == "force_terminate" else "process.wait failure"
    )
    with pytest.raises(RuntimeError, match=expected_error):
        await supervisor.close(session_id)

    unconfirmed = await executions.get(terminal.execution_id)
    durable_terminal = await terminals.get(session_id)
    assert unconfirmed is not None
    assert unconfirmed.status is ExecutionStatus.STARTING
    assert unconfirmed.pid == execution.pid
    assert unconfirmed.containment_id == execution.containment_id
    assert unconfirmed.physical_stop_confirmed_at is None
    assert durable_terminal is not None
    assert durable_terminal.status is TerminalStatus.CREATED

    wait_failure_enabled = False
    assert manager.containment is not None
    manager.containment.fail_force_terminate = False
    closed = await supervisor.close(session_id)
    confirmed = await executions.get(terminal.execution_id)

    assert closed.status is TerminalStatus.CLOSED
    assert confirmed is not None
    assert confirmed.status is ExecutionStatus.CANCELLED
    assert confirmed.physical_stop_confirmed_at is not None


@pytest.mark.skipif(os.name != "posix", reason="Unix PTY implementation")
async def test_uncontained_unix_pty_natural_exit_fails_closed(tmp_path: Path) -> None:
    handle = await UnixPTYBackend(autodetect_containment=False).start(
        _request(
            tmp_path,
            sys.executable,
            "-c",
            "raise SystemExit(7)",
            session_id="uncontained-natural-exit",
        ),
        transcript_path=tmp_path / "natural.log",
        environment=merge_environment(),
    )
    await handle.activate()

    with pytest.raises(UnverifiedProcessTreeTerminationError, match="ended naturally"):
        await handle.wait()
    await handle.close_output()
    assert handle.process.returncode == 7


@pytest.mark.skipif(os.name != "posix", reason="setsid and double-fork require POSIX")
async def test_uncontained_unix_pty_close_fails_closed_for_escaped_descendant(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "escaped-heartbeat"
    pid_file = tmp_path / "escaped.pid"
    handle = await UnixPTYBackend(autodetect_containment=False).start(
        _request(
            tmp_path,
            sys.executable,
            str(FIXTURE),
            "setsid-double-fork",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
            "--pid-file",
            str(pid_file),
            session_id="uncontained-double-fork",
        ),
        transcript_path=tmp_path / "escaped.log",
        environment=merge_environment(),
    )
    escaped_pid: int | None = None
    try:
        await handle.activate()
        await _wait_for_file(heartbeat)
        await _wait_for_file(pid_file)
        escaped_pid = int(pid_file.read_text())

        with pytest.raises(UnverifiedProcessTreeTerminationError, match="cannot be proven"):
            await handle.terminate(0.05)
        heartbeat_size = heartbeat.stat().st_size
        await asyncio.sleep(0.15)

        assert _pid_exists(escaped_pid)
        assert heartbeat.stat().st_size > heartbeat_size
    finally:
        if escaped_pid is not None and _pid_exists(escaped_pid):
            os.kill(escaped_pid, signal.SIGKILL)
        await handle.close_output()


@pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or not os.environ.get("RIFTX_TEST_CGROUP_V2_ROOT")
    or not os.environ.get("RIFTX_TEST_PAYLOAD_UID")
    or not os.environ.get("RIFTX_TEST_PAYLOAD_GID"),
    reason="requires delegated cgroup v2 and a distinct payload uid/gid",
)
async def test_real_cgroup_unix_pty_contains_setsid_double_fork(tmp_path: Path) -> None:
    manager = LinuxCgroupV2Manager(
        Path(os.environ["RIFTX_TEST_CGROUP_V2_ROOT"]),
        payload_uid=int(os.environ["RIFTX_TEST_PAYLOAD_UID"]),
        payload_gid=int(os.environ["RIFTX_TEST_PAYLOAD_GID"]),
    )
    await asyncio.to_thread(tmp_path.chmod, 0o777)
    heartbeat = tmp_path / "heartbeat"
    pid_file = tmp_path / "escaped.pid"
    session_id = f"real-cgroup-{tmp_path.name}"
    handle = await UnixPTYBackend(
        manager,
        autodetect_containment=False,
        require_containment=True,
    ).start(
        _request(
            tmp_path,
            sys.executable,
            str(FIXTURE),
            "setsid-double-fork",
            "--seconds",
            "30",
            "--heartbeat",
            str(heartbeat),
            "--pid-file",
            str(pid_file),
            session_id=session_id,
        ),
        transcript_path=tmp_path / "real-cgroup.log",
        environment=merge_environment(),
    )
    escaped_pid: int | None = None
    try:
        await handle.activate()
        await _wait_for_file(heartbeat)
        await _wait_for_file(pid_file)
        escaped_pid = int(pid_file.read_text())

        await handle.terminate(0.1)
        heartbeat_size = heartbeat.stat().st_size
        await _wait_for_pid_exit(escaped_pid)
        await asyncio.sleep(0.15)

        assert heartbeat.stat().st_size == heartbeat_size
        await handle.cleanup_confirmed_containment()
        assert not manager.containment_for(f"terminal:{session_id}").path.exists()
    finally:
        if escaped_pid is not None and _pid_exists(escaped_pid):
            os.kill(escaped_pid, signal.SIGKILL)
        await handle.close_output()


class _GatedRecordingHandle:
    pid = 424242
    process_group_id = 424242
    containment_identifier = "test-containment"

    def __init__(self) -> None:
        self.activated = asyncio.Event()
        self.terminated = asyncio.Event()

    @property
    def activation_pending(self) -> bool:
        return not self.activated.is_set()

    async def activate(self) -> None:
        self.activated.set()

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
        return None

    async def close_output(self) -> None:
        return None


class _GatedRecordingBackend:
    def __init__(self, handle: _GatedRecordingHandle) -> None:
        self.handle = handle

    async def start(self, request, *, transcript_path, environment):
        return self.handle


class _BlockingContainment:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.terminate_started = asyncio.Event()
        self.release_terminate = asyncio.Event()
        self.terminate_calls = 0

    @property
    def identifier(self) -> str:
        return self.delegate.identifier

    def boundary_exists(self) -> bool:
        return self.delegate.boundary_exists()

    async def terminate(self, *, grace_seconds: float) -> None:
        self.terminate_calls += 1
        self.terminate_started.set()
        await self.release_terminate.wait()
        await self.delegate.terminate(grace_seconds=grace_seconds)

    async def cleanup(self) -> None:
        await self.delegate.cleanup()


class _BlockingContainmentManager:
    def __init__(self, root: Path) -> None:
        self.delegate = FakeKernelContainmentManager(root)
        self.containment: _BlockingContainment | None = None

    async def prepare(self, execution_key: str) -> _BlockingContainment:
        self.containment = _BlockingContainment(await self.delegate.prepare(execution_key))
        return self.containment

    def containment_for(self, execution_key: str) -> _BlockingContainment:
        resolved = self.delegate.containment_for(execution_key)
        if self.containment is None or self.containment.identifier != resolved.identifier:
            raise AssertionError("blocking containment was not prepared")
        return self.containment


async def test_terminal_supervisor_persists_identity_before_activation(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    handle = _GatedRecordingHandle()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        native_backend=_GatedRecordingBackend(handle),  # type: ignore[arg-type]
        platform_name="posix",
        autodetect_containment=False,
    )
    second_guard_entered = asyncio.Event()
    release_guard = asyncio.Event()
    guard_calls = 0

    async def effect_guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            second_guard_entered.set()
            await release_guard.wait()

    start = asyncio.create_task(
        supervisor.start(
            _request(tmp_path, "fake-shell", session_id="durable-before-activate"),
            effect_guard=effect_guard,
        )
    )
    await second_guard_entered.wait()

    persisted = (await executions.list("run-pty-containment"))[0]
    assert persisted.status is ExecutionStatus.STARTING
    assert persisted.pid == handle.pid
    assert persisted.process_group_id == handle.process_group_id
    assert persisted.containment_id == handle.containment_identifier
    assert not handle.activated.is_set()

    release_guard.set()
    terminal = await start
    assert handle.activated.is_set()
    assert (await executions.get(terminal.execution_id)).status is ExecutionStatus.RUNNING  # type: ignore[union-attr]
    await supervisor.close(terminal.id)


async def _persist_detached_terminal(
    executions: FileExecutionRepository,
    terminals: FileTerminalRepository,
    *,
    session_id: str,
    containment_id: str,
) -> None:
    execution = Execution(
        id=f"execution-{session_id}",
        execution_key=f"terminal:{session_id}",
        run_id="run-pty-containment",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["fake-shell"],
        cwd="/tmp",
        stdout_path="/tmp/detached-terminal.log",
        stderr_path="/tmp/detached-terminal.log",
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.pid = 424242
    execution.process_group_id = 424242
    execution.containment_id = containment_id
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(ExecutionStatus.LOST)
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id=session_id,
        run_id=execution.run_id,
        execution_id=execution.id,
    )
    terminal.transition_to(TerminalStatus.OPEN)
    terminal.transition_to(TerminalStatus.LOST)
    await terminals.create(terminal)


async def _persist_detached_execution_without_terminal(
    executions: FileExecutionRepository,
    *,
    session_id: str,
    containment_id: str,
) -> Execution:
    execution = Execution(
        id=f"execution-{session_id}",
        execution_key=f"terminal:{session_id}",
        run_id="run-pty-containment",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["fake-shell"],
        cwd="/tmp",
        stdout_path="/tmp/detached-terminal.log",
        stderr_path="/tmp/detached-terminal.log",
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.pid = 424242
    execution.process_group_id = 424242
    execution.containment_id = containment_id
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(ExecutionStatus.LOST)
    await executions.create_if_absent(execution)
    return execution


async def test_terminal_supervisor_stops_detached_matching_containment(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    manager = FakeKernelContainmentManager(tmp_path / "containment")
    containment = await manager.prepare("terminal:detached-contained")
    await _persist_detached_terminal(
        executions,
        terminals,
        session_id="detached-contained",
        containment_id=containment.identifier,
    )
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        platform_name="posix",
        containment_manager=manager,
        autodetect_containment=False,
    )

    closed = await supervisor.close("detached-contained")
    persisted = await executions.get("execution-detached-contained")

    assert closed.status is TerminalStatus.CLOSED
    assert persisted is not None and persisted.status is ExecutionStatus.CANCELLED
    assert not containment.path.exists()


async def test_terminal_supervisor_stops_containment_when_terminal_row_is_missing(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    manager = FakeKernelContainmentManager(tmp_path / "containment")
    containment = await manager.prepare("terminal:missing-terminal-row")
    execution = await _persist_detached_execution_without_terminal(
        executions,
        session_id="missing-terminal-row",
        containment_id=containment.identifier,
    )
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        platform_name="posix",
        containment_manager=manager,
        autodetect_containment=False,
    )

    closed = await supervisor.close_execution(execution.id)

    assert closed.status is ExecutionStatus.CANCELLED
    assert await terminals.get_by_execution(execution.id) is None
    assert not containment.path.exists()


async def test_detached_containment_close_survives_caller_cancellation(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    manager = _BlockingContainmentManager(tmp_path / "containment")
    containment = await manager.prepare("terminal:shielded-detached-close")
    execution = await _persist_detached_execution_without_terminal(
        executions,
        session_id="shielded-detached-close",
        containment_id=containment.identifier,
    )
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        platform_name="posix",
        containment_manager=manager,  # type: ignore[arg-type]
        autodetect_containment=False,
    )

    first_close = asyncio.create_task(supervisor.close_execution(execution.id))
    await containment.terminate_started.wait()
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    containment.release_terminate.set()
    closed = await supervisor.close_execution(execution.id)

    assert closed.status is ExecutionStatus.CANCELLED
    assert containment.terminate_calls == 1
    assert not containment.delegate.path.exists()


async def test_terminal_supervisor_missing_row_does_not_infer_stop_from_missing_leaf(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    manager = FakeKernelContainmentManager(tmp_path / "containment")
    containment = await manager.prepare("terminal:missing-row-and-leaf")
    containment_id = containment.identifier
    await containment.cleanup()
    execution = await _persist_detached_execution_without_terminal(
        executions,
        session_id="missing-row-and-leaf",
        containment_id=containment_id,
    )
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        platform_name="posix",
        containment_manager=manager,
        autodetect_containment=False,
    )

    with pytest.raises(ProcessTerminationError, match="containment .* is missing"):
        await supervisor.close_execution(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None and persisted.status is ExecutionStatus.LOST


async def test_terminal_supervisor_missing_row_without_containment_fails_closed(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    execution = Execution(
        id="execution-no-terminal-containment",
        execution_key="terminal:no-terminal-containment",
        run_id="run-pty-containment",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["fake-shell"],
        cwd="/tmp",
        stdout_path="/tmp/no-terminal-containment.log",
        stderr_path="/tmp/no-terminal-containment.log",
    )
    execution.transition_to(ExecutionStatus.STARTING)
    await executions.create_if_absent(execution)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        platform_name="posix",
        containment_manager=FakeKernelContainmentManager(tmp_path / "containment"),
        autodetect_containment=False,
    )

    with pytest.raises(ProcessTerminationError, match="terminal state is missing"):
        await supervisor.close_execution(execution.id)

    persisted = await executions.get(execution.id)
    assert persisted is not None and persisted.status is ExecutionStatus.STARTING


@pytest.mark.parametrize("boundary_state", ["missing", "replaced"])
async def test_terminal_containment_uncertainty_preserves_active_recovery_state(
    tmp_path: Path,
    boundary_state: str,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    manager = FakeKernelContainmentManager(tmp_path / "containment")
    session_id = f"uncertain-{boundary_state}"
    execution_key = f"terminal:{session_id}"
    original = await manager.prepare(execution_key)
    persisted_identifier = original.identifier
    await original.cleanup()
    if boundary_state == "replaced":
        replacement = await manager.prepare(execution_key)
        assert replacement.identifier != persisted_identifier
        error = "different delegated root"
    else:
        error = "containment .* is missing"
    execution = Execution(
        id=f"execution-{session_id}",
        execution_key=execution_key,
        run_id="run-pty-containment",
        node_id="local",
        executor_type=ExecutorType.PTY,
        argv=["fake-shell"],
        cwd="/tmp",
        stdout_path="/tmp/uncertain-terminal.log",
        stderr_path="/tmp/uncertain-terminal.log",
    )
    execution.transition_to(ExecutionStatus.STARTING)
    execution.pid = 424242
    execution.process_group_id = 424242
    execution.containment_id = persisted_identifier
    execution.transition_to(ExecutionStatus.RUNNING)
    await executions.create_if_absent(execution)
    terminal = TerminalSession(
        id=session_id,
        run_id=execution.run_id,
        execution_id=execution.id,
    )
    terminal.transition_to(TerminalStatus.OPEN)
    await terminals.create(terminal)
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        platform_name="posix",
        containment_manager=manager,
        autodetect_containment=False,
    )

    recovered = await supervisor.recover()
    with pytest.raises(ProcessTerminationError, match=error):
        await supervisor.close_execution(execution.id)

    assert [item.id for item in recovered] == [terminal.id]
    persisted_execution = await executions.get(execution.id)
    persisted_terminal = await terminals.get(terminal.id)
    assert persisted_execution is not None
    assert persisted_execution.status is ExecutionStatus.RUNNING
    assert persisted_terminal is not None
    assert persisted_terminal.status is TerminalStatus.OPEN
    if boundary_state == "replaced":
        assert replacement.path.exists()


async def test_terminal_supervisor_does_not_infer_stop_from_missing_leaf(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    manager = FakeKernelContainmentManager(tmp_path / "containment")
    missing = await manager.prepare("terminal:missing-containment")
    missing_identifier = missing.identifier
    await missing.cleanup()
    await _persist_detached_terminal(
        executions,
        terminals,
        session_id="missing-containment",
        containment_id=missing_identifier,
    )
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        platform_name="posix",
        containment_manager=manager,
        autodetect_containment=False,
    )

    with pytest.raises(ProcessTerminationError, match="containment .* is missing"):
        await supervisor.close("missing-containment")

    execution = await executions.get("execution-missing-containment")
    terminal = await terminals.get("missing-containment")
    assert execution is not None and execution.status is ExecutionStatus.LOST
    assert terminal is not None and terminal.status is TerminalStatus.LOST


async def test_terminal_supervisor_require_containment_rejects_backend_before_start(
    tmp_path: Path,
) -> None:
    executions = FileExecutionRepository(tmp_path / "executions.json")
    terminals = FileTerminalRepository(tmp_path / "terminals.json")
    handle = _GatedRecordingHandle()
    supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=RunnerPaths(tmp_path / "state"),
        native_backend=_GatedRecordingBackend(handle),  # type: ignore[arg-type]
        platform_name="posix",
        autodetect_containment=False,
        require_containment=True,
    )

    with pytest.raises(ProcessStartError, match="no delegated cgroup v2 boundary"):
        await supervisor.start(_request(tmp_path, "fake-shell", session_id="containment-required"))

    assert await executions.list("run-pty-containment") == []
    assert not handle.activated.is_set()
