"""Windows ConPTY backend powered by the optional pywinpty runtime."""

from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from riftx.executors.process import (
    ProcessTreeTerminationError,
    _kill_windows_process_tree,
)

from .models import TerminalLaunchRequest


class ConPTYUnavailableError(RuntimeError):
    """Raised when the Windows ConPTY runtime is unavailable."""


class ConPTYProcessTreeTerminationError(RuntimeError):
    """Raised when Windows cannot affirmatively terminate a ConPTY process tree."""


class _WinPTYProcess(Protocol):
    pid: int
    exitstatus: int | None

    def read(self, size: int = ...) -> str | bytes: ...

    def write(self, data: str) -> object: ...

    def setwinsize(self, rows: int, cols: int) -> object: ...

    def isalive(self) -> bool: ...

    def terminate(self, force: bool = ...) -> object: ...

    def close(self, force: bool = ...) -> object: ...


SpawnProcess = Callable[
    [list[str], Path, dict[str, str], int, int],
    _WinPTYProcess,
]
TerminateProcessTree = Callable[[int, float], Awaitable[None]]


class ConPTYHandle:
    def __init__(
        self,
        process: _WinPTYProcess,
        transcript_path: Path,
        *,
        terminate_process_tree: TerminateProcessTree,
    ) -> None:
        self.process = process
        self._terminate_process_tree = terminate_process_tree
        self._write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(
            self._pump_output(transcript_path),
            name=f"riftx-conpty-reader-{process.pid}",
        )

    @property
    def pid(self) -> int:
        return int(self.process.pid)

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
        if not data:
            return
        text = data.decode("utf-8", errors="replace")
        async with self._write_lock:
            await asyncio.to_thread(self.process.write, text)

    async def resize(self, cols: int, rows: int) -> None:
        await asyncio.to_thread(self.process.setwinsize, rows, cols)

    async def interrupt(self) -> None:
        await self.write(b"\x03")

    async def terminate(
        self,
        grace_seconds: float,
        *,
        cleanup_containment: bool = False,
    ) -> None:
        # pywinpty's isalive()/terminate() describe only its leader. taskkill /T
        # is best-effort tree enumeration rather than a persistent kernel-owned
        # Job Object, so even a successful call cannot prove all descendants are
        # gone.
        if not await asyncio.to_thread(self.process.isalive):
            raise ConPTYProcessTreeTerminationError(
                f"cannot confirm ConPTY process tree {self.pid!r} stopped because "
                "its leader exited before tree termination was acknowledged"
            )
        confirmation_seconds = max(grace_seconds, 0.5)
        await self._terminate_process_tree(self.pid, confirmation_seconds)
        try:
            await asyncio.wait_for(
                self._wait_until_dead(),
                timeout=confirmation_seconds,
            )
        except TimeoutError:
            raise ConPTYProcessTreeTerminationError(
                f"ConPTY leader {self.pid!r} remains alive after tree termination"
            ) from None
        raise ConPTYProcessTreeTerminationError(
            f"taskkill stopped the observed ConPTY tree {self.pid!r}, but complete "
            "descendant absence cannot be proven without a kernel Job Object"
        )

    async def wait(self, *, cleanup_containment: bool = False) -> int:
        await self._wait_until_dead()
        raise ConPTYProcessTreeTerminationError(
            f"ConPTY leader {self.pid!r} exited, but complete descendant absence "
            "cannot be proven without a kernel Job Object"
        )

    async def cleanup_confirmed_containment(self) -> None:
        return None

    async def close_output(self) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self._reader_task), timeout=1.0)
        except TimeoutError:
            await asyncio.to_thread(self.process.close, True)
            await asyncio.gather(self._reader_task, return_exceptions=True)
        else:
            await asyncio.to_thread(self.process.close, False)

    async def _pump_output(self, transcript_path: Path) -> None:
        with transcript_path.open("ab", buffering=0) as transcript:
            while True:
                try:
                    chunk = await asyncio.to_thread(self.process.read, 64 * 1024)
                except (EOFError, OSError):
                    return
                if not chunk:
                    if not await asyncio.to_thread(self.process.isalive):
                        return
                    await asyncio.sleep(0.01)
                    continue
                data = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                transcript.write(data)

    async def _wait_until_dead(self) -> None:
        await asyncio.to_thread(_wait_process, self.process)


class ConPTYBackend:
    """Spawn real Windows pseudo consoles and expose the native terminal contract."""

    def __init__(
        self,
        *,
        spawn_process: SpawnProcess | None = None,
        terminate_process_tree: TerminateProcessTree | None = None,
    ) -> None:
        self._spawn_process = spawn_process or _spawn_pywinpty
        self._terminate_process_tree = terminate_process_tree or _terminate_windows_process_tree

    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        transcript_path: Path,
        environment: dict[str, str],
    ) -> ConPTYHandle:
        try:
            process = await asyncio.to_thread(
                self._spawn_process,
                request.argv,
                request.cwd,
                environment,
                request.cols,
                request.rows,
            )
        except ConPTYUnavailableError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"failed to start ConPTY command {request.argv[0]!r}: {exc}"
            ) from exc
        return ConPTYHandle(
            process,
            transcript_path,
            terminate_process_tree=self._terminate_process_tree,
        )


def _spawn_pywinpty(
    argv: list[str],
    cwd: Path,
    environment: dict[str, str],
    cols: int,
    rows: int,
) -> _WinPTYProcess:
    try:
        module = importlib.import_module("winpty")
    except ImportError as exc:
        raise ConPTYUnavailableError(
            "ConPTY requires the optional pywinpty package on Windows"
        ) from exc
    process_type = getattr(module, "PtyProcess", None)
    if process_type is None:
        raise ConPTYUnavailableError("pywinpty does not expose PtyProcess")

    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": environment,
        "dimensions": (rows, cols),
    }
    backend_type = getattr(module, "Backend", None)
    if backend_type is not None:
        conpty = getattr(backend_type, "ConPTY", None) or getattr(backend_type, "Conpty", None)
        if conpty is not None:
            kwargs["backend"] = conpty
    return process_type.spawn(argv, **kwargs)


def _wait_process(process: _WinPTYProcess) -> None:
    while process.isalive():
        time.sleep(0.02)


async def _terminate_windows_process_tree(pid: int, timeout_seconds: float) -> None:
    try:
        await _kill_windows_process_tree(
            pid,
            timeout_seconds=timeout_seconds,
        )
    except ProcessTreeTerminationError as exc:
        raise ConPTYProcessTreeTerminationError(
            f"could not terminate ConPTY process tree {pid!r}: {exc}"
        ) from exc
