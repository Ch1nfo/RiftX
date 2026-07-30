"""Application boundary for local interactive terminal sessions."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.ports import RunEventRepository, RunRepository
from riftx.domain import (
    Execution,
    TerminalOwner,
    TerminalSession,
    TerminalTakeoverSummary,
)
from riftx.executors import EnvironmentMode
from riftx.runner import OutputSlice, TerminalController, TerminalLaunchRequest

from .artifacts import ArtifactApplicationService, RegisterArtifact, RegisterArtifactContent


@dataclass(frozen=True, slots=True)
class CreateTerminal:
    session_id: str | None = None
    execution_id: str | None = None
    agent_session_id: str | None = None
    tool_call_id: str | None = None
    argv: list[str] = field(default_factory=list)
    tool_id: str | None = None
    tool_version: str | None = None
    cwd: str | None = None
    env: dict[str, str | None] = field(default_factory=dict)
    cols: int = 120
    rows: int = 40
    owner: TerminalOwner = TerminalOwner.AGENT


@dataclass(frozen=True, slots=True)
class TerminalView:
    terminal: TerminalSession
    execution: Execution
    takeover_summary: TerminalTakeoverSummary | None = None


class TerminalApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        supervisor: TerminalController,
        artifact_service: ArtifactApplicationService | None = None,
        event_repository: RunEventRepository | None = None,
    ) -> None:
        self._runs = run_repository
        self._supervisor = supervisor
        self._artifacts = artifact_service
        self._events = event_repository

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
                    session_id=command.session_id,
                    execution_id=command.execution_id,
                    agent_session_id=command.agent_session_id,
                    tool_call_id=command.tool_call_id,
                    run_id=run.id,
                    node_id=run.node_id,
                    cwd=cwd,
                    argv=argv,
                    tool_id=command.tool_id,
                    tool_version=command.tool_version,
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
        before = await self._supervisor.get(session_id)
        execution = await self._supervisor.get_execution(session_id)
        started_cursor = before.takeover_cursor
        takeover_started_at = before.takeover_started_at
        terminal = await self._supervisor.release(session_id)
        summary = None
        if started_cursor is not None and self._artifacts is not None:
            ended_cursor = terminal.output_cursor
            content = await self._read_range(session_id, started_cursor, ended_cursor)
            artifact = await self._artifacts.register_content(
                terminal.run_id,
                RegisterArtifactContent(
                    content=content,
                    name=(
                        f"terminal-{terminal.id}-takeover-"
                        f"{started_cursor}-{ended_cursor}.log"
                    ),
                    mime_type="application/octet-stream",
                    description="Immutable terminal character stream captured during takeover.",
                ),
            )
            summary = TerminalTakeoverSummary(
                run_id=terminal.run_id,
                terminal_id=terminal.id,
                execution_id=execution.id,
                started_cursor=started_cursor,
                ended_cursor=ended_cursor,
                byte_count=len(content),
                artifact_id=artifact.id,
                summary=_terminal_delta_summary(content),
                takeover_started_at=takeover_started_at,
            )
            if self._events is not None:
                await self._events.append(
                    terminal.run_id,
                    "terminal.takeover_summarized",
                    summary.model_dump(mode="json"),
                )
        return TerminalView(
            terminal=terminal,
            execution=execution,
            takeover_summary=summary,
        )

    async def close(self, session_id: str) -> TerminalView:
        terminal = await self._supervisor.close(session_id)
        execution = await self._supervisor.get_execution(session_id)
        if terminal.transcript_artifact_id is None and self._artifacts is not None:
            artifact = await self._artifacts.register(
                terminal.run_id,
                RegisterArtifact(
                    source_path=execution.stdout_path,
                    name=f"terminal-{terminal.id}-transcript.log",
                    mime_type="application/octet-stream",
                    description="Complete immutable terminal character stream.",
                    execution_id=execution.id,
                ),
            )
            terminal = await self._supervisor.attach_transcript_artifact(
                terminal.id,
                artifact.id,
            )
        return TerminalView(
            terminal=terminal,
            execution=execution,
        )

    async def _read_range(self, session_id: str, start: int, end: int) -> bytes:
        chunks: list[bytes] = []
        cursor = start
        while cursor < end:
            output = await self._supervisor.read(
                session_id,
                cursor=cursor,
                max_bytes=min(1024 * 1024, end - cursor),
            )
            chunks.append(output.data)
            if output.next_cursor <= cursor:
                break
            cursor = output.next_cursor
        content = b"".join(chunks)
        if len(content) != end - start:
            raise ApplicationConflictError(
                "terminal_takeover_stream_incomplete",
                "Unable to read the complete terminal takeover character stream",
                details={"session_id": session_id, "start": start, "end": end},
            )
        return content


def _default_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "/bin/sh" if os.name == "posix" else "cmd.exe"


def _terminal_delta_summary(content: bytes, *, max_characters: int = 4000) -> str:
    if not content:
        return "User terminal takeover produced no output."
    text = content.decode("utf-8", errors="replace").replace("\x00", "")
    recent = text[-max_characters:]
    return (
        f"User terminal takeover produced {len(content)} bytes. "
        f"Recent output:\n{recent}"
    )
