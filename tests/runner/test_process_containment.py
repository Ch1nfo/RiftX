from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import riftx.executors._cgroup_launcher as launcher_module
import riftx.executors.containment as containment_module
from riftx.domain import ExecutionStatus
from riftx.executors import DirectProcessExecutor, ProcessExecutionRequest, merge_environment
from riftx.executors.containment import (
    LinuxCgroupV2Containment,
    LinuxCgroupV2Manager,
    ProcessContainmentTerminationError,
    ProcessContainmentUnavailableError,
    _execution_digest,
)
from riftx.executors.process import (
    ProcessHandle,
    ProcessStartError,
    UnverifiedProcessTreeTerminationError,
    _target_environment_file,
    _trusted_launcher_environment,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_process.py"
LAUNCHER = Path(containment_module.__file__).with_name("_cgroup_launcher.py")


def _distinct_payload_uid() -> int:
    return 60000 if _runner_euid() != 60000 else 60001


def _runner_euid() -> int:
    return getattr(os, "geteuid", lambda: 0)()


def _runner_gid() -> int:
    return getattr(os, "getegid", lambda: 0)()


def test_linux_manager_requires_payload_uid_and_gid_as_a_pair(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="configured together"):
        LinuxCgroupV2Manager(tmp_path, verify_filesystem=False, payload_uid=60000)


@pytest.mark.skipif(os.name != "posix", reason="payload identity drop is POSIX-only")
def test_launcher_rejects_payload_uid_equal_to_runner_uid() -> None:
    with pytest.raises(RuntimeError, match="must differ"):
        launcher_module._drop_payload_identity(_runner_euid(), _runner_gid())


@pytest.mark.skipif(os.name != "posix", reason="payload identity drop is POSIX-only")
def test_launcher_fails_closed_when_payload_identity_drop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_setgroups(_groups: list[int]) -> None:
        raise PermissionError("setgroups denied")

    monkeypatch.setattr(launcher_module.os, "setgroups", deny_setgroups)
    with pytest.raises(PermissionError, match="setgroups denied"):
        launcher_module._drop_payload_identity(_distinct_payload_uid(), _runner_gid())


def test_launcher_rejects_writable_ancestor_cgroup_procs(tmp_path: Path) -> None:
    writable = tmp_path / "cgroup.procs"
    writable.write_text("", encoding="ascii")

    with pytest.raises(RuntimeError, match="retains .* access"):
        launcher_module._verify_ancestor_migration_denied((writable,))


def test_launcher_rejects_residual_payload_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        launcher_module,
        "_read_self_status",
        lambda: "CapInh:\t0000000000000000\n"
        "CapPrm:\t0000000000000000\n"
        "CapEff:\t0000000000000001\n"
        "CapAmb:\t0000000000000000\n",
    )

    with pytest.raises(RuntimeError, match="CapEff"):
        launcher_module._verify_capabilities_cleared()


def _request(tmp_path: Path, *argv: str, execution_key: str = "contained-test") -> Any:
    return ProcessExecutionRequest(
        execution_key=execution_key,
        argv=list(argv),
        cwd=tmp_path,
        env=merge_environment(),
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )


def _fake_cgroup(path: Path, *, populated: str = "0") -> None:
    path.mkdir(exist_ok=True)
    (path / "cgroup.events").write_text(f"populated {populated}\n", encoding="ascii")
    (path / "cgroup.procs").write_text("", encoding="ascii")
    (path / "cgroup.kill").write_text("", encoding="ascii")
    (path / "cgroup.max.descendants").write_text("max\n", encoding="ascii")


async def _socket_line(control: socket.socket) -> bytes:
    payload = bytearray()
    loop = asyncio.get_running_loop()
    while True:
        chunk = await loop.sock_recv(control, 512)
        if not chunk:
            return bytes(payload)
        payload.extend(chunk)
        newline = payload.find(b"\n")
        if newline >= 0:
            return bytes(payload[:newline])


async def _start_launcher(
    cgroup: Path,
    target: list[str],
) -> tuple[asyncio.subprocess.Process, socket.socket]:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    parent.setblocking(False)
    child.set_inheritable(True)
    environment_file = _target_environment_file(dict(os.environ))
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            str(LAUNCHER),
            "--cgroup",
            str(cgroup),
            "--control-fd",
            str(child.fileno()),
            "--target-env-fd",
            str(environment_file.fileno()),
            "--",
            *target,
            env=_trusted_launcher_environment(),
            pass_fds=(child.fileno(), environment_file.fileno()),
            start_new_session=True,
        )
    finally:
        environment_file.close()
    child.close()
    return process, parent


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_file(path: Path, *, timeout_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {path}")


def _wait_for_pid_exit(pid: int, *, timeout_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for pid {pid}")


class _FilesystemLauncherContainment:
    def __init__(self, path: Path) -> None:
        self.delegate = LinuxCgroupV2Containment(path=path, digest="f" * 64)
        self.terminated = False
        self.cleaned = False

    @property
    def identifier(self) -> str:
        return self.delegate.identifier

    def launcher_argv(
        self,
        target_argv: list[str],
        *,
        control_fd: int,
        target_env_fd: int,
    ) -> list[str]:
        return self.delegate.launcher_argv(
            target_argv,
            control_fd=control_fd,
            target_env_fd=target_env_fd,
        )

    async def wait_empty(self, timeout_seconds: float | None) -> bool:
        return True

    async def terminate(self, *, grace_seconds: float) -> None:
        self.terminated = True

    async def force_terminate(self, *, confirmation_seconds: float) -> None:
        self.terminated = True

    async def cleanup(self) -> None:
        self.cleaned = True


class _StaticManager:
    def __init__(self, containment: Any) -> None:
        self.containment = containment

    async def prepare(self, execution_key: str) -> Any:
        return self.containment


def test_cgroup_leaf_uses_full_digest_and_never_embeds_execution_key(tmp_path: Path) -> None:
    dangerous = "../../run/with spaces/\x00-not-a-path"
    manager = LinuxCgroupV2Manager(tmp_path, verify_filesystem=False)
    containment = manager.containment_for(dangerous)

    assert containment.path.parent == tmp_path
    assert containment.path.name == f"riftx-{_execution_digest(dangerous)}"
    assert len(containment.path.name) == len("riftx-") + 64
    assert dangerous not in containment.path.name
    assert manager.containment_for(dangerous).path == containment.path
    assert manager.containment_for(dangerous + "x").path != containment.path


async def test_manager_rejects_leaf_without_cgroup_kill(tmp_path: Path) -> None:
    root = tmp_path / "delegated"
    root.mkdir()
    (root / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    (root / "cgroup.procs").write_text("", encoding="ascii")
    manager = LinuxCgroupV2Manager(
        root,
        verify_filesystem=False,
        payload_uid=_distinct_payload_uid(),
        payload_gid=_runner_gid(),
    )

    with pytest.raises(ProcessContainmentUnavailableError, match="cgroup.kill"):
        await manager.prepare("missing-kill")

    assert not manager.containment_for("missing-kill").path.exists()


async def test_manager_prepares_leaf_and_disables_nested_cgroups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "delegated"
    root.mkdir()
    (root / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    (root / "cgroup.procs").write_text("", encoding="ascii")
    original_mkdir = Path.mkdir

    def materialize_controls(path: Path, *args: Any, **kwargs: Any) -> None:
        original_mkdir(path, *args, **kwargs)
        if path.parent == root and path.name.startswith("riftx-"):
            (path / "cgroup.events").write_text("populated 0\n", encoding="ascii")
            (path / "cgroup.procs").write_text("", encoding="ascii")
            (path / "cgroup.kill").write_text("", encoding="ascii")
            (path / "cgroup.max.descendants").write_text("max\n", encoding="ascii")

    monkeypatch.setattr(Path, "mkdir", materialize_controls)
    containment = await LinuxCgroupV2Manager(
        root,
        verify_filesystem=False,
        payload_uid=_distinct_payload_uid(),
        payload_gid=_runner_gid(),
    ).prepare("safe")

    assert containment.identifier.endswith(f":{_execution_digest('safe')}")
    assert (containment.path / "cgroup.max.descendants").read_text().startswith("0\n")


@pytest.mark.skipif(os.name != "posix", reason="launcher uses inherited POSIX descriptors")
async def test_launcher_parent_eof_never_executes_target(tmp_path: Path) -> None:
    cgroup = tmp_path / "fake-cgroup"
    _fake_cgroup(cgroup)
    marker = tmp_path / "must-not-run"
    target = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]
    process, control = await _start_launcher(cgroup, target)

    assert await _socket_line(control) == b"READY"
    control.close()
    assert await process.wait() == 125
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="launcher uses inherited POSIX descriptors")
async def test_launcher_executes_only_after_exact_activation(tmp_path: Path) -> None:
    cgroup = tmp_path / "fake-cgroup"
    _fake_cgroup(cgroup)
    marker = tmp_path / "ran"
    target = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]
    process, control = await _start_launcher(cgroup, target)

    assert await _socket_line(control) == b"READY"
    await asyncio.sleep(0.05)
    assert not marker.exists()
    await asyncio.get_running_loop().sock_sendall(control, b"\x01")
    assert await asyncio.get_running_loop().sock_recv(control, 1) == b""
    control.close()
    assert await process.wait() == 0
    assert marker.read_text() == "ran"


@pytest.mark.skipif(os.name != "posix", reason="launcher uses inherited POSIX descriptors")
async def test_executor_deferred_activation_blocks_target_side_effects(tmp_path: Path) -> None:
    cgroup = tmp_path / "fake-cgroup"
    _fake_cgroup(cgroup)
    containment = _FilesystemLauncherContainment(cgroup)
    executor = DirectProcessExecutor(
        _StaticManager(containment),
        autodetect_containment=False,
        require_containment=True,
        defer_activation=True,
    )
    marker = tmp_path / "effect"
    request = _request(
        tmp_path,
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    )
    handle = await executor.start(request)
    try:
        await asyncio.sleep(0.05)
        assert handle.activation_pending is True
        assert not marker.exists()
        with pytest.raises(ProcessStartError, match="still gated"):
            await handle.wait()

        await handle.activate()
        result = await handle.wait()
        assert result.status is ExecutionStatus.EXITED
        assert marker.read_text() == "ran"
        await handle.cleanup_confirmed_containment()
    finally:
        if handle.process.returncode is None:
            handle.process.kill()
            await handle.process.wait()


@pytest.mark.skipif(os.name != "posix", reason="launcher uses inherited POSIX descriptors")
async def test_gated_launcher_does_not_load_target_python_environment_before_activation(
    tmp_path: Path,
) -> None:
    cgroup = tmp_path / "fake-cgroup"
    _fake_cgroup(cgroup)
    site_directory = tmp_path / "target-python-path"
    site_directory.mkdir()
    site_marker = tmp_path / "sitecustomize-ran"
    target_marker = tmp_path / "target-environment"
    (site_directory / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(site_marker)!r}).write_text('loaded')\n",
        encoding="utf-8",
    )
    request = _request(
        tmp_path,
        sys.executable,
        "-c",
        "import os; from pathlib import Path; "
        f"Path({str(target_marker)!r}).write_text(os.environ['PAYLOAD_ENV_VALUE'])",
    )
    request.env = merge_environment(
        {
            "PYTHONPATH": str(site_directory),
            "PAYLOAD_ENV_VALUE": "preserved-after-activation",
        }
    )
    handle = await DirectProcessExecutor(
        _StaticManager(_FilesystemLauncherContainment(cgroup)),
        autodetect_containment=False,
        require_containment=True,
        defer_activation=True,
    ).start(request)

    try:
        await asyncio.sleep(0.05)
        assert not site_marker.exists()
        assert not target_marker.exists()

        await handle.activate()
        assert (await handle.wait()).status is ExecutionStatus.EXITED
        assert site_marker.read_text() == "loaded"
        assert target_marker.read_text() == "preserved-after-activation"
        await handle.cleanup_confirmed_containment()
    finally:
        if handle.process.returncode is None:
            handle.process.kill()
            await handle.process.wait()


@pytest.mark.skipif(os.name != "posix", reason="launcher uses inherited POSIX descriptors")
async def test_uncontained_posix_deferred_activation_still_gates_target(tmp_path: Path) -> None:
    executor = DirectProcessExecutor(
        autodetect_containment=False,
        defer_activation=True,
    )
    marker = tmp_path / "uncontained-effect"
    handle = await executor.start(
        _request(
            tmp_path,
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        )
    )

    await asyncio.sleep(0.05)
    assert handle.containment_identifier is None
    assert handle.activation_pending is True
    assert not marker.exists()
    await handle.activate()
    with pytest.raises(UnverifiedProcessTreeTerminationError, match="ended naturally"):
        await handle.wait()
    assert marker.read_text() == "ran"


async def test_required_containment_rejects_start_without_backend(tmp_path: Path) -> None:
    executor = DirectProcessExecutor(
        autodetect_containment=False,
        require_containment=True,
    )

    with pytest.raises(ProcessStartError, match="kernel-backed containment backend"):
        await executor.start(_request(tmp_path, sys.executable, "-c", "pass"))


@pytest.mark.skipif(os.name != "posix", reason="Linux payload identity semantics")
async def test_optional_unsafe_real_cgroup_manager_runs_truly_uncontained(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "optional-uncontained"
    executor = DirectProcessExecutor(
        LinuxCgroupV2Manager(tmp_path, verify_filesystem=False),
        autodetect_containment=False,
        require_containment=False,
    )

    assert executor.containment_manager is None
    handle = await executor.start(
        _request(
            tmp_path,
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        )
    )
    assert handle.containment_identifier is None
    with pytest.raises(UnverifiedProcessTreeTerminationError, match="ended naturally"):
        await handle.wait()
    assert marker.read_text() == "ran"


@pytest.mark.skipif(os.name != "posix", reason="Linux payload identity semantics")
async def test_required_real_cgroup_manager_rejects_missing_payload_identity(
    tmp_path: Path,
) -> None:
    executor = DirectProcessExecutor(
        LinuxCgroupV2Manager(tmp_path, verify_filesystem=False),
        autodetect_containment=False,
        require_containment=True,
    )

    assert executor.containment_manager is None
    with pytest.raises(ProcessStartError, match="payload_uid and payload_gid"):
        await executor.start(_request(tmp_path, sys.executable, "-c", "pass"))


@pytest.mark.skipif(os.name != "posix", reason="Linux payload identity semantics")
async def test_required_real_cgroup_manager_rejects_runner_uid_as_payload_uid(
    tmp_path: Path,
) -> None:
    executor = DirectProcessExecutor(
        LinuxCgroupV2Manager(
            tmp_path,
            verify_filesystem=False,
            payload_uid=_runner_euid(),
            payload_gid=_runner_gid(),
        ),
        autodetect_containment=False,
        require_containment=True,
    )

    with pytest.raises(ProcessStartError, match="differ from the Runner"):
        await executor.start(_request(tmp_path, sys.executable, "-c", "pass"))


@pytest.mark.skipif(os.name != "posix", reason="launcher uses inherited POSIX descriptors")
async def test_activation_reports_exec_failure_before_returning_handle(tmp_path: Path) -> None:
    cgroup = tmp_path / "fake-cgroup"
    _fake_cgroup(cgroup)
    containment = _FilesystemLauncherContainment(cgroup)
    executor = DirectProcessExecutor(
        _StaticManager(containment),
        autodetect_containment=False,
    )

    with pytest.raises(ProcessStartError, match="EXEC_ERROR"):
        await executor.start(_request(tmp_path, str(tmp_path / "missing-executable")))

    assert containment.terminated is True
    assert containment.cleaned is True


async def test_cgroup_kill_failure_is_never_stop_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup = tmp_path / "cgroup"
    _fake_cgroup(cgroup, populated="1")
    (cgroup / "cgroup.kill").unlink()
    containment = LinuxCgroupV2Containment(path=cgroup, digest="a" * 64, poll_seconds=0.001)
    monkeypatch.setattr(containment_module, "_DEFAULT_CONFIRMATION_SECONDS", 0.01)

    with pytest.raises(ProcessContainmentTerminationError, match="cgroup.kill failed"):
        await containment.force_terminate(confirmation_seconds=0.001)


async def test_populated_must_clear_after_cgroup_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup = tmp_path / "cgroup"
    _fake_cgroup(cgroup, populated="1")
    containment = LinuxCgroupV2Containment(path=cgroup, digest="b" * 64, poll_seconds=0.001)
    monkeypatch.setattr(containment_module, "_DEFAULT_CONFIRMATION_SECONDS", 0.01)

    with pytest.raises(ProcessContainmentTerminationError, match="remains populated"):
        await containment.force_terminate(confirmation_seconds=0.001)

    assert (cgroup / "cgroup.kill").read_text().startswith("1\n")


async def test_cgroup_kill_confirms_only_after_populated_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup = tmp_path / "cgroup"
    _fake_cgroup(cgroup, populated="1")
    containment = LinuxCgroupV2Containment(path=cgroup, digest="c" * 64, poll_seconds=0.001)

    def kill_and_empty(self: LinuxCgroupV2Containment) -> None:
        (self.path / "cgroup.kill").write_text("1\n", encoding="ascii")
        (self.path / "cgroup.events").write_text("populated 0\n", encoding="ascii")

    monkeypatch.setattr(LinuxCgroupV2Containment, "_write_kill", kill_and_empty)
    await containment.force_terminate(confirmation_seconds=0.01)

    assert containment._confirmed_empty is True


async def test_terminate_uses_only_authoritative_cgroup_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup = tmp_path / "cgroup"
    _fake_cgroup(cgroup, populated="1")
    containment = LinuxCgroupV2Containment(path=cgroup, digest="d" * 64, poll_seconds=0.001)

    def kill_and_empty(self: LinuxCgroupV2Containment) -> None:
        (self.path / "cgroup.kill").write_text("1\n", encoding="ascii")
        (self.path / "cgroup.events").write_text("populated 0\n", encoding="ascii")

    def unexpected_pid_signal(*_args: object) -> None:
        raise AssertionError("numeric PID signalling must not be used for cgroup stop")

    monkeypatch.setattr(containment_module.os, "kill", unexpected_pid_signal)
    monkeypatch.setattr(LinuxCgroupV2Containment, "_write_kill", kill_and_empty)

    await containment.terminate(grace_seconds=0.01)
    assert containment._confirmed_empty is True


class _DelayedContainment:
    identifier = "test-delayed-containment"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned = False

    async def terminate(self, *, grace_seconds: float) -> None:
        self.started.set()
        await self.release.wait()

    async def wait_empty(self, timeout_seconds: float | None) -> bool:
        return True

    async def force_terminate(self, *, confirmation_seconds: float) -> None:
        self.started.set()
        await self.release.wait()

    async def cleanup(self) -> None:
        self.cleaned = True

    def launcher_argv(
        self,
        target_argv: list[str],
        *,
        control_fd: int,
        target_env_fd: int,
    ) -> list[str]:
        raise AssertionError("not used")


async def test_cancelled_cancel_waits_for_stop_but_defers_containment_cleanup(
    tmp_path: Path,
) -> None:
    containment = _DelayedContainment()
    process = SimpleNamespace(pid=43123, returncode=-9, wait=AsyncMock(return_value=-9))
    handle = ProcessHandle(
        process=process,
        request=_request(tmp_path, sys.executable, "-c", "pass"),
        started_at=datetime.now(UTC),
        containment=containment,
    )

    cancellation = asyncio.create_task(handle.cancel(termination_grace_seconds=0.01))
    await containment.started.wait()
    cancellation.cancel()
    await asyncio.sleep(0)
    assert not cancellation.done()

    containment.release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancellation
    assert containment.cleaned is False

    await handle.cleanup_confirmed_containment()
    assert containment.cleaned is True


@pytest.mark.skipif(os.name != "posix", reason="setsid and double-fork are POSIX-specific")
async def test_uncontained_cancel_fails_closed_when_setsid_child_escapes(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "escaped-heartbeat"
    pid_file = tmp_path / "escaped.pid"
    handle = await DirectProcessExecutor(autodetect_containment=False).start(
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
        )
    )
    escaped_pid: int | None = None
    try:
        await asyncio.to_thread(_wait_for_file, heartbeat)
        await asyncio.to_thread(_wait_for_file, pid_file)
        escaped_pid = int(pid_file.read_text())

        with pytest.raises(UnverifiedProcessTreeTerminationError, match="cannot be proven"):
            await handle.cancel(termination_grace_seconds=0.05)
        size_after_best_effort = heartbeat.stat().st_size
        await asyncio.sleep(0.15)

        assert _pid_exists(escaped_pid)
        assert heartbeat.stat().st_size > size_after_best_effort
    finally:
        if escaped_pid is not None and _pid_exists(escaped_pid):
            os.kill(escaped_pid, signal.SIGKILL)


@pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or not os.environ.get("RIFTX_TEST_CGROUP_V2_ROOT")
    or not os.environ.get("RIFTX_TEST_PAYLOAD_UID")
    or not os.environ.get("RIFTX_TEST_PAYLOAD_GID"),
    reason="requires delegated cgroup v2 and a distinct payload uid/gid",
)
async def test_real_cgroup_cancel_contains_setsid_double_fork(tmp_path: Path) -> None:
    root = Path(os.environ["RIFTX_TEST_CGROUP_V2_ROOT"])
    manager = LinuxCgroupV2Manager(
        root,
        payload_uid=int(os.environ["RIFTX_TEST_PAYLOAD_UID"]),
        payload_gid=int(os.environ["RIFTX_TEST_PAYLOAD_GID"]),
    )
    await asyncio.to_thread(tmp_path.chmod, 0o777)
    heartbeat = tmp_path / "heartbeat"
    pid_file = tmp_path / "escaped.pid"
    execution_key = f"setsid-double-fork:{tmp_path}"
    executor = DirectProcessExecutor(
        manager,
        autodetect_containment=False,
        require_containment=True,
    )
    handle = await executor.start(
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
            execution_key=execution_key,
        )
    )
    escaped_pid: int | None = None
    try:
        await asyncio.to_thread(_wait_for_file, heartbeat)
        await asyncio.to_thread(_wait_for_file, pid_file)
        escaped_pid = int(pid_file.read_text())

        result = await handle.cancel(termination_grace_seconds=0.1)
        heartbeat_size = heartbeat.stat().st_size
        await asyncio.to_thread(_wait_for_pid_exit, escaped_pid)
        await asyncio.sleep(0.15)

        assert result.status is ExecutionStatus.CANCELLED
        assert heartbeat.stat().st_size == heartbeat_size
        await handle.cleanup_confirmed_containment()
        assert not manager.containment_for(execution_key).path.exists()
    finally:
        if escaped_pid is not None and _pid_exists(escaped_pid):
            os.kill(escaped_pid, signal.SIGKILL)
