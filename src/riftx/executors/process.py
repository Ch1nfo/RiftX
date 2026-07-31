"""Direct host process executor."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from riftx.domain import ExecutionStatus

from .containment import (
    LinuxCgroupV2Manager,
    ProcessContainment,
    ProcessContainmentError,
    ProcessContainmentManager,
)
from .models import ProcessExecutionRequest, ProcessResult

_MIN_WINDOWS_TREE_CONFIRMATION_SECONDS = 0.5
_DEFAULT_LAUNCHER_READY_TIMEOUT_SECONDS = 5.0
_LAUNCHER_READY = b"READY"
_ACTIVATE = b"\x01"
_MAX_TARGET_ENVIRONMENT_BYTES = 4 * 1024 * 1024


class ProcessStartError(RuntimeError):
    """Raised when the operating system rejects process creation."""


class UnconfirmedProcessStartError(ProcessStartError):
    """Raised when a spawned launcher could not be proven safely cleaned up.

    ``handle`` is intentionally retained so the supervisor can durably record
    the exact PID/PGID/containment identity and retry physical termination.  A
    caller must never collapse this error into a pre-spawn ``FAILED`` row.
    """

    def __init__(
        self,
        message: str,
        *,
        handle: ProcessHandle,
        start_error: BaseException,
        cleanup_error: BaseException,
    ) -> None:
        super().__init__(message)
        self.handle = handle
        self.start_error = start_error
        self.cleanup_error = cleanup_error


class ProcessTreeTerminationError(RuntimeError):
    """Raised when an owned process tree cannot be proven stopped."""


class ProcessGroupTerminationError(ProcessTreeTerminationError):
    """Raised when a POSIX process group remains alive after forced termination."""


class UnverifiedProcessTreeTerminationError(ProcessTreeTerminationError):
    """Raised after best-effort cleanup when no complete-tree evidence exists."""


@dataclass(slots=True)
class _ActivationGate:
    socket: socket.socket
    confirmation_timeout_seconds: float
    released: bool = False
    exec_confirmed: bool = False
    exec_failed: bool = False

    async def release(self) -> None:
        if self.released:
            return
        try:
            await asyncio.get_running_loop().sock_sendall(self.socket, _ACTIVATE)
            confirmation = await _read_control_line_or_eof(
                self.socket,
                timeout_seconds=self.confirmation_timeout_seconds,
            )
            if confirmation:
                detail = confirmation.decode("utf-8", errors="replace")
                self.exec_failed = confirmation.startswith(b"EXEC_ERROR:")
                raise ProcessStartError(f"contained launcher exec failed: {detail}")
            self.exec_confirmed = True
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise ProcessStartError(
                "contained launcher exited before activation was acknowledged"
            ) from exc
        finally:
            self.socket.close()
            self.released = True

    def abort(self) -> None:
        if self.released:
            return
        self.socket.close()
        self.released = True


@dataclass(slots=True)
class ProcessHandle:
    """A running child process whose stdout and stderr are durable files."""

    process: asyncio.subprocess.Process
    request: ProcessExecutionRequest
    started_at: datetime
    containment: ProcessContainment | None = None
    _activation_gate: _ActivationGate | None = None

    @property
    def pid(self) -> int:
        if self.process.pid is None:
            raise RuntimeError("started process does not have a pid")
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
        return self._activation_gate is not None and not self._activation_gate.released

    async def activate(self) -> None:
        """Release a contained launcher after durable admission is persisted."""

        gate = self._activation_gate
        if gate is None or gate.released:
            return
        await gate.release()

    async def abort_gated_start(
        self,
        *,
        confirmation_seconds: float = 0.5,
        cleanup_containment: bool = False,
    ) -> bool:
        """Abort a launcher known not to have executed target code.

        Returns ``False`` when activation reached an uncertain or successful
        exec boundary, in which case callers must use normal physical-stop
        handling instead.
        """

        gate = self._activation_gate
        if gate is None or gate.exec_confirmed:
            return False
        if gate.released and not gate.exec_failed:
            return False
        if not gate.released:
            gate.abort()

        async def abort_and_confirm() -> None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=confirmation_seconds)
            except TimeoutError as exc:
                raise ProcessTreeTerminationError(
                    "gated launcher did not exit after activation denial"
                ) from exc
            if self.containment is not None:
                await self.containment.force_terminate(
                    confirmation_seconds=confirmation_seconds
                )
                if cleanup_containment:
                    await self.cleanup_confirmed_containment()

        abort_task = asyncio.create_task(
            abort_and_confirm(),
            name=f"riftx-gated-start-abort-{self.pid}",
        )
        _, interrupted = await _complete_task_uninterruptibly(abort_task)
        if interrupted:
            raise asyncio.CancelledError
        return True

    async def wait(
        self,
        *,
        termination_grace_seconds: float = 2.0,
        cleanup_containment: bool = False,
    ) -> ProcessResult:
        if self.activation_pending:
            raise ProcessStartError(
                "contained process is still gated; activate it only after durable admission"
            )

        async def wait_for_owned_processes() -> int:
            exit_code = await self.process.wait()
            if self.containment is not None:
                # Leader exit is not completion for a daemonizing process.  A
                # cgroup stays populated across setsid(), forks and leader
                # exit, so this waits for the actual owned execution to end.
                await self.containment.wait_empty(timeout_seconds=None)
                if cleanup_containment:
                    await self.cleanup_confirmed_containment()
            elif os.name == "posix":
                # The group leader's exit is not execution completion while a
                # same-PGID descendant remains alive.  Keep the execution
                # managed (and cancellable with its trusted handle) until the
                # entire owned group disappears.
                await _wait_for_posix_process_group_exit(
                    self.process_group_id,
                    timeout_seconds=None,
                )
                raise UnverifiedProcessTreeTerminationError(
                    f"process group {self.process_group_id!r} ended naturally, but "
                    "complete descendant absence cannot be proven without kernel containment"
                )
            else:
                raise UnverifiedProcessTreeTerminationError(
                    f"process leader {self.pid!r} ended naturally, but complete descendant "
                    "absence cannot be proven without a kernel Job Object"
                )
            return exit_code

        try:
            if self.request.timeout_seconds is None:
                exit_code = await wait_for_owned_processes()
            else:
                exit_code = await asyncio.wait_for(
                    wait_for_owned_processes(),
                    timeout=self.request.timeout_seconds,
                )
        except TimeoutError:
            await self._terminate(
                termination_grace_seconds,
                cleanup_containment=cleanup_containment,
            )
            return ProcessResult(
                status=ExecutionStatus.FAILED,
                exit_code=self.process.returncode,
                timed_out=True,
            )

        return ProcessResult(status=ExecutionStatus.EXITED, exit_code=exit_code)

    async def cancel(
        self,
        *,
        termination_grace_seconds: float = 2.0,
        cleanup_containment: bool = False,
    ) -> ProcessResult:
        await self._terminate(
            termination_grace_seconds,
            cleanup_containment=cleanup_containment,
        )
        return ProcessResult(
            status=ExecutionStatus.CANCELLED,
            exit_code=self.process.returncode,
        )

    async def cleanup_confirmed_containment(self) -> None:
        """Remove an empty containment only after durable stop proof is saved."""

        containment = self.containment
        if containment is None:
            return
        cleanup = asyncio.create_task(
            containment.cleanup(),
            name=f"riftx-containment-cleanup-{self.pid}",
        )
        _, interrupted = await _complete_task_uninterruptibly(cleanup)
        if interrupted:
            raise asyncio.CancelledError

    async def _terminate(
        self,
        grace_seconds: float,
        *,
        cleanup_containment: bool,
    ) -> None:
        gate = self._activation_gate
        if gate is not None and not gate.released:
            # EOF is itself a launch denial: the trusted launcher exits rather
            # than executing target code.  cgroup termination below is still
            # required to confirm the ownership boundary empty.
            gate.abort()

        if self.containment is not None:
            termination = asyncio.create_task(
                self._terminate_contained(
                    grace_seconds,
                    cleanup_containment=cleanup_containment,
                ),
                name=f"riftx-containment-stop-{self.pid}",
            )
            _, interrupted = await _complete_task_uninterruptibly(termination)
            if interrupted:
                # Safety cleanup completed before cancellation is propagated;
                # a caller cannot cancel the cancellation and leak descendants.
                raise asyncio.CancelledError
            return

        if _is_windows():
            # taskkill /T is useful best-effort cleanup, but it is not a
            # persistent kernel ownership boundary: descendants can detach or
            # race tree enumeration. Never turn its success into affirmative
            # whole-tree stop evidence.
            confirmation_seconds = max(
                grace_seconds,
                _MIN_WINDOWS_TREE_CONFIRMATION_SECONDS,
            )
            await _kill_windows_process_tree(
                self.pid,
                timeout_seconds=confirmation_seconds,
            )
            leader_wait = asyncio.create_task(self.process.wait())
            try:
                await asyncio.wait_for(
                    asyncio.shield(leader_wait),
                    timeout=confirmation_seconds,
                )
            except TimeoutError as exc:
                leader_wait.cancel()
                await asyncio.gather(leader_wait, return_exceptions=True)
                raise ProcessTreeTerminationError(
                    f"taskkill succeeded for process tree {self.pid!r}, "
                    "but leader exit could not be confirmed"
                ) from exc
            if self.process.returncode is None:
                raise ProcessTreeTerminationError(
                    f"taskkill succeeded for process tree {self.pid!r}, "
                    "but the leader still has no exit status"
                )
            raise UnverifiedProcessTreeTerminationError(
                f"taskkill stopped the observed process tree {self.pid!r}, but complete "
                "descendant absence cannot be proven without a kernel Job Object"
            )

        # A process group is only a best-effort cleanup mechanism.  Even when
        # it disappears, a setsid()/double-fork descendant could remain.  Do
        # the cleanup, then fail closed instead of returning CANCELLED.
        leader_wait = asyncio.create_task(self.process.wait())
        try:
            await _terminate_posix_process_group(
                self.process_group_id,
                grace_seconds=grace_seconds,
            )
        except BaseException:
            # A signal/confirmation failure can leave the leader alive.  Do
            # not turn that failure into an unbounded wait; asyncio's process
            # transport still owns child reaping after this waiter is
            # cancelled.
            leader_wait.cancel()
            await asyncio.gather(leader_wait, return_exceptions=True)
            raise
        await leader_wait
        raise UnverifiedProcessTreeTerminationError(
            f"process group {self.process_group_id!r} stopped, but complete descendant "
            "absence cannot be proven without kernel containment"
        )

    async def _terminate_contained(
        self,
        grace_seconds: float,
        *,
        cleanup_containment: bool,
    ) -> None:
        containment = self.containment
        if containment is None:
            raise RuntimeError("contained termination requires a containment")
        await containment.terminate(grace_seconds=grace_seconds)
        confirmation_seconds = max(grace_seconds, 0.5)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=confirmation_seconds)
        except TimeoutError as exc:
            raise ProcessTreeTerminationError(
                f"containment {containment.identifier!r} is empty, but leader exit "
                "could not be reaped"
            ) from exc
        if cleanup_containment:
            await self.cleanup_confirmed_containment()


class DirectProcessExecutor:
    """Launch argv directly without a shell."""

    def __init__(
        self,
        containment_manager: ProcessContainmentManager | None = None,
        *,
        autodetect_containment: bool = True,
        require_containment: bool = False,
        defer_activation: bool = False,
        launcher_ready_timeout_seconds: float = _DEFAULT_LAUNCHER_READY_TIMEOUT_SECONDS,
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
                # An unprivileged payload sharing the Runner uid can migrate
                # itself out of delegated cgroups. Never expose that manager as
                # affirmative containment, including to detached recovery.
                containment_configuration_error = exc
                containment_manager = None
        self._containment_manager = containment_manager
        self._containment_configuration_error = containment_configuration_error
        self._require_containment = require_containment
        self._defer_activation = defer_activation
        self._launcher_ready_timeout_seconds = launcher_ready_timeout_seconds

    @property
    def containment_manager(self) -> ProcessContainmentManager | None:
        return self._containment_manager

    async def start(self, request: ProcessExecutionRequest) -> ProcessHandle:
        request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        request.stderr_path.parent.mkdir(parents=True, exist_ok=True)

        containment: ProcessContainment | None = None
        if self._require_containment and self._containment_configuration_error is not None:
            raise ProcessStartError(
                f"kernel process containment is unsafe for {request.execution_key!r}: "
                f"{self._containment_configuration_error}"
            )
        if os.name == "posix" and self._containment_manager is not None:
            try:
                containment = await self._containment_manager.prepare(request.execution_key)
            except (OSError, ValueError, ProcessContainmentError) as exc:
                raise ProcessStartError(
                    f"failed to prepare kernel containment for "
                    f"{request.execution_key!r}: {exc}"
                ) from exc
        elif self._require_containment:
            raise ProcessStartError(
                f"kernel process containment is required for {request.execution_key!r}, "
                "but no supported kernel-backed containment backend is available"
            )

        if containment is not None or (os.name == "posix" and self._defer_activation):
            return await self._start_gated(request, containment)
        return await self._start_native(request)

    async def _start_native(self, request: ProcessExecutionRequest) -> ProcessHandle:
        stdout_file = _open_log(request.stdout_path)
        stderr_file = _open_log(request.stderr_path)
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv,
                cwd=request.cwd,
                env=request.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                **_process_group_options(),
            )
        except (OSError, ValueError) as exc:
            raise ProcessStartError(
                f"failed to start {request.argv[0]!r} for {request.execution_key!r}: {exc}"
            ) from exc
        finally:
            stdout_file.close()
            stderr_file.close()

        return ProcessHandle(process=process, request=request, started_at=datetime.now(UTC))

    async def _start_gated(
        self,
        request: ProcessExecutionRequest,
        containment: ProcessContainment | None,
    ) -> ProcessHandle:
        parent_socket: socket.socket | None = None
        child_socket: socket.socket | None = None
        process: asyncio.subprocess.Process | None = None
        handle: ProcessHandle | None = None
        stdout_file: BinaryIO | None = None
        stderr_file: BinaryIO | None = None
        target_env_file: BinaryIO | None = None
        try:
            parent_socket, child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            parent_socket.setblocking(False)
            child_socket.set_inheritable(True)
            stdout_file = _open_log(request.stdout_path)
            stderr_file = _open_log(request.stderr_path)
            target_env_file = _target_environment_file(request.env)
            launch_argv = _gated_launcher_argv(
                request.argv,
                control_fd=child_socket.fileno(),
                target_env_fd=target_env_file.fileno(),
                containment=containment,
            )
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *launch_argv,
                    cwd=request.cwd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    pass_fds=(child_socket.fileno(), target_env_file.fileno()),
                    # The target environment is untrusted input. In particular,
                    # PYTHONPATH/sitecustomize and LD_PRELOAD could execute before
                    # the wrapper joins its cgroup or reaches the activation gate.
                    # The wrapper therefore starts with no inherited environment
                    # and receives the inert target mapping over a separate FD.
                    env=_trusted_launcher_environment(),
                    **_process_group_options(),
                )
            )
            process, spawn_interrupted = await _complete_task_uninterruptibly(spawn)
            handle = ProcessHandle(
                process=process,
                request=request,
                started_at=datetime.now(UTC),
                containment=containment,
                _activation_gate=_ActivationGate(
                    parent_socket,
                    confirmation_timeout_seconds=self._launcher_ready_timeout_seconds,
                ),
            )
            child_socket.close()
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
            if ready_line != _LAUNCHER_READY:
                detail = ready_line.decode("utf-8", errors="replace") or "launcher closed control"
                raise ProcessStartError(
                    f"contained launcher failed for {request.execution_key!r}: {detail}"
                )

            if not self._defer_activation:
                await handle.activate()
            return handle
        except BaseException as exc:
            if parent_socket is not None:
                parent_socket.close()
            if child_socket is not None:
                child_socket.close()
            cleanup = asyncio.create_task(
                _cleanup_failed_gated_start(process, containment),
                name=f"riftx-gated-start-cleanup-{request.execution_key}",
            )
            try:
                _, cleanup_interrupted = await _complete_task_uninterruptibly(cleanup)
            except BaseException as cleanup_exc:
                if handle is not None:
                    raise UnconfirmedProcessStartError(
                        f"spawned launcher for {request.execution_key!r} could not be "
                        f"safely cleaned up: {cleanup_exc}",
                        handle=handle,
                        start_error=exc,
                        cleanup_error=cleanup_exc,
                    ) from cleanup_exc
                raise ProcessStartError(
                    f"failed to safely clean up contained start "
                    f"{request.execution_key!r}: {cleanup_exc}"
                ) from exc
            if isinstance(exc, asyncio.CancelledError) or cleanup_interrupted:
                raise asyncio.CancelledError from exc
            if isinstance(exc, ProcessStartError):
                raise
            if isinstance(exc, (OSError, ValueError)):
                raise ProcessStartError(
                    f"failed to start {request.argv[0]!r} for "
                    f"{request.execution_key!r}: {exc}"
                ) from exc
            raise
        finally:
            if stdout_file is not None:
                stdout_file.close()
            if stderr_file is not None:
                stderr_file.close()
            if target_env_file is not None:
                target_env_file.close()


async def _read_launcher_readiness(
    control_socket: socket.socket,
    *,
    timeout_seconds: float,
) -> bytes:
    async def read_line() -> bytes:
        loop = asyncio.get_running_loop()
        payload = bytearray()
        while len(payload) <= 4096:
            chunk = await loop.sock_recv(control_socket, 512)
            if not chunk:
                break
            payload.extend(chunk)
            newline = payload.find(b"\n")
            if newline >= 0:
                return bytes(payload[:newline])
        if len(payload) > 4096:
            raise ProcessStartError("contained launcher readiness exceeded 4096 bytes")
        return bytes(payload)

    try:
        return await asyncio.wait_for(read_line(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise ProcessStartError("timed out waiting for contained launcher admission") from exc


async def _read_control_line_or_eof(
    control_socket: socket.socket,
    *,
    timeout_seconds: float,
) -> bytes:
    async def read_line_or_eof() -> bytes:
        loop = asyncio.get_running_loop()
        payload = bytearray()
        while len(payload) <= 4096:
            chunk = await loop.sock_recv(control_socket, 512)
            if not chunk:
                return bytes(payload)
            payload.extend(chunk)
            newline = payload.find(b"\n")
            if newline >= 0:
                return bytes(payload[:newline])
        raise ProcessStartError("contained launcher confirmation exceeded 4096 bytes")

    try:
        return await asyncio.wait_for(read_line_or_eof(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise ProcessStartError("timed out waiting for contained launcher exec") from exc


def _gated_launcher_argv(
    target_argv: list[str],
    *,
    control_fd: int,
    target_env_fd: int,
    containment: ProcessContainment | None,
) -> list[str]:
    if containment is not None:
        return containment.launcher_argv(
            target_argv,
            control_fd=control_fd,
            target_env_fd=target_env_fd,
        )
    launcher = Path(__file__).with_name("_cgroup_launcher.py")
    return [
        sys.executable,
        "-I",
        "-S",
        str(launcher),
        "--control-fd",
        str(control_fd),
        "--target-env-fd",
        str(target_env_fd),
        "--",
        *target_argv,
    ]


def _target_environment_file(environment: dict[str, str]) -> BinaryIO:
    """Serialize an inert target environment into an anonymous inherited file."""

    for key, value in environment.items():
        if not key or "=" in key or "\x00" in key:
            raise ValueError(f"invalid environment variable name: {key!r}")
        if "\x00" in value:
            raise ValueError(f"environment variable {key!r} contains a null byte")
    payload = json.dumps(
        environment,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > _MAX_TARGET_ENVIRONMENT_BYTES:
        raise ValueError(
            "target environment exceeds "
            f"{_MAX_TARGET_ENVIRONMENT_BYTES} serialized bytes"
        )
    stream = tempfile.TemporaryFile(mode="w+b")
    try:
        stream.write(payload)
        stream.flush()
        stream.seek(0)
    except BaseException:
        stream.close()
        raise
    return stream


def _trusted_launcher_environment() -> dict[str, str]:
    """Return the fixed environment used before kernel admission and activation."""

    # An empty environment also strips platform loader injection variables such
    # as LD_PRELOAD/DYLD_INSERT_LIBRARIES. Python is invoked with -I -S as a
    # second, independent guard against sitecustomize and .pth execution.
    return {}


async def _cleanup_failed_gated_start(
    process: asyncio.subprocess.Process | None,
    containment: ProcessContainment | None,
) -> None:
    # Closing the activation socket happens before this coroutine is created.
    # Therefore a launcher not yet admitted observes EOF and cannot exec.  A
    # direct kill covers the short pre-membership window; cgroup.kill then
    # covers the admitted launcher and every descendant.
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
            raise ProcessTreeTerminationError(
                "contained launcher could not be reaped after failed start"
            ) from exc
    if containment is not None:
        await containment.cleanup()


async def _complete_task_uninterruptibly[TaskResult](
    task: asyncio.Task[TaskResult],
) -> tuple[TaskResult, bool]:
    """Finish a safety-critical task even if the awaiting caller is cancelled.

    The caller is told whether cancellation was observed and propagates it only
    after the inner operation has either completed or raised its own safety
    error.  Repeated ``Task.cancel()`` calls are tolerated.
    """

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    return task.result(), interrupted


def _open_log(path: Path) -> BinaryIO:
    return path.open("ab", buffering=0)


async def _terminate_posix_process_group(
    process_group_id: int,
    *,
    grace_seconds: float,
) -> None:
    if process_group_id <= 0 or process_group_id == os.getpgrp():
        raise ProcessGroupTerminationError(
            f"refusing to terminate unsafe process group {process_group_id!r}"
        )
    if not _signal_posix_process_group(process_group_id, signal.SIGTERM):
        return
    if await _wait_for_posix_process_group_exit(process_group_id, grace_seconds):
        return
    if not _signal_posix_process_group(process_group_id, signal.SIGKILL):
        return
    confirmation_seconds = max(grace_seconds, 0.5)
    if not await _wait_for_posix_process_group_exit(
        process_group_id,
        confirmation_seconds,
    ):
        raise ProcessGroupTerminationError(
            f"process group {process_group_id!r} remains alive after SIGKILL"
        )


def _signal_posix_process_group(process_group_id: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(process_group_id, sig)
    except ProcessLookupError:
        return False
    return True


def _posix_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_posix_process_group_exit(
    process_group_id: int,
    timeout_seconds: float | None,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = None if timeout_seconds is None else loop.time() + max(timeout_seconds, 0.0)
    while await asyncio.to_thread(_posix_process_group_exists, process_group_id):
        if deadline is None:
            await asyncio.sleep(0.02)
            continue
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.02, remaining))
    return True


def _process_group_options() -> dict[str, object]:
    if _is_windows():
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _is_windows() -> bool:
    return os.name == "nt"


async def _kill_windows_process_tree(
    pid: int,
    *,
    timeout_seconds: float,
) -> None:
    if pid <= 0:
        raise ProcessTreeTerminationError(f"refusing to terminate unsafe process id {pid!r}")
    try:
        taskkill = await asyncio.create_subprocess_exec(
            "taskkill.exe",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ProcessLookupError) as exc:
        raise ProcessTreeTerminationError(
            f"failed to start taskkill for process tree {pid!r}: {exc}"
        ) from exc

    bounded_timeout = max(timeout_seconds, 0.01)
    try:
        exit_code = await asyncio.wait_for(taskkill.wait(), timeout=bounded_timeout)
    except TimeoutError as exc:
        try:
            taskkill.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            await asyncio.wait_for(taskkill.wait(), timeout=0.1)
        except (OSError, TimeoutError):
            pass
        raise ProcessTreeTerminationError(
            f"taskkill timed out while terminating process tree {pid!r}"
        ) from exc
    except OSError as exc:
        raise ProcessTreeTerminationError(
            f"taskkill failed while terminating process tree {pid!r}: {exc}"
        ) from exc

    if exit_code != 0:
        raise ProcessTreeTerminationError(
            f"taskkill failed for process tree {pid!r} with exit status {exit_code!r}"
        )
