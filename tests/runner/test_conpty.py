from __future__ import annotations

import asyncio
import importlib.util
import queue
import shutil
import sys
from pathlib import Path

import pytest

from riftx.domain import Engagement, Objective, Run, TerminalOwner, TerminalStatus
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.runner import RunnerPaths, TerminalLaunchRequest, TerminalSupervisor
from riftx.runner.conpty import ConPTYBackend


class FakeWinPTYProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.exitstatus: int | None = None
        self._alive = True
        self._chunks: queue.Queue[str | None] = queue.Queue()
        self._chunks.put("READY\r\n")
        self.writes: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self.terminations: list[bool] = []

    def read(self, _: int = 65536) -> str:
        chunk = self._chunks.get(timeout=2)
        if chunk is None:
            raise EOFError
        return chunk

    def write(self, data: str) -> None:
        self.writes.append(data)
        if data != "\x03":
            self._chunks.put(f"ECHO:{data}")

    def setwinsize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def isalive(self) -> bool:
        return self._alive

    def terminate(self, force: bool = False) -> None:
        self.terminations.append(force)
        self._alive = False
        self.exitstatus = 1 if force else 0
        self._chunks.put(None)

    def close(self, force: bool = False) -> None:
        if self._alive:
            self.terminate(force)


class FakeNativeHandle:
    def __init__(self, transcript: Path) -> None:
        self.pid = 5252
        self.transcript = transcript
        self.writes: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.interrupts = 0
        self._closed = asyncio.Event()

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        await asyncio.to_thread(_append_bytes, self.transcript, b"ECHO:" + data)

    async def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def terminate(self, _: float) -> None:
        self._closed.set()

    async def wait(self) -> int:
        await self._closed.wait()
        return 0

    async def close_output(self) -> None:
        return None


class FakeNativeBackend:
    def __init__(self) -> None:
        self.handle: FakeNativeHandle | None = None

    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        transcript_path: Path,
        environment: dict[str, str],
    ) -> FakeNativeHandle:
        assert request.argv == ["powershell.exe"]
        assert environment["RIFTX_TEST"] == "1"
        self.handle = FakeNativeHandle(transcript_path)
        await asyncio.to_thread(transcript_path.write_bytes, b"CONPTY READY\r\n")
        return self.handle


@pytest.mark.asyncio
async def test_conpty_backend_streams_writes_resizes_interrupts_and_closes(
    tmp_path: Path,
) -> None:
    process = FakeWinPTYProcess()
    calls: list[tuple[list[str], int, int]] = []

    def spawn(
        argv: list[str],
        _: Path,
        __: dict[str, str],
        cols: int,
        rows: int,
    ) -> FakeWinPTYProcess:
        calls.append((argv, cols, rows))
        return process

    transcript = tmp_path / "transcript.log"
    transcript.touch()
    backend = ConPTYBackend(spawn_process=spawn)
    handle = await backend.start(
        TerminalLaunchRequest(
            run_id="run-1",
            node_id="windows-a",
            cwd=tmp_path,
            argv=["powershell.exe", "-NoLogo"],
            cols=120,
            rows=40,
        ),
        transcript_path=transcript,
        environment={"PATH": "test"},
    )
    await handle.write("你好\r\n".encode())
    await handle.resize(132, 48)
    await handle.interrupt()
    await handle.terminate(0.01)
    assert await handle.wait() == 0
    await handle.close_output()

    assert calls == [(["powershell.exe", "-NoLogo"], 120, 40)]
    assert process.writes == ["你好\r\n", "\x03"]
    assert process.sizes == [(48, 132)]
    assert "READY" in transcript.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_terminal_supervisor_uses_conpty_backend_and_preserves_ownership(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'conpty.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="ConPTY test")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="windows-a",
            objective=Objective(description="Exercise ConPTY"),
            workspace_path=str(tmp_path),
        )
    )
    backend = FakeNativeBackend()
    supervisor = TerminalSupervisor(
        terminal_repository=SQLAlchemyTerminalRepository(database.session_factory),
        execution_repository=SQLAlchemyExecutionRepository(database.session_factory),
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "state"),
        native_backend=backend,
        platform_name="nt",
        termination_grace_seconds=0.01,
    )
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            session_id="terminal-windows",
            execution_id="execution-windows",
            run_id="run-1",
            node_id="windows-a",
            cwd=tmp_path,
            argv=["powershell.exe"],
            env={"RIFTX_TEST": "1"},
        )
    )
    assert terminal.status is TerminalStatus.OPEN
    assert terminal.id == "terminal-windows"
    assert terminal.execution_id == "execution-windows"

    await supervisor.take_over(terminal.id)
    await supervisor.write(terminal.id, b"Get-Location\r\n", actor=TerminalOwner.USER)
    await supervisor.resize(terminal.id, cols=160, rows=50)
    await supervisor.interrupt(terminal.id, actor=TerminalOwner.USER)
    output = await supervisor.read(terminal.id)
    assert b"CONPTY READY" in output.data
    assert b"ECHO:Get-Location" in output.data
    assert backend.handle is not None
    assert backend.handle.sizes == [(160, 50)]
    assert backend.handle.interrupts == 1

    closed = await supervisor.close(terminal.id)
    assert closed.status is TerminalStatus.CLOSED
    await database.dispose()


@pytest.mark.skipif(
    sys.platform != "win32"
    or importlib.util.find_spec("winpty") is None
    or shutil.which("pwsh.exe") is None,
    reason="real ConPTY smoke test requires Windows, pywinpty, and PowerShell 7",
)
@pytest.mark.asyncio
async def test_real_conpty_runs_interactive_powershell_with_utf8(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'real-conpty.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-real", name="Real ConPTY")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-real",
            engagement_id="engagement-real",
            node_id="windows-real",
            objective=Objective(description="Real ConPTY smoke test"),
            workspace_path=str(tmp_path),
        )
    )
    supervisor = TerminalSupervisor(
        terminal_repository=SQLAlchemyTerminalRepository(database.session_factory),
        execution_repository=SQLAlchemyExecutionRepository(database.session_factory),
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        paths=RunnerPaths(tmp_path / "real-state"),
        platform_name="nt",
        termination_grace_seconds=0.5,
    )
    terminal = await supervisor.start(
        TerminalLaunchRequest(
            run_id="run-real",
            node_id="windows-real",
            cwd=tmp_path,
            argv=["pwsh.exe", "-NoLogo", "-NoProfile", "-NoExit"],
        )
    )
    await supervisor.write(
        terminal.id,
        "Write-Output 'RIFTX_CONPTY_UTF8_你好'\r\n".encode(),
        actor=TerminalOwner.AGENT,
    )
    for _ in range(200):
        output = await supervisor.read(terminal.id)
        if "RIFTX_CONPTY_UTF8_你好" in output.data.decode(errors="replace"):
            break
        await asyncio.sleep(0.025)
    else:
        raise AssertionError("PowerShell output was not observed through ConPTY")
    await supervisor.close(terminal.id)
    await database.dispose()


def _append_bytes(path: Path, data: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(data)
