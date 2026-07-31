"""Unix pseudo-terminal backend."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import shutil
import signal
import socket
import struct
import sys
import termios
from pathlib import Path
from typing import BinaryIO

from riftx.executors.containment import (
    LinuxCgroupV2Manager,
    ProcessContainment,
    ProcessContainmentError,
    ProcessContainmentManager,
)
from riftx.executors.process import (
    ProcessStartError,
    UnverifiedProcessTreeTerminationError,
    _ActivationGate,
    _complete_task_uninterruptibly,
    _read_launcher_readiness,
    _target_environment_file,
    _terminate_posix_process_group,
    _trusted_launcher_environment,
    _wait_for_posix_process_group_exit,
)

from .models import TerminalLaunchRequest
from .terminal_backend import UnconfirmedTerminalStartError


class UnixPTYHandle:
    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process,
        master_fd: int,
        transcript_path: Path,
        containment: ProcessContainment | None,
        activation_gate: _ActivationGate,
    ) -> None:
        self.process = process
        self.master_fd = master_fd
        self.containment = containment
        self._activation_gate = activation_gate
        self._write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(
            _pump_output(master_fd, transcript_path),
            name=f"riftx-unix-pty-reader-{process.pid}",
        )

    @property
    def pid(self) -> int:
        if self.process.pid is None:
            raise RuntimeError("PTY child does not have a pid")
        return self.process.pid

    @property
    def process_group_id(self) -> int:
        return self.pid

    @property
    def containment_identifier(self) -> str | None:
        if self.containment is None:
            return None
        return self.containment.identifier

    @property
    def activation_pending(self) -> bool:
        return not self._activation_gate.released

    async def activate(self) -> None:
        await self._activation_gate.release()

    async def abort_gated_start(
        self,
        *,
        confirmation_seconds: float = 0.5,
        cleanup_containment: bool = False,
    ) -> bool:
        gate = self._activation_gate
        if gate.exec_confirmed:
            return False
        # Once activation was sent without an explicit exec failure, target
        # execution is uncertain and normal containment termination is required.
        if gate.released and not gate.exec_failed:
            return False
        if not gate.released:
            gate.abort()

        async def abort_and_confirm() -> None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=confirmation_seconds)
            except TimeoutError as exc:
                raise ProcessStartError(
                    "gated PTY launcher did not exit after activation denial"
                ) from exc
            if self.containment is not None:
                await self.containment.force_terminate(
                    confirmation_seconds=confirmation_seconds
                )
                if cleanup_containment:
                    await self.cleanup_confirmed_containment()

        abort_task = asyncio.create_task(
            abort_and_confirm(),
            name=f"riftx-gated-pty-abort-{self.pid}",
        )
        _, interrupted = await _complete_task_uninterruptibly(abort_task)
        if interrupted:
            raise asyncio.CancelledError
        return True

    async def write(self, data: bytes) -> None:
        if not data:
            return
        async with self._write_lock:
            await asyncio.to_thread(os.write, self.master_fd, data)

    async def resize(self, cols: int, rows: int) -> None:
        await asyncio.to_thread(_set_window_size, self.master_fd, cols, rows)
        if self.process.returncode is None:
            _signal_process_group(self.pid, signal.SIGWINCH)

    async def interrupt(self) -> None:
        if self.process.returncode is None:
            _signal_process_group(self.pid, signal.SIGINT)

    async def terminate(
        self,
        grace_seconds: float,
        *,
        cleanup_containment: bool = False,
    ) -> None:
        if await self.abort_gated_start(
            confirmation_seconds=max(grace_seconds, 0.5)
        ):
            return

        if self.containment is not None:
            termination = asyncio.create_task(
                self._terminate_contained(
                    grace_seconds,
                    cleanup_containment=cleanup_containment,
                ),
                name=f"riftx-pty-containment-stop-{self.pid}",
            )
            _, interrupted = await _complete_task_uninterruptibly(termination)
            if interrupted:
                raise asyncio.CancelledError
            return

        # PGID cleanup is useful but is not complete-tree evidence: a PTY child
        # can setsid()/double-fork away.  Always fail closed after best effort.
        leader_wait = asyncio.create_task(self.process.wait())
        try:
            await _terminate_posix_process_group(
                self.pid,
                grace_seconds=grace_seconds,
            )
        except BaseException:
            leader_wait.cancel()
            await asyncio.gather(leader_wait, return_exceptions=True)
            raise
        await leader_wait
        raise UnverifiedProcessTreeTerminationError(
            f"PTY process group {self.process_group_id!r} stopped, but complete "
            "descendant absence cannot be proven without kernel containment"
        )

    async def wait(self, *, cleanup_containment: bool = False) -> int:
        exit_code = await self.process.wait()
        gate = self._activation_gate
        target_may_have_executed = gate.exec_confirmed or (
            gate.released and not gate.exec_failed
        )
        if not target_may_have_executed:
            if self.containment is not None:
                await self.containment.wait_empty(timeout_seconds=None)
                if cleanup_containment:
                    await self.cleanup_confirmed_containment()
            return exit_code
        if self.containment is not None:
            # Leader exit is not terminal completion.  Only populated=0 on the
            # cgroup ownership boundary proves daemonized descendants are gone.
            await self.containment.wait_empty(timeout_seconds=None)
            if cleanup_containment:
                await self.cleanup_confirmed_containment()
            return exit_code

        await _wait_for_posix_process_group_exit(
            self.process_group_id,
            timeout_seconds=None,
        )
        raise UnverifiedProcessTreeTerminationError(
            f"PTY process group {self.process_group_id!r} ended naturally, but "
            "complete descendant absence cannot be proven without kernel containment"
        )

    async def cleanup_confirmed_containment(self) -> None:
        """Remove an empty containment only after durable stop proof is saved."""

        containment = self.containment
        if containment is None:
            return
        cleanup = asyncio.create_task(
            containment.cleanup(),
            name=f"riftx-pty-containment-cleanup-{self.pid}",
        )
        _, interrupted = await _complete_task_uninterruptibly(cleanup)
        if interrupted:
            raise asyncio.CancelledError

    async def _terminate_contained(
        self,
        grace_seconds: float,
        *,
        cleanup_containment: bool,
    ) -> None:
        containment = self.containment
        if containment is None:
            raise RuntimeError("contained PTY termination requires a containment")
        await containment.terminate(grace_seconds=grace_seconds)
        try:
            await asyncio.wait_for(
                self.process.wait(),
                timeout=max(grace_seconds, 0.5),
            )
        except TimeoutError as exc:
            raise UnverifiedProcessTreeTerminationError(
                f"PTY containment {containment.identifier!r} is empty, but its "
                "leader could not be reaped"
            ) from exc
        if cleanup_containment:
            await self.cleanup_confirmed_containment()

    async def close_output(self) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self._reader_task), timeout=1.0)
        except TimeoutError:
            _safe_close(self.master_fd)
            await asyncio.gather(self._reader_task, return_exceptions=True)
        else:
            _safe_close(self.master_fd)


class UnixPTYBackend:
    def __init__(
        self,
        containment_manager: ProcessContainmentManager | None = None,
        *,
        autodetect_containment: bool = True,
        require_containment: bool = False,
        launcher_ready_timeout_seconds: float = 5.0,
    ) -> None:
        if launcher_ready_timeout_seconds <= 0:
            raise ValueError("launcher ready timeout must be positive")
        if containment_manager is None and autodetect_containment:
            containment_manager = LinuxCgroupV2Manager.autodetect()
        containment_configuration_error: ProcessContainmentError | None = None
        if isinstance(containment_manager, LinuxCgroupV2Manager):
            try:
                containment_manager.require_distinct_payload_identity()
            except ProcessContainmentError as exc:
                containment_configuration_error = exc
                containment_manager = None
        self._containment_manager = containment_manager
        self._containment_configuration_error = containment_configuration_error
        self._require_containment = require_containment
        self._launcher_ready_timeout_seconds = launcher_ready_timeout_seconds

    @property
    def containment_manager(self) -> ProcessContainmentManager | None:
        return self._containment_manager

    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        transcript_path: Path,
        environment: dict[str, str],
    ) -> UnixPTYHandle:
        _validate_target(request.argv[0], request.cwd, environment)
        if request.session_id is None:
            raise ProcessStartError("Unix PTY launch requires a durable session id")
        execution_key = f"terminal:{request.session_id}"
        containment: ProcessContainment | None = None
        if self._require_containment and self._containment_configuration_error is not None:
            raise ProcessStartError(
                f"PTY kernel containment is unsafe for {execution_key!r}: "
                f"{self._containment_configuration_error}"
            )
        if self._containment_manager is not None:
            try:
                containment = await self._containment_manager.prepare(execution_key)
            except (OSError, ValueError, ProcessContainmentError) as exc:
                raise ProcessStartError(
                    f"failed to prepare PTY kernel containment for {execution_key!r}: {exc}"
                ) from exc
        elif self._require_containment:
            raise ProcessStartError(
                f"kernel process containment is required for {execution_key!r}, "
                "but no delegated cgroup v2 backend is available"
            )

        master_fd: int | None = None
        slave_fd: int | None = None
        parent_socket: socket.socket | None = None
        child_socket: socket.socket | None = None
        process: asyncio.subprocess.Process | None = None
        target_env_file: BinaryIO | None = None
        try:
            master_fd, slave_fd = pty.openpty()
            parent_socket, child_socket = _open_control_socketpair()
            parent_socket.setblocking(False)
            child_socket.set_inheritable(True)
            _set_window_size(master_fd, request.cols, request.rows)
            target_env_file = _target_environment_file(environment)
            launch_argv = [
                sys.executable,
                "-I",
                "-S",
                str(Path(__file__).with_name("_pty_child.py")),
                "--control-fd",
                str(child_socket.fileno()),
                "--target-env-fd",
                str(target_env_file.fileno()),
            ]
            if containment is not None:
                launch_argv.extend(
                    ["--cgroup", str(containment.launcher_membership_path)]
                )
                payload_uid = getattr(containment, "payload_uid", None)
                payload_gid = getattr(containment, "payload_gid", None)
                if payload_uid is not None and payload_gid is not None:
                    launch_argv.extend(
                        [
                            "--delegated-root",
                            str(containment.launcher_membership_path.parent),
                            "--payload-uid",
                            str(payload_uid),
                            "--payload-gid",
                            str(payload_gid),
                        ]
                    )
            launch_argv.extend(["--", *request.argv])
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *launch_argv,
                    cwd=request.cwd,
                    env=_trusted_launcher_environment(),
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    pass_fds=(child_socket.fileno(), target_env_file.fileno()),
                    start_new_session=True,
                )
            )
            process, spawn_interrupted = await _complete_task_uninterruptibly(spawn)
            child_socket.close()
            _safe_close(slave_fd)
            slave_fd = None
            if spawn_interrupted:
                raise asyncio.CancelledError

            readiness = asyncio.create_task(
                _read_launcher_readiness(
                    parent_socket,
                    timeout_seconds=self._launcher_ready_timeout_seconds,
                )
            )
            ready_line, ready_interrupted = await _complete_task_uninterruptibly(readiness)
            if ready_interrupted:
                raise asyncio.CancelledError
            if ready_line != b"READY":
                detail = (
                    ready_line.decode("utf-8", errors="replace")
                    or "launcher closed control"
                )
                raise ProcessStartError(
                    f"PTY launcher failed for {execution_key!r}: {detail}"
                )

            return UnixPTYHandle(
                process=process,
                master_fd=master_fd,
                transcript_path=transcript_path,
                containment=containment,
                activation_gate=_ActivationGate(
                    parent_socket,
                    confirmation_timeout_seconds=self._launcher_ready_timeout_seconds,
                ),
            )
        except BaseException as exc:
            if parent_socket is not None:
                parent_socket.close()
            if child_socket is not None:
                child_socket.close()
            if slave_fd is not None:
                _safe_close(slave_fd)
            cleanup = asyncio.create_task(
                _cleanup_failed_pty_start(process, containment),
                name=f"riftx-gated-pty-cleanup-{execution_key}",
            )
            try:
                _, cleanup_interrupted = await _complete_task_uninterruptibly(cleanup)
            except BaseException as cleanup_exc:
                if process is not None and master_fd is not None and parent_socket is not None:
                    handle = UnixPTYHandle(
                        process=process,
                        master_fd=master_fd,
                        transcript_path=transcript_path,
                        containment=containment,
                        activation_gate=_ActivationGate(
                            parent_socket,
                            confirmation_timeout_seconds=(
                                self._launcher_ready_timeout_seconds
                            ),
                        ),
                    )
                    raise UnconfirmedTerminalStartError(
                        f"spawned PTY launcher for {execution_key!r} could not be safely "
                        f"cleaned up: {cleanup_exc}",
                        handle=handle,
                        start_error=exc,
                        cleanup_error=cleanup_exc,
                    ) from cleanup_exc
                if master_fd is not None:
                    _safe_close(master_fd)
                raise ProcessStartError(
                    f"failed to safely clean up PTY start {execution_key!r}: {cleanup_exc}"
                ) from exc
            if master_fd is not None:
                _safe_close(master_fd)
            if isinstance(exc, asyncio.CancelledError) or cleanup_interrupted:
                raise asyncio.CancelledError from exc
            if isinstance(exc, ProcessStartError):
                raise
            if isinstance(exc, (OSError, ValueError)):
                raise ProcessStartError(
                    f"failed to start Unix PTY {request.argv[0]!r}: {exc}"
                ) from exc
            raise
        finally:
            if target_env_file is not None:
                target_env_file.close()


async def _cleanup_failed_pty_start(
    process: asyncio.subprocess.Process | None,
    containment: ProcessContainment | None,
) -> None:
    if process is not None and process.returncode is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    if containment is not None:
        await containment.force_terminate(confirmation_seconds=0.5)
    if process is not None:
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError as exc:
            raise UnverifiedProcessTreeTerminationError(
                "PTY launcher could not be reaped after failed start"
            ) from exc
    if containment is not None:
        await containment.cleanup()


def _open_control_socketpair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


async def _pump_output(master_fd: int, transcript_path: Path) -> None:
    with transcript_path.open("ab", buffering=0) as transcript:
        while True:
            try:
                data = await asyncio.to_thread(os.read, master_fd, 64 * 1024)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    return
                raise
            if not data:
                return
            transcript.write(data)


def _validate_target(executable: str, cwd: Path, environment: dict[str, str]) -> None:
    if os.path.dirname(executable):
        path = Path(executable)
        if not path.is_absolute():
            path = cwd / path
        if not path.exists():
            raise FileNotFoundError(executable)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise PermissionError(executable)
        return
    if shutil.which(executable, path=environment.get("PATH")) is None:
        raise FileNotFoundError(executable)


def _set_window_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _signal_process_group(process_group_id: int, sig: signal.Signals) -> None:
    try:
        os.killpg(process_group_id, sig)
    except ProcessLookupError:
        return


def _safe_close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError as exc:
        if exc.errno != errno.EBADF:
            raise
