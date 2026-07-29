"""Application boundary for local interactive terminal sessions."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.ports import RunRepository
from riftx.domain import Execution, TerminalOwner, TerminalSession
from riftx.executors import EnvironmentMode
from riftx.runner import OutputSlice, TerminalLaunchRequest, TerminalSupervisor


@dataclass(frozen=True, slots=True)
class CreateTerminal:
    argv: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str | None] = field(default_factory=dict)
    cols: int = 120
    rows: int = 40
    owner: TerminalOwner = TerminalOwner.AGENT


@dataclass(frozen=True, slots=True)
class TerminalView:
    terminal: TerminalSession
    execution: Execution


class TerminalApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        supervisor: TerminalSupervisor,
    ) -> None:
        self._runs = run_repository
        self._supervisor = supervisor

    async def create(self, run_id: str, command: CreateTerminal) -> TerminalView:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        cwd = await asyncio.to_thread(
            lambda: Path(command.cwd or run.workspace_path).expanduser().resolve()
        )
        argv = command.argv or [_default_shell()]
        try:
            terminal = await self._supervisor.start(
                TerminalLaunchRequest(
                    run_id=run.id,
                    node_id=run.node_id,
                    cwd=cwd,
                    argv=argv,
                    environment_mode=EnvironmentMode.INHERIT,
                    env=command.env,
                    cols=command.cols,
                    rows=command.rows,
                    owner=command.owner,
                )
            )
        except (OSError, ValueError) as exc:
            raise ApplicationConflictError(
                "terminal_start_failed",
                f"Unable to start terminal command: {exc}",
                details={"argv": argv, "cwd": str(cwd)},
            ) from exc
        return TerminalView(
            terminal=terminal,
            execution=await self._supervisor.get_execution(terminal.id),
        )

    async def get(self, session_id: str) -> TerminalView:
        return TerminalView(
            terminal=await self._supervisor.get(session_id),
            execution=await self._supervisor.get_execution(session_id),
        )

    async def read(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> OutputSlice:
        return await self._supervisor.read(
            session_id,
            cursor=cursor,
            max_bytes=max_bytes,
        )

    async def write(self, session_id: str, data: bytes, *, actor: TerminalOwner) -> None:
        await self._supervisor.write(session_id, data, actor=actor)

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalView:
        terminal = await self._supervisor.resize(session_id, cols=cols, rows=rows)
        return TerminalView(
            terminal=terminal,
            execution=await self._supervisor.get_execution(session_id),
        )

    async def interrupt(self, session_id: str, *, actor: TerminalOwner) -> None:
        await self._supervisor.interrupt(session_id, actor=actor)

    async def take_over(self, session_id: str) -> TerminalView:
        terminal = await self._supervisor.take_over(session_id)
        return TerminalView(
            terminal=terminal,
            execution=await self._supervisor.get_execution(session_id),
        )

    async def release(self, session_id: str) -> TerminalView:
        terminal = await self._supervisor.release(session_id)
        return TerminalView(
            terminal=terminal,
            execution=await self._supervisor.get_execution(session_id),
        )

    async def close(self, session_id: str) -> TerminalView:
        terminal = await self._supervisor.close(session_id)
        return TerminalView(
            terminal=terminal,
            execution=await self._supervisor.get_execution(session_id),
        )


def _default_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "/bin/sh" if os.name == "posix" else "cmd.exe"
