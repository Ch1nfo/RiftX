"""Native terminal backend contracts shared by Unix PTY and Windows ConPTY."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from riftx.executors.process import ProcessStartError

from .models import TerminalLaunchRequest


class NativeTerminalHandle(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def process_group_id(self) -> int: ...

    @property
    def containment_identifier(self) -> str | None: ...

    @property
    def activation_pending(self) -> bool: ...

    async def activate(self) -> None: ...

    async def abort_gated_start(
        self,
        *,
        confirmation_seconds: float = 0.5,
        cleanup_containment: bool = False,
    ) -> bool: ...

    async def write(self, data: bytes) -> None: ...

    async def resize(self, cols: int, rows: int) -> None: ...

    async def interrupt(self) -> None: ...

    async def terminate(
        self,
        grace_seconds: float,
        *,
        cleanup_containment: bool = False,
    ) -> None: ...

    async def wait(self, *, cleanup_containment: bool = False) -> int: ...

    async def cleanup_confirmed_containment(self) -> None: ...

    async def close_output(self) -> None: ...


class UnconfirmedTerminalStartError(ProcessStartError):
    """A native terminal spawned but its startup cleanup is unconfirmed."""

    def __init__(
        self,
        message: str,
        *,
        handle: NativeTerminalHandle,
        start_error: BaseException,
        cleanup_error: BaseException,
    ) -> None:
        super().__init__(message)
        self.handle = handle
        self.start_error = start_error
        self.cleanup_error = cleanup_error


class NativeTerminalBackend(Protocol):
    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        transcript_path: Path,
        environment: dict[str, str],
    ) -> NativeTerminalHandle: ...
