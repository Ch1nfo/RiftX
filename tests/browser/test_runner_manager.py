from __future__ import annotations

import asyncio
import builtins
from pathlib import Path

import pytest

from riftx.browser import (
    BrowserActCommand,
    BrowserAttachment,
    BrowserObserveCommand,
    BrowserOpenCommand,
    BrowserSessionCommand,
)
from riftx.domain import (
    BrowserAction,
    BrowserActionType,
    BrowserMode,
    BrowserObservation,
    BrowserOwner,
    BrowserPage,
    FormSummary,
    InteractiveElement,
    Scope,
)
from riftx.runner import RunnerPaths
from riftx.runner import browser as browser_module
from riftx.runner.browser import PlaywrightBrowserEngine, RunnerBrowserManager


class FakeEngineSession:
    profile_path = None

    def __init__(self, session_id: str, url: str) -> None:
        self.session_id = session_id
        self.url = url
        self.title = "Start"
        self.closed = False
        self.acted: list[BrowserAction] = []
        self.storage = "before"
        self.downloads: list[tuple[BrowserAttachment, bytes]] = []

    async def pages(self) -> list[BrowserPage]:
        return [
            BrowserPage(
                id="page-1",
                browser_session_id=self.session_id,
                url=self.url,
                title=self.title,
            )
        ]

    async def observe(
        self,
        page_id: str,
        *,
        browser_session_id: str,
        version: int,
        include_screenshot: bool,
        include_network: bool,
    ) -> tuple[BrowserPage, BrowserObservation, bytes]:
        page = (await self.pages())[0]
        page.last_observation_version = version
        return (
            page,
            BrowserObservation(
                browser_session_id=browser_session_id,
                page_id=page_id,
                url=self.url,
                title=self.title,
                visible_text_excerpt="Visible body",
                headings=["Heading"],
                interactive_elements=[
                    InteractiveElement(
                        ref="e-1",
                        role="link",
                        text="Next",
                        href="/next",
                    )
                ],
                forms=[FormSummary(ref="form-1")],
                observation_version=version,
            ),
            b"png" if include_screenshot else b"",
        )

    async def act(self, action: BrowserAction):
        self.acted.append(action)
        if action.action is BrowserActionType.CLICK:
            self.url = "https://example.com/next"
            self.title = "Next"
        return None, b""

    async def storage_digest(self) -> str:
        return self.storage

    async def download_count(self) -> int:
        return len(self.downloads)

    async def downloads_since(self, index: int):
        return self.downloads[index:]

    async def close(self) -> None:
        self.closed = True


class FakeEngine:
    def __init__(self) -> None:
        self.sessions: dict[str, FakeEngineSession] = {}
        self.open_calls = 0

    async def open(self, command: BrowserOpenCommand) -> FakeEngineSession:
        self.open_calls += 1
        session = FakeEngineSession(command.session_id, command.url)
        self.sessions[command.session_id] = session
        return session


def open_command() -> BrowserOpenCommand:
    return BrowserOpenCommand(
        session_id="browser-1",
        run_id="run-1",
        agent_session_id="agent-session-1",
        node_id="local",
        mode=BrowserMode.MANAGED_EPHEMERAL,
        url="https://example.com/start",
        scope=Scope(domains=["example.com"]),
    )


async def test_playwright_engine_reports_optional_install_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def import_without_playwright(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("playwright"):
            raise ImportError("optional dependency unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_playwright)

    with pytest.raises(RuntimeError, match=r"riftx\[browser\]"):
        await PlaywrightBrowserEngine(RunnerPaths(tmp_path)).open(open_command())


async def test_manager_constructs_default_engine_once_on_first_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    constructed: list[RunnerPaths] = []

    def create_engine(paths: RunnerPaths) -> FakeEngine:
        constructed.append(paths)
        return engine

    monkeypatch.setattr(browser_module, "PlaywrightBrowserEngine", create_engine)
    manager = RunnerBrowserManager(node_id="local", paths=RunnerPaths(tmp_path))

    assert constructed == []
    await asyncio.gather(manager.open(open_command()), manager.open(open_command()))

    assert len(constructed) == 1
    assert engine.open_calls == 1


async def test_failed_first_open_leaves_no_active_session(tmp_path: Path) -> None:
    class FlakyEngine(FakeEngine):
        async def open(self, command: BrowserOpenCommand) -> FakeEngineSession:
            if self.open_calls == 0:
                self.open_calls += 1
                raise RuntimeError("browser startup failed")
            return await super().open(command)

    engine = FlakyEngine()
    manager = RunnerBrowserManager(
        node_id="local",
        paths=RunnerPaths(tmp_path),
        engine=engine,
    )

    with pytest.raises(RuntimeError, match="browser startup failed"):
        await manager.open(open_command())

    await manager.open(open_command())
    assert engine.open_calls == 2


async def test_manager_produces_bounded_observation_and_checks_versions(
    tmp_path: Path,
) -> None:
    engine = FakeEngine()
    manager = RunnerBrowserManager(
        node_id="local", paths=RunnerPaths(tmp_path), engine=engine
    )

    opened = await manager.open(open_command())
    assert opened.result.observation is not None
    assert opened.result.observation.observation_version == 1
    assert opened.result.observation.visible_text_excerpt == "Visible body"
    assert opened.result.attachment is not None
    assert opened.attachment_content == b"png"

    with pytest.raises(ValueError, match="stale observation"):
        await manager.act(
            BrowserActCommand(
                session_id="browser-1",
                action=BrowserAction(
                    action_key="stale",
                    browser_session_id="browser-1",
                    page_id="page-1",
                    observation_version=2,
                    action=BrowserActionType.CLICK,
                    element_ref="e-1",
                ),
            )
        )

    acted = await manager.act(
        BrowserActCommand(
            session_id="browser-1",
            action=BrowserAction(
                action_key="click-1",
                browser_session_id="browser-1",
                page_id="page-1",
                observation_version=1,
                action=BrowserActionType.CLICK,
                element_ref="e-1",
            ),
        )
    )
    assert acted.result.action is not None
    assert acted.result.action.result_observation_id == acted.result.observation.id
    assert acted.result.observation.url == "https://example.com/next"
    assert acted.result.observation.observation_version == 2


async def test_takeover_blocks_agent_writes_and_release_summarizes_changes(
    tmp_path: Path,
) -> None:
    engine = FakeEngine()
    manager = RunnerBrowserManager(
        node_id="local", paths=RunnerPaths(tmp_path), engine=engine
    )
    await manager.open(open_command())

    takeover = await manager.takeover(BrowserSessionCommand(session_id="browser-1"))
    assert takeover.result.session.owner is BrowserOwner.USER
    with pytest.raises(PermissionError, match="Agent writes are blocked"):
        await manager.act(
            BrowserActCommand(
                session_id="browser-1",
                action=BrowserAction(
                    action_key="blocked",
                    browser_session_id="browser-1",
                    page_id="page-1",
                    observation_version=1,
                    action=BrowserActionType.CLICK,
                    element_ref="e-1",
                ),
            )
        )

    engine.sessions["browser-1"].url = "https://example.com/user"
    engine.sessions["browser-1"].storage = "after"
    engine.sessions["browser-1"].downloads.append(
        (
            BrowserAttachment(
                kind="download",
                name="user-download.txt",
                mime_type="text/plain",
            ),
            b"downloaded",
        )
    )
    await manager.observe(
        BrowserObserveCommand(session_id="browser-1", include_screenshot=False)
    )
    released = await manager.release(BrowserSessionCommand(session_id="browser-1"))
    assert released.result.session.owner is BrowserOwner.AGENT
    assert released.result.takeover_summary is not None
    assert released.result.takeover_summary.storage_changed is True
    assert "https://example.com/user" in released.result.takeover_summary.url_changes
    assert released.result.attachment is not None
    assert released.result.attachment.name == "user-download.txt"
    assert released.attachment_content == b"downloaded"


async def test_manager_enforces_scope_for_initial_and_clicked_urls(tmp_path: Path) -> None:
    manager = RunnerBrowserManager(
        node_id="local", paths=RunnerPaths(tmp_path), engine=FakeEngine()
    )
    rejected = open_command().model_copy(update={"url": "https://outside.example/start"})
    with pytest.raises(ValueError, match="outside authorized scope"):
        await manager.open(rejected)

async def test_user_takeover_redacts_out_of_scope_content_on_release(tmp_path: Path) -> None:
    engine = FakeEngine()
    manager = RunnerBrowserManager(
        node_id="local", paths=RunnerPaths(tmp_path), engine=engine
    )
    await manager.open(open_command())
    await manager.takeover(BrowserSessionCommand(session_id="browser-1"))
    engine.sessions["browser-1"].url = "https://outside.invalid/private"
    released = await manager.release(BrowserSessionCommand(session_id="browser-1"))

    assert released.result.observation is not None
    assert released.result.observation.url == "about:blank#riftx-out-of-scope"
    assert released.result.observation.interactive_elements == []
    assert released.result.attachment is None
