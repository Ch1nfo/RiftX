"""Native terminal backend contracts shared by Unix PTY and Windows ConPTY."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import TerminalLaunchRequest


class NativeTerminalHandle(Protocol):
    @property
    def pid(self) -> int: ...

    async def write(self, data: bytes) -> None: ...

    async def resize(self, cols: int, rows: int) -> None: ...

    async def interrupt(self) -> None: ...

    async def terminate(self, grace_seconds: float) -> None: ...

    async def wait(self) -> int: ...

    async def close_output(self) -> None: ...


class NativeTerminalBackend(Protocol):
    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        transcript_path: Path,
        environment: dict[str, str],
    ) -> NativeTerminalHandle: ...
