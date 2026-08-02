"""Application boundary for local interactive terminal sessions."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.ports import RunEventRepository, RunRepository
from riftx.domain import (
    DomainError,
    Execution,
    Run,
    RunStatus,
    TerminalOwner,
    TerminalSession,
    TerminalStatus,
    TerminalTakeoverSummary,
)
from riftx.executors import EnvironmentMode
from riftx.hooks import HookBus, HookDecision, HookPoint, HookRequest
from riftx.runner import EffectGuard, OutputSlice, TerminalController, TerminalLaunchRequest

from .artifacts import ArtifactApplicationService, RegisterArtifact, RegisterArtifactContent


@dataclass(frozen=True, slots=True)
class CreateTerminal:
    session_id: str | None = None
    execution_id: str | None = None
    execution_key: str | None = None
    agent_session_id: str | None = None
    tool_call_id: str | None = None
    attempt_group: str | None = None
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
        hooks: HookBus | None = None,
    ) -> None:
        self._runs = run_repository
        self._supervisor = supervisor
        self._artifacts = artifact_service
        self._events = event_repository
        self._hooks = hooks

    async def materialize_launch_request(
        self,
        run_id: str,
        command: CreateTerminal,
    ) -> TerminalLaunchRequest:
        """Resolve the exact launch identity shared by start and failure settlement."""

        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        self._require_execution_allowed(run)
        return await self._build_launch_request(run, command)

    async def create(
        self,
        run_id: str,
        command: CreateTerminal,
        *,
        effect_guard: EffectGuard | None = None,
        launch_request: TerminalLaunchRequest | None = None,
    ) -> TerminalView:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        self._require_execution_allowed(run)
        if launch_request is None:
            launch_request = await self._build_launch_request(run, command)
        else:
            await self._require_materialized_request_matches(
                run,
                command,
                launch_request,
            )
        cwd = launch_request.cwd
        argv = launch_request.argv

        async def combined_effect_guard() -> None:
            current = await self._runs.get(run.id)
            if current is None:
                raise EntityNotFoundError("Run", run.id)
            self._require_execution_allowed(current)
            if effect_guard is not None:
                await effect_guard()

        # A terminal hook is itself part of admission.  The caller's durable
        # claim must still be current before the hook can observe the launch.
        await combined_effect_guard()
        await self._terminal_hook(
            HookPoint.TERMINAL_OPEN,
            run.id,
            {
                "session_id": command.session_id,
                "agent_session_id": command.agent_session_id,
                "argv": argv,
                "cwd": str(cwd),
                "owner": command.owner.value,
            },
        )

        try:
            terminal = await self._supervisor.start(
                launch_request,
                effect_guard=combined_effect_guard,
            )
        except (OSError, ValueError) as exc:
            raise ApplicationConflictError(
                "terminal_start_failed",
                f"Unable to start terminal command: {exc}",
                details={"argv": argv, "cwd": str(cwd)},
            ) from exc
        try:
            await combined_effect_guard()
        except BaseException:
            # Preserve the admission failure even if best-effort cleanup also
            # has trouble confirming the terminal stop.
            with suppress(Exception):
                await self._supervisor.close(terminal.id)
            raise
        return TerminalView(
            terminal=terminal,
            execution=await self._supervisor.get_execution(terminal.id),
        )

    @staticmethod
    async def _build_launch_request(
        run: Run,
        command: CreateTerminal,
    ) -> TerminalLaunchRequest:
        cwd = await asyncio.to_thread(
            lambda: Path(command.cwd or run.workspace_path).expanduser().resolve()
        )
        argv = command.argv or [_default_shell()]
        return TerminalLaunchRequest(
            session_id=command.session_id,
            execution_id=command.execution_id,
            execution_key=command.execution_key,
            agent_session_id=command.agent_session_id,
            tool_call_id=command.tool_call_id,
            attempt_group=command.attempt_group,
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

    @staticmethod
    async def _require_materialized_request_matches(
        run: Run,
        command: CreateTerminal,
        request: TerminalLaunchRequest,
    ) -> None:
        cwd = await asyncio.to_thread(
            lambda: Path(command.cwd or run.workspace_path).expanduser().resolve()
        )
        argv = command.argv or [_default_shell()]
        fields: tuple[tuple[str, object, object], ...] = (
            ("session_id", request.session_id, command.session_id),
            ("execution_id", request.execution_id, command.execution_id),
            ("execution_key", request.execution_key, command.execution_key),
            ("agent_session_id", request.agent_session_id, command.agent_session_id),
            ("tool_call_id", request.tool_call_id, command.tool_call_id),
            ("attempt_group", request.attempt_group, command.attempt_group),
            ("run_id", request.run_id, run.id),
            ("node_id", request.node_id, run.node_id),
            ("runner_principal", request.runner_principal, None),
            ("cwd", request.cwd, cwd),
            ("argv", request.argv, argv),
            ("tool_id", request.tool_id, command.tool_id),
            ("tool_version", request.tool_version, command.tool_version),
            ("environment_mode", request.environment_mode, EnvironmentMode.INHERIT),
            ("env", request.env, command.env),
            ("cols", request.cols, command.cols),
            ("rows", request.rows, command.rows),
            ("owner", request.owner, command.owner),
        )
        mismatched = [name for name, actual, expected in fields if actual != expected]
        if mismatched:
            raise ValueError(
                "materialized terminal launch does not match command: "
                + ", ".join(sorted(mismatched))
            )

    @staticmethod
    def _require_execution_allowed(run: Run) -> None:
        if run.status not in {
            RunStatus.PAUSING,
            RunStatus.PAUSED,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
            RunStatus.COMPLETING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
        }:
            return
        raise ApplicationConflictError(
            "run_execution_blocked",
            f"Run {run.id!r} cannot start a terminal while it is {run.status.value}",
            details={"run_id": run.id, "status": run.status.value},
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
        terminal = await self._supervisor.get(session_id)
        run = await self._runs.get(terminal.run_id)
        if run is None:
            raise EntityNotFoundError("Run", terminal.run_id)
        self._require_execution_allowed(run)
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
        before = await self._supervisor.get(session_id)
        await self._terminal_hook(
            HookPoint.TERMINAL_OWNER_CHANGED,
            before.run_id,
            {
                "terminal_id": before.id,
                "previous_owner": before.owner.value,
                "owner": TerminalOwner.USER.value,
            },
        )
        terminal = await self._supervisor.take_over(session_id)
        return TerminalView(
            terminal=terminal,
            execution=await self._supervisor.get_execution(session_id),
        )

    async def release(self, session_id: str) -> TerminalView:
        before = await self._supervisor.get(session_id)
        await self._terminal_hook(
            HookPoint.TERMINAL_OWNER_CHANGED,
            before.run_id,
            {
                "terminal_id": before.id,
                "previous_owner": before.owner.value,
                "owner": TerminalOwner.AGENT.value,
            },
        )
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
                    name=(f"terminal-{terminal.id}-takeover-{started_cursor}-{ended_cursor}.log"),
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
        before = await self._supervisor.get(session_id)
        await self._terminal_hook(
            HookPoint.TERMINAL_CLOSE,
            before.run_id,
            {
                "terminal_id": before.id,
                "owner": before.owner.value,
                "status": before.status.value,
            },
        )
        terminal = await self._supervisor.close(session_id)
        execution = await self._supervisor.get_execution(session_id)
        if (
            terminal.status is TerminalStatus.CLOSED
            and terminal.transcript_artifact_id is None
            and self._artifacts is not None
        ):
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

    async def _terminal_hook(
        self,
        point: HookPoint,
        run_id: str,
        payload: dict[str, object],
    ) -> None:
        if self._hooks is None:
            return
        outcome = await self._hooks.dispatch(
            HookRequest(point=point, run_id=run_id, payload=payload)
        )
        if self._events is not None:
            for emitted in outcome.emitted_events:
                event_type = emitted.get("event_type")
                if isinstance(event_type, str) and event_type:
                    await self._events.append(
                        run_id,
                        event_type,
                        {key: value for key, value in emitted.items() if key != "event_type"},
                    )
        if outcome.decision in {HookDecision.BLOCK, HookDecision.REQUIRE_APPROVAL}:
            raise DomainError(f"Runtime Hook blocked {point.value}")

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
    return f"User terminal takeover produced {len(content)} bytes. Recent output:\n{recent}"
