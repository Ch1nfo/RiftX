"""Runner-local managed Chromium runtime and remote browser command routing."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import mimetypes
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from riftx.browser.models import (
    BrowserActCommand,
    BrowserAttachment,
    BrowserObserveCommand,
    BrowserOpenCommand,
    BrowserOperation,
    BrowserRuntimeExchange,
    BrowserRuntimeResult,
    BrowserSessionCommand,
)
from riftx.domain import (
    RUNNER_STOP_ACK_BROWSER_SCHEMA,
    BrowserAction,
    BrowserActionStatus,
    BrowserActionType,
    BrowserMode,
    BrowserObservation,
    BrowserOwner,
    BrowserPage,
    BrowserPageStatus,
    BrowserSession,
    BrowserSessionStatus,
    BrowserTakeoverSummary,
    FormFieldSummary,
    FormSummary,
    InteractiveElement,
    NetworkEventSummary,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandStatus,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
)
from riftx.domain.base import new_id, utc_now
from riftx.scope import ScopeGuard, ScopeTargetKind, ScopeViolationError

from .paths import RunnerPaths

_MAX_VISIBLE_TEXT = 20_000
_MAX_HEADINGS = 100
_MAX_ELEMENTS = 300
_MAX_FORMS = 50
_MAX_NETWORK_EVENTS = 100


class BrowserRunner(Protocol):
    async def open(self, command: BrowserOpenCommand) -> BrowserRuntimeExchange: ...

    async def observe(self, command: BrowserObserveCommand) -> BrowserRuntimeExchange: ...

    async def act(self, command: BrowserActCommand) -> BrowserRuntimeExchange: ...

    async def takeover(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange: ...

    async def release(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange: ...

    async def close(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange: ...


class BrowserCommandControl(Protocol):
    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
        run_id: str,
        origin: RunnerCommandOrigin,
        operation_family: RunnerOperationFamily,
        resource_kind: RunnerResourceKind,
        resource_id: str,
        execution_id: str | None = None,
        output_contract: RunnerOutputContract | None = None,
        target: RunnerPrincipal | None = None,
    ) -> tuple[RunnerCommand, bool]: ...

    async def wait_command(
        self,
        command_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.1,
    ) -> RunnerCommand: ...

    async def read_command_output(self, command_id: str) -> bytes: ...


class BrowserEngineSession(Protocol):
    profile_path: str | None

    async def pages(self) -> list[BrowserPage]: ...

    async def observe(
        self,
        page_id: str,
        *,
        browser_session_id: str,
        version: int,
        include_screenshot: bool,
        include_network: bool,
    ) -> tuple[BrowserPage, BrowserObservation, bytes]: ...

    async def act(self, action: BrowserAction) -> tuple[BrowserAttachment | None, bytes]: ...

    async def storage_digest(self) -> str: ...

    async def download_count(self) -> int: ...

    async def downloads_since(self, index: int) -> list[tuple[BrowserAttachment, bytes]]: ...

    async def close(self) -> None: ...


class BrowserEngine(Protocol):
    async def open(self, command: BrowserOpenCommand) -> BrowserEngineSession: ...


@dataclass(slots=True)
class _ManagedBrowserSession:
    session: BrowserSession
    scope: ScopeGuard
    engine: BrowserEngineSession
    observations: list[BrowserObservation] = field(default_factory=list)
    actions: dict[str, BrowserAction] = field(default_factory=dict)
    takeover_page_ids: set[str] = field(default_factory=set)
    takeover_storage_digest: str | None = None
    takeover_download_cursor: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RunnerBrowserManager:
    """Owns browser processes on one Runner and enforces observation/action ordering."""

    def __init__(
        self,
        *,
        node_id: str,
        paths: RunnerPaths,
        engine: BrowserEngine | None = None,
    ) -> None:
        self._node_id = node_id
        self._paths = paths
        self._engine = engine or PlaywrightBrowserEngine(paths)
        self._sessions: dict[str, _ManagedBrowserSession] = {}
        self._persistent_profiles: dict[str, str] = {}

    async def open(self, command: BrowserOpenCommand) -> BrowserRuntimeExchange:
        if command.node_id != self._node_id:
            raise ValueError("Browser launch targets a different Runner node")
        guard = ScopeGuard(command.scope)
        guard.require(command.url, kind=ScopeTargetKind.URL)
        existing = self._sessions.get(command.session_id)
        if existing is not None:
            self._require_managed_identity(
                existing,
                run_id=command.run_id,
                node_id=command.node_id,
            )
            async with existing.lock:
                return await self._observe_exchange(
                    existing,
                    existing.session.current_page_id,
                    include_screenshot=command.include_screenshot,
                    include_network=True,
                )

        if command.mode is BrowserMode.MANAGED_PERSISTENT and command.profile_id:
            active_session = self._persistent_profiles.get(command.profile_id)
            if active_session is not None:
                raise RuntimeError(
                    f"Browser profile {command.profile_id!r} is already used by "
                    f"session {active_session!r}"
                )

        engine_session = await self._engine.open(command)
        managed: _ManagedBrowserSession | None = None
        try:
            pages = await engine_session.pages()
            if not pages:
                raise RuntimeError("Browser engine did not create or expose a page")
            session = BrowserSession(
                id=command.session_id,
                run_id=command.run_id,
                agent_session_id=command.agent_session_id,
                node_id=command.node_id,
                mode=command.mode,
                status=BrowserSessionStatus.CREATED,
                owner=BrowserOwner.AGENT,
                browser_type="chromium",
                profile_id=command.profile_id,
                profile_path=engine_session.profile_path,
                cdp_endpoint=command.cdp_endpoint,
            )
            session.transition_to(BrowserSessionStatus.STARTING)
            for page in pages:
                session.register_page(page.id, make_current=False)
            session.current_page_id = pages[0].id
            session.transition_to(BrowserSessionStatus.ACTIVE)
            managed = _ManagedBrowserSession(
                session=session,
                scope=guard,
                engine=engine_session,
            )
            self._sessions[session.id] = managed
            if command.mode is BrowserMode.MANAGED_PERSISTENT and command.profile_id:
                self._persistent_profiles[command.profile_id] = session.id
            async with managed.lock:
                return await self._observe_exchange(
                    managed,
                    session.current_page_id,
                    include_screenshot=command.include_screenshot,
                    include_network=True,
                )
        except BaseException:
            self._sessions.pop(command.session_id, None)
            if command.profile_id is not None:
                self._persistent_profiles.pop(command.profile_id, None)
            try:
                await asyncio.shield(engine_session.close())
            except BaseException:
                pass
            raise

    async def observe(self, command: BrowserObserveCommand) -> BrowserRuntimeExchange:
        managed = self._require(command.session_id)
        self._require_managed_identity(
            managed,
            run_id=command.run_id,
            node_id=command.node_id,
        )
        async with managed.lock:
            return await self._observe_exchange(
                managed,
                command.page_id,
                include_screenshot=command.include_screenshot,
                include_network=command.include_network,
            )

    async def act(self, command: BrowserActCommand) -> BrowserRuntimeExchange:
        managed = self._require(command.session_id)
        self._require_managed_identity(
            managed,
            run_id=command.run_id,
            node_id=command.node_id,
        )
        action = command.action
        async with managed.lock:
            previous = managed.actions.get(action.action_key)
            if previous is not None:
                if _action_fingerprint(previous) != _action_fingerprint(action):
                    raise ValueError("Browser action key was reused with different arguments")
                if previous.status is BrowserActionStatus.FAILED:
                    raise RuntimeError(previous.error or "Previous browser action failed")
                if previous.status is not BrowserActionStatus.COMPLETED:
                    raise RuntimeError("Browser action with this key is already running")
                observation = _observation_by_id(
                    managed.observations, previous.result_observation_id
                )
                return BrowserRuntimeExchange(
                    result=BrowserRuntimeResult(
                        session=managed.session,
                        pages=await managed.engine.pages(),
                        observation=observation,
                        action=previous,
                    )
                )
            if not managed.session.agent_can_write:
                raise PermissionError(
                    "Browser session is owned by "
                    f"{managed.session.owner.value}; Agent writes are blocked"
                )
            latest = self._latest_for_page(managed, action.page_id)
            if latest is None or latest.observation_version != action.observation_version:
                actual = latest.observation_version if latest is not None else 0
                raise ValueError(
                    "Browser action used a stale observation version "
                    f"{action.observation_version}; latest is {actual}"
                )
            self._validate_action_scope(managed, action, latest)
            running = action.model_copy(update={"status": BrowserActionStatus.RUNNING, "error": ""})
            managed.actions[action.action_key] = running
            try:
                attachment, content = await managed.engine.act(running)
                exchange = await self._observe_exchange(
                    managed,
                    action.page_id,
                    include_screenshot=command.include_screenshot and attachment is None,
                    include_network=True,
                )
                observation = exchange.result.observation
                if observation is None:
                    raise RuntimeError("Browser action did not produce an observation")
                managed.scope.require(observation.url, kind=ScopeTargetKind.URL)
                completed = running.model_copy(
                    update={
                        "status": BrowserActionStatus.COMPLETED,
                        "result_observation_id": observation.id,
                        "completed_at": utc_now(),
                    }
                )
                managed.actions[action.action_key] = completed
                result_attachment = attachment or exchange.result.attachment
                result_content = content or exchange.attachment_content
                return BrowserRuntimeExchange(
                    result=exchange.result.model_copy(
                        update={"action": completed, "attachment": result_attachment}
                    ),
                    attachment_content=result_content,
                )
            except ScopeViolationError as exc:
                try:
                    await managed.engine.act(
                        BrowserAction(
                            action_key=f"{action.action_key}:scope-recovery",
                            browser_session_id=managed.session.id,
                            page_id=action.page_id,
                            observation_version=action.observation_version,
                            action=BrowserActionType.GO_BACK,
                        )
                    )
                    await self._observe_exchange(
                        managed,
                        action.page_id,
                        include_screenshot=False,
                        include_network=True,
                    )
                except Exception:
                    pass
                message = "Browser action navigated outside authorized scope"
                managed.actions[action.action_key] = running.model_copy(
                    update={
                        "status": BrowserActionStatus.FAILED,
                        "error": message,
                        "completed_at": utc_now(),
                    }
                )
                raise ScopeViolationError(message) from exc
            except Exception as exc:
                managed.actions[action.action_key] = running.model_copy(
                    update={
                        "status": BrowserActionStatus.FAILED,
                        "error": str(exc)[:8192],
                        "completed_at": utc_now(),
                    }
                )
                raise

    async def takeover(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        managed = self._require(command.session_id)
        self._require_managed_identity(
            managed,
            run_id=command.run_id or (command.session.run_id if command.session else None),
            node_id=command.node_id or (command.session.node_id if command.session else None),
        )
        async with managed.lock:
            latest = self._latest(managed)
            if managed.session.owner is not BrowserOwner.USER:
                version = latest.observation_version if latest is not None else 0
                managed.session.take_over(observation_version=version)
                managed.takeover_page_ids = set(managed.session.page_ids)
                managed.takeover_storage_digest = await managed.engine.storage_digest()
                managed.takeover_download_cursor = await managed.engine.download_count()
            return BrowserRuntimeExchange(
                result=BrowserRuntimeResult(
                    session=managed.session,
                    pages=await managed.engine.pages(),
                    observation=latest,
                )
            )

    async def release(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        managed = self._require(command.session_id)
        self._require_managed_identity(
            managed,
            run_id=command.run_id or (command.session.run_id if command.session else None),
            node_id=command.node_id or (command.session.node_id if command.session else None),
        )
        async with managed.lock:
            if managed.session.owner is not BrowserOwner.USER:
                raise ValueError("Browser session is not under user takeover")
            started_version = managed.session.takeover_observation_version or 0
            started_at = managed.session.takeover_started_at
            exchange = await self._observe_exchange(
                managed,
                managed.session.current_page_id,
                include_screenshot=True,
                include_network=True,
            )
            observation = exchange.result.observation
            assert observation is not None
            current_pages = await managed.engine.pages()
            current_digest = await managed.engine.storage_digest()
            downloads = await managed.engine.downloads_since(managed.takeover_download_cursor)
            takeover_observations = [
                item for item in managed.observations if item.observation_version > started_version
            ]
            urls: list[str] = []
            network_by_sequence: dict[int, NetworkEventSummary] = {}
            for item in takeover_observations:
                if not urls or urls[-1] != item.url:
                    urls.append(item.url)
                for event in item.recent_network_summary:
                    network_by_sequence[event.sequence] = event
            network = [network_by_sequence[key] for key in sorted(network_by_sequence)]
            opened_pages = [
                page.id for page in current_pages if page.id not in managed.takeover_page_ids
            ]
            summary = BrowserTakeoverSummary(
                run_id=managed.session.run_id,
                browser_session_id=managed.session.id,
                started_observation_version=started_version,
                ended_observation_version=observation.observation_version,
                started_at=started_at,
                url_changes=urls[-100:],
                opened_page_ids=opened_pages[-100:],
                network_summary=network[-_MAX_NETWORK_EVENTS:],
                storage_changed=(
                    managed.takeover_storage_digest is not None
                    and current_digest != managed.takeover_storage_digest
                ),
                summary=_takeover_summary_text(
                    urls=urls,
                    opened_pages=opened_pages,
                    network_count=len(network),
                    storage_changed=(
                        managed.takeover_storage_digest is not None
                        and current_digest != managed.takeover_storage_digest
                    ),
                ),
            )
            managed.session.release()
            managed.takeover_page_ids.clear()
            managed.takeover_storage_digest = None
            managed.takeover_download_cursor = 0
            attachment, attachment_content = _takeover_download_attachment(
                managed.session.id, downloads
            )
            if attachment is None:
                attachment = exchange.result.attachment
                attachment_content = exchange.attachment_content
            return BrowserRuntimeExchange(
                result=exchange.result.model_copy(
                    update={
                        "session": managed.session,
                        "takeover_summary": summary,
                        "attachment": attachment,
                    }
                ),
                attachment_content=attachment_content,
            )

    async def close(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        managed = self._require(command.session_id)
        self._require_managed_identity(
            managed,
            run_id=command.run_id or (command.session.run_id if command.session else None),
            node_id=command.node_id or (command.session.node_id if command.session else None),
        )
        async with managed.lock:
            pages = await managed.engine.pages()
            await managed.engine.close()
            if managed.session.status not in {
                BrowserSessionStatus.CLOSED,
                BrowserSessionStatus.LOST,
            }:
                managed.session.transition_to(BrowserSessionStatus.CLOSED)
            for page in pages:
                page.status = BrowserPageStatus.CLOSED
                page.closed_at = utc_now()
            self._sessions.pop(command.session_id, None)
            if managed.session.profile_id is not None:
                self._persistent_profiles.pop(managed.session.profile_id, None)
            return BrowserRuntimeExchange(
                result=BrowserRuntimeResult(session=managed.session, pages=pages)
            )

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            try:
                await self.close(BrowserSessionCommand(session_id=session_id))
            except Exception:
                self._sessions.pop(session_id, None)

    def _require(self, session_id: str) -> _ManagedBrowserSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise KeyError(f"Browser session {session_id!r} is not active on this Runner") from exc

    @staticmethod
    def _require_managed_identity(
        managed: _ManagedBrowserSession,
        *,
        run_id: str | None,
        node_id: str | None,
    ) -> None:
        if run_id is not None and run_id != managed.session.run_id:
            raise RuntimeError("Browser command Run does not own the local session")
        if node_id is not None and node_id != managed.session.node_id:
            raise RuntimeError("Browser command node does not own the local session")

    async def _observe_exchange(
        self,
        managed: _ManagedBrowserSession,
        page_id: str | None,
        *,
        include_screenshot: bool,
        include_network: bool,
    ) -> BrowserRuntimeExchange:
        target_page_id = page_id or managed.session.current_page_id
        if target_page_id is None:
            raise RuntimeError("Browser session has no current page")
        version = (self._latest(managed).observation_version if managed.observations else 0) + 1
        page, observation, screenshot = await managed.engine.observe(
            target_page_id,
            browser_session_id=managed.session.id,
            version=version,
            include_screenshot=include_screenshot,
            include_network=include_network,
        )
        try:
            managed.scope.require(observation.url, kind=ScopeTargetKind.URL)
        except ScopeViolationError:
            if managed.session.owner is not BrowserOwner.USER:
                raise
            observation = _redact_out_of_scope_observation(observation, managed.scope)
            screenshot = b""
        pages = await managed.engine.pages()
        if managed.session.owner is BrowserOwner.USER:
            pages = [_redact_out_of_scope_page(item, managed.scope) for item in pages]
        managed.session.page_ids = []
        for item in pages:
            managed.session.register_page(item.id, make_current=False)
        managed.session.current_page_id = page.id
        managed.observations.append(observation)
        attachment = None
        if screenshot:
            attachment = BrowserAttachment(
                kind="screenshot",
                name=f"browser-{managed.session.id}-{page.id}-v{version}.png",
                mime_type="image/png",
                description="Managed browser observation screenshot",
                metadata={"page_id": page.id, "observation_version": version},
            )
        return BrowserRuntimeExchange(
            result=BrowserRuntimeResult(
                session=managed.session,
                pages=pages,
                observation=observation,
                attachment=attachment,
            ),
            attachment_content=screenshot,
        )

    def _latest(self, managed: _ManagedBrowserSession) -> BrowserObservation | None:
        return managed.observations[-1] if managed.observations else None

    def _latest_for_page(
        self, managed: _ManagedBrowserSession, page_id: str
    ) -> BrowserObservation | None:
        return next(
            (item for item in reversed(managed.observations) if item.page_id == page_id),
            None,
        )

    def _validate_action_scope(
        self,
        managed: _ManagedBrowserSession,
        action: BrowserAction,
        observation: BrowserObservation,
    ) -> None:
        if action.action is BrowserActionType.NAVIGATE and action.url:
            managed.scope.require(action.url, kind=ScopeTargetKind.URL)
        if action.element_ref:
            element = next(
                (
                    item
                    for item in observation.interactive_elements
                    if item.ref == action.element_ref
                ),
                None,
            )
            if element is None:
                raise ValueError(
                    f"Element ref {action.element_ref!r} is not present in the observation"
                )
            if element.disabled:
                raise ValueError(f"Element ref {action.element_ref!r} is disabled")
            if action.action in {BrowserActionType.CLICK, BrowserActionType.DOWNLOAD}:
                if element.href:
                    managed.scope.require(
                        urljoin(observation.url, element.href), kind=ScopeTargetKind.URL
                    )


class PlaywrightBrowserEngine:
    """Lazy Playwright adapter; importing RiftX does not require browser binaries."""

    def __init__(self, paths: RunnerPaths) -> None:
        self._paths = paths

    async def open(self, command: BrowserOpenCommand) -> BrowserEngineSession:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional runtime install
            raise RuntimeError(
                "Playwright is not installed; install `riftx[browser]`, then run "
                "`playwright install chromium` on the Runner"
            ) from exc
        playwright = await async_playwright().start()
        browser = None
        context = None
        profile_path: str | None = None
        try:
            if command.mode is BrowserMode.ATTACHED_CDP:
                browser = await playwright.chromium.connect_over_cdp(command.cdp_endpoint)
                contexts = browser.contexts
                context = contexts[0] if contexts else await browser.new_context()
            elif command.mode is BrowserMode.MANAGED_PERSISTENT:
                profile = self._paths.browser_profile(command.profile_id or "default")
                profile.mkdir(parents=True, exist_ok=True)
                profile_path = str(profile)
                context = await playwright.chromium.launch_persistent_context(
                    profile,
                    headless=command.headless,
                    accept_downloads=True,
                )
            else:
                browser = await playwright.chromium.launch(headless=command.headless)
                context = await browser.new_context(accept_downloads=True)
            assert context is not None
            pages = list(context.pages)
            page = pages[0] if pages else await context.new_page()
            await page.goto(command.url, wait_until="domcontentloaded")
            await _wait_for_stability(page)
            return _PlaywrightSession(
                playwright=playwright,
                browser=browser,
                context=context,
                profile_path=profile_path,
                browser_session_id=command.session_id,
                attached=command.mode is BrowserMode.ATTACHED_CDP,
            )
        except BaseException:
            if context is not None and command.mode is not BrowserMode.ATTACHED_CDP:
                try:
                    await asyncio.shield(context.close())
                except BaseException:
                    pass
            if browser is not None:
                try:
                    await asyncio.shield(browser.close())
                except BaseException:
                    pass
            try:
                await asyncio.shield(playwright.stop())
            except BaseException:
                pass
            raise


class _PlaywrightSession:
    def __init__(
        self,
        *,
        playwright: Any,
        browser: Any,
        context: Any,
        profile_path: str | None,
        browser_session_id: str,
        attached: bool,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self.profile_path = profile_path
        self._browser_session_id = browser_session_id
        self._attached = attached
        self._page_ids: dict[Any, str] = {}
        self._pages_by_id: dict[str, Any] = {}
        self._next_page = 1
        self._network: dict[str, list[NetworkEventSummary]] = {}
        self._network_sequence = 0
        self._console_errors: dict[str, list[str]] = {}
        self._alerts: dict[str, list[str]] = {}
        self._downloads: list[tuple[BrowserAttachment, bytes]] = []
        self._download_tasks: set[asyncio.Task[None]] = set()
        for page in context.pages:
            self._register_page(page)
        context.on("page", self._register_page)

    def _register_page(self, page: Any) -> None:
        if page in self._page_ids:
            return
        page_id = f"page-{self._next_page}"
        self._next_page += 1
        self._page_ids[page] = page_id
        self._pages_by_id[page_id] = page
        self._network[page_id] = []
        self._console_errors[page_id] = []
        self._alerts[page_id] = []
        page.on("response", lambda response: self._record_response(page_id, response))
        page.on("requestfailed", lambda request: self._record_failed(page_id, request))
        page.on("console", lambda message: self._record_console(page_id, message))
        page.on("dialog", lambda dialog: self._record_dialog(page_id, dialog))
        page.on("download", self._schedule_download_capture)

    def _schedule_download_capture(self, download: Any) -> None:
        task = asyncio.create_task(self._capture_download(download))
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)

    async def _capture_download(self, download: Any) -> None:
        try:
            path = await download.path()
            if path is None:
                return
            content = await asyncio.to_thread(Path(path).read_bytes)
            name = _safe_download_name(download.suggested_filename)
            self._downloads.append(
                (
                    BrowserAttachment(
                        kind="download",
                        name=name,
                        mime_type=(mimetypes.guess_type(name)[0] or "application/octet-stream"),
                        description="File downloaded during browser user takeover",
                    ),
                    content,
                )
            )
        except Exception:
            return

    def _record_response(self, page_id: str, response: Any) -> None:
        self._network_sequence += 1
        request = response.request
        self._append_network(
            page_id,
            NetworkEventSummary(
                sequence=self._network_sequence,
                method=request.method,
                url=response.url,
                resource_type=request.resource_type or "",
                status_code=response.status,
            ),
        )

    def _record_failed(self, page_id: str, request: Any) -> None:
        self._network_sequence += 1
        failure = request.failure
        self._append_network(
            page_id,
            NetworkEventSummary(
                sequence=self._network_sequence,
                method=request.method,
                url=request.url,
                resource_type=request.resource_type or "",
                failed=True,
                failure_text=(failure or "request failed")[:2000],
            ),
        )

    def _append_network(self, page_id: str, event: NetworkEventSummary) -> None:
        events = self._network.setdefault(page_id, [])
        events.append(event)
        del events[:-_MAX_NETWORK_EVENTS]

    def _record_console(self, page_id: str, message: Any) -> None:
        if message.type == "error":
            errors = self._console_errors.setdefault(page_id, [])
            errors.append(str(message.text)[:2000])
            del errors[:-100]

    def _record_dialog(self, page_id: str, dialog: Any) -> None:
        alerts = self._alerts.setdefault(page_id, [])
        alerts.append(f"{dialog.type}: {dialog.message}"[:2000])
        del alerts[:-50]
        asyncio.create_task(dialog.dismiss())

    async def pages(self) -> list[BrowserPage]:
        items: list[BrowserPage] = []
        for page, page_id in list(self._page_ids.items()):
            if page.is_closed():
                continue
            items.append(
                BrowserPage(
                    id=page_id,
                    browser_session_id=self._browser_session_id,
                    url=page.url,
                    title=await _safe_title(page),
                )
            )
        return items

    async def observe(
        self,
        page_id: str,
        *,
        browser_session_id: str,
        version: int,
        include_screenshot: bool,
        include_network: bool,
    ) -> tuple[BrowserPage, BrowserObservation, bytes]:
        page = self._require_page(page_id)
        raw = await page.evaluate(_OBSERVATION_SCRIPT)
        interactive = [InteractiveElement.model_validate(item) for item in raw["elements"]]
        forms = [
            FormSummary(
                ref=item["ref"],
                action=item.get("action"),
                method=item.get("method"),
                fields=[FormFieldSummary.model_validate(field) for field in item["fields"]],
            )
            for item in raw["forms"]
        ]
        title = (await _safe_title(page))[:2000]
        page_model = BrowserPage(
            id=page_id,
            browser_session_id=browser_session_id,
            url=page.url,
            title=title,
            last_observation_version=version,
        )
        observation = BrowserObservation(
            browser_session_id=browser_session_id,
            page_id=page_id,
            url=page.url,
            title=title,
            visible_text_excerpt=str(raw["visible_text"])[:_MAX_VISIBLE_TEXT],
            headings=[str(item)[:1000] for item in raw["headings"][:_MAX_HEADINGS]],
            interactive_elements=interactive[:_MAX_ELEMENTS],
            forms=forms[:_MAX_FORMS],
            alerts=list(self._alerts.get(page_id, []))[-50:],
            console_errors=list(self._console_errors.get(page_id, []))[-100:],
            recent_network_summary=(
                list(self._network.get(page_id, []))[-_MAX_NETWORK_EVENTS:]
                if include_network
                else []
            ),
            observation_version=version,
        )
        screenshot = await page.screenshot(type="png") if include_screenshot else b""
        return page_model, observation, screenshot

    async def act(self, action: BrowserAction) -> tuple[BrowserAttachment | None, bytes]:
        page = self._require_page(action.page_id)
        locator = None
        if action.element_ref:
            locator = page.locator(f'[data-riftx-ref="{action.element_ref}"]').first
        if action.action is BrowserActionType.NAVIGATE:
            await page.goto(action.url, wait_until="domcontentloaded")
        elif action.action is BrowserActionType.CLICK:
            assert locator is not None
            await locator.click()
        elif action.action is BrowserActionType.FILL:
            assert locator is not None
            await locator.fill(action.value or "")
        elif action.action is BrowserActionType.TYPE:
            assert locator is not None
            await locator.press_sequentially(
                action.value or "", delay=float(action.options.get("delay_ms", 0))
            )
        elif action.action is BrowserActionType.SELECT:
            assert locator is not None
            await locator.select_option(action.value or "")
        elif action.action is BrowserActionType.PRESS:
            assert locator is not None
            await locator.press(action.value or str(action.options.get("key", "Enter")))
        elif action.action is BrowserActionType.SCROLL:
            delta_x = float(action.options.get("delta_x", 0))
            delta_y = float(action.options.get("delta_y", 700))
            await page.mouse.wheel(delta_x, delta_y)
        elif action.action is BrowserActionType.UPLOAD:
            assert locator is not None
            if not action.value:
                raise ValueError("upload requires a Runner-local file path")
            await locator.set_input_files(action.value)
        elif action.action is BrowserActionType.DOWNLOAD:
            assert locator is not None
            async with page.expect_download() as download_info:
                await locator.click()
            download = await download_info.value
            path = await download.path()
            if path is None:
                raise RuntimeError("Browser download did not produce a local file")
            content = await asyncio.to_thread(Path(path).read_bytes)
            name = _safe_download_name(download.suggested_filename)
            return (
                BrowserAttachment(
                    kind="download",
                    name=name,
                    mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                    description="File downloaded by managed browser action",
                    metadata={"page_id": action.page_id},
                ),
                content,
            )
        elif action.action is BrowserActionType.WAIT:
            await page.wait_for_timeout(float(action.options.get("milliseconds", 500)))
        elif action.action is BrowserActionType.EVALUATE:
            expression = action.options.get("expression")
            if not isinstance(expression, str) or not expression:
                raise ValueError("evaluate requires options.expression")
            await page.evaluate(expression)
        elif action.action is BrowserActionType.GO_BACK:
            await page.go_back(wait_until="domcontentloaded")
        elif action.action is BrowserActionType.RELOAD:
            await page.reload(wait_until="domcontentloaded")
        else:  # pragma: no cover - enum exhaustiveness guard
            raise ValueError(f"Unsupported browser action {action.action.value!r}")
        await _wait_for_stability(page)
        return None, b""

    async def storage_digest(self) -> str:
        cookies = await self._context.cookies()
        origins: list[dict[str, object]] = []
        for page in self._pages_by_id.values():
            if page.is_closed() or urlsplit(page.url).scheme not in {"http", "https"}:
                continue
            try:
                storage = await page.evaluate(
                    """() => ({
                      origin: location.origin,
                      local: Object.keys(localStorage).sort(),
                      session: Object.keys(sessionStorage).sort()
                    })"""
                )
            except Exception:
                continue
            origins.append(storage)
        encoded = json.dumps(
            {"cookies": cookies, "origins": origins},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    async def download_count(self) -> int:
        if self._download_tasks:
            await asyncio.gather(*tuple(self._download_tasks), return_exceptions=True)
        return len(self._downloads)

    async def downloads_since(self, index: int) -> list[tuple[BrowserAttachment, bytes]]:
        if self._download_tasks:
            await asyncio.gather(*tuple(self._download_tasks), return_exceptions=True)
        return list(self._downloads[max(index, 0) :])

    async def close(self) -> None:
        try:
            if not self._attached:
                await self._context.close()
                if self._browser is not None:
                    await self._browser.close()
        finally:
            await self._playwright.stop()

    def _require_page(self, page_id: str) -> Any:
        try:
            page = self._pages_by_id[page_id]
        except KeyError as exc:
            raise KeyError(f"Browser page {page_id!r} is not active") from exc
        if page.is_closed():
            raise KeyError(f"Browser page {page_id!r} is closed")
        return page


class RemoteBrowserClient:
    """Dispatch browser operations to an authenticated remote Runner."""

    def __init__(self, *, node_id: str, control: BrowserCommandControl) -> None:
        self._node_id = node_id
        self._control = control

    async def open(self, command: BrowserOpenCommand) -> BrowserRuntimeExchange:
        return await self._dispatch(
            BrowserOperation.OPEN,
            command.model_dump(mode="json"),
            idempotency_key=f"browser:{command.session_id}:open",
        )

    async def observe(self, command: BrowserObserveCommand) -> BrowserRuntimeExchange:
        return await self._dispatch(
            BrowserOperation.OBSERVE,
            command.model_dump(mode="json"),
            idempotency_key=f"browser:{command.session_id}:observe:{_nonce()}",
        )

    async def act(self, command: BrowserActCommand) -> BrowserRuntimeExchange:
        return await self._dispatch(
            BrowserOperation.ACT,
            command.model_dump(mode="json"),
            idempotency_key=f"browser:{command.session_id}:act:{command.action.action_key}",
        )

    async def takeover(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        return await self._dispatch(
            BrowserOperation.TAKEOVER,
            command.model_dump(mode="json"),
            idempotency_key=f"browser:{command.session_id}:takeover:{_nonce()}",
        )

    async def release(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        return await self._dispatch(
            BrowserOperation.RELEASE,
            command.model_dump(mode="json"),
            idempotency_key=f"browser:{command.session_id}:release:{_nonce()}",
        )

    async def close(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        return await self._dispatch(
            BrowserOperation.CLOSE,
            command.model_dump(mode="json"),
            # The local tombstone makes close idempotent.  A fresh command id
            # lets a retry recover after an earlier offline/failed delivery.
            idempotency_key=f"browser:{command.session_id}:close:{_nonce()}",
            kind=RunnerCommandKind.BROWSER_CLOSE,
        )

    async def _dispatch(
        self,
        operation: BrowserOperation,
        command: dict[str, object],
        *,
        idempotency_key: str,
        kind: RunnerCommandKind = RunnerCommandKind.BROWSER,
    ) -> BrowserRuntimeExchange:
        run_id = command.get("run_id")
        node_id = command.get("node_id")
        session_id = command.get("session_id")
        raw_session = command.get("session")
        if run_id is None and isinstance(raw_session, dict):
            run_id = raw_session.get("run_id")
        if node_id is None and isinstance(raw_session, dict):
            node_id = raw_session.get("node_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Remote browser command is missing its authoritative Run")
        if node_id != self._node_id:
            raise ValueError("Remote browser command targets a different Runner node")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Remote browser command is missing its session identity")
        is_stop = kind is RunnerCommandKind.BROWSER_CLOSE
        runner_command, _ = await self._control.enqueue(
            self._node_id,
            kind=kind,
            idempotency_key=idempotency_key,
            run_id=run_id,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=(
                RunnerOperationFamily.SAFETY_STOP
                if is_stop
                else RunnerOperationFamily.BROWSER
            ),
            resource_kind=RunnerResourceKind.BROWSER_SESSION,
            resource_id=session_id,
            output_contract=RunnerOutputContract(
                max_result_bytes=512 * 1024,
                max_output_bytes=0 if is_stop else 100_000_000,
                allowed_streams=() if is_stop else ("command",),
                result_schema=(
                    "riftx.runner-result/browser-stop/v1"
                    if is_stop
                    else "riftx.runner-result/browser/v1"
                ),
                stop_ack_schema=RUNNER_STOP_ACK_BROWSER_SCHEMA if is_stop else None,
            ),
            payload={
                "operation": operation.value,
                "command": command,
            },
        )
        completed = await self._control.wait_command(runner_command.id, timeout_seconds=330)
        if completed.status is not RunnerCommandStatus.COMPLETED:
            raise RuntimeError(
                f"Remote browser command failed: {completed.error or 'unknown error'}"
            )
        raw = completed.result.get("result")
        if not isinstance(raw, dict):
            raise RuntimeError("Remote browser command omitted its structured result")
        result = BrowserRuntimeResult.model_validate(raw)
        content = await self._control.read_command_output(completed.id)
        return BrowserRuntimeExchange(result=result, attachment_content=content)


class NodeBrowserRouter:
    def __init__(
        self,
        *,
        local_node_id: str,
        local: BrowserRunner,
        remote_factory: Callable[[str], BrowserRunner],
    ) -> None:
        self._local_node_id = local_node_id
        self._local = local
        self._remote_factory = remote_factory

    def _runner(self, node_id: str) -> BrowserRunner:
        return self._local if node_id == self._local_node_id else self._remote_factory(node_id)

    async def open(self, command: BrowserOpenCommand) -> BrowserRuntimeExchange:
        return await self._runner(command.node_id).open(command)

    async def observe(self, command: BrowserObserveCommand) -> BrowserRuntimeExchange:
        return await self._runner_for_session(command.session_id).observe(command)

    async def act(self, command: BrowserActCommand) -> BrowserRuntimeExchange:
        return await self._runner_for_session(command.session_id).act(command)

    async def takeover(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        return await self._runner_for_session(command.session_id).takeover(command)

    async def release(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        return await self._runner_for_session(command.session_id).release(command)

    async def close(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        return await self._runner_for_session(command.session_id).close(command)

    def bind_session(self, session_id: str, node_id: str) -> None:
        if not hasattr(self, "_session_nodes"):
            self._session_nodes: dict[str, str] = {}
        self._session_nodes[session_id] = node_id

    def _runner_for_session(self, session_id: str) -> BrowserRunner:
        try:
            node_id = self._session_nodes[session_id]
        except (AttributeError, KeyError) as exc:
            raise KeyError(f"Browser session {session_id!r} is not bound to a Runner") from exc
        return self._runner(node_id)


async def execute_browser_command(
    manager: BrowserRunner,
    *,
    operation: BrowserOperation,
    payload: dict[str, object],
) -> BrowserRuntimeExchange:
    if operation is BrowserOperation.OPEN:
        return await manager.open(BrowserOpenCommand.model_validate(payload))
    if operation is BrowserOperation.OBSERVE:
        return await manager.observe(BrowserObserveCommand.model_validate(payload))
    if operation is BrowserOperation.ACT:
        return await manager.act(BrowserActCommand.model_validate(payload))
    command = BrowserSessionCommand.model_validate(payload)
    if operation is BrowserOperation.TAKEOVER:
        return await manager.takeover(command)
    if operation is BrowserOperation.RELEASE:
        return await manager.release(command)
    if operation is BrowserOperation.CLOSE:
        return await manager.close(command)
    raise ValueError(f"Unsupported browser operation {operation.value!r}")


def _is_scope_allowed(guard: ScopeGuard, url: str) -> bool:
    try:
        return guard.check(url, kind=ScopeTargetKind.URL).allowed
    except ValueError:
        return False


def _redact_out_of_scope_observation(
    observation: BrowserObservation, guard: ScopeGuard
) -> BrowserObservation:
    network = [
        event for event in observation.recent_network_summary if _is_scope_allowed(guard, event.url)
    ]
    return observation.model_copy(
        update={
            "url": "about:blank#riftx-out-of-scope",
            "title": "Outside authorized scope",
            "visible_text_excerpt": (
                "Page content withheld because the user navigated outside the authorized scope."
            ),
            "headings": [],
            "interactive_elements": [],
            "forms": [],
            "alerts": [],
            "console_errors": [],
            "recent_network_summary": network,
        }
    )


def _redact_out_of_scope_page(page: BrowserPage, guard: ScopeGuard) -> BrowserPage:
    if _is_scope_allowed(guard, page.url):
        return page
    return page.model_copy(
        update={
            "url": "about:blank#riftx-out-of-scope",
            "title": "Outside authorized scope",
        }
    )


def _action_fingerprint(action: BrowserAction) -> str:
    payload = action.model_dump(
        mode="json",
        exclude={
            "id",
            "status",
            "result_observation_id",
            "download_artifact_id",
            "error",
            "created_at",
            "completed_at",
        },
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _observation_by_id(
    observations: list[BrowserObservation], observation_id: str | None
) -> BrowserObservation | None:
    if observation_id is None:
        return None
    return next((item for item in observations if item.id == observation_id), None)


def _takeover_summary_text(
    *, urls: list[str], opened_pages: list[str], network_count: int, storage_changed: bool
) -> str:
    return (
        "User browser takeover ended. "
        f"Observed {len(urls)} URL transition(s), {len(opened_pages)} new page(s), "
        f"and {network_count} network event(s). "
        f"Login/storage state {'changed' if storage_changed else 'did not visibly change'}."
    )


def _takeover_download_attachment(
    session_id: str,
    downloads: list[tuple[BrowserAttachment, bytes]],
) -> tuple[BrowserAttachment | None, bytes]:
    if not downloads:
        return None, b""
    if len(downloads) == 1:
        return downloads[0]
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, (metadata, content) in enumerate(downloads, start=1):
            name = metadata.name
            if name in used:
                name = f"{index}-{name}"
            used.add(name)
            archive.writestr(name, content)
    return (
        BrowserAttachment(
            kind="download",
            name=f"browser-{session_id}-takeover-downloads.zip",
            mime_type="application/zip",
            description="Files downloaded during browser user takeover",
            metadata={"download_count": len(downloads)},
        ),
        buffer.getvalue(),
    )


def _safe_download_name(value: str) -> str:
    name = Path(value).name.strip().replace("\x00", "")
    return name[:255] or "download.bin"


def _nonce() -> str:
    return new_id()


async def _safe_title(page: Any) -> str:
    try:
        return str(await page.title())
    except Exception:
        return ""


async def _wait_for_stability(page: Any) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=2_000)
    except Exception:
        await page.wait_for_timeout(150)


_OBSERVATION_SCRIPT = r"""() => {
  const visible = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      rect.width > 0 && rect.height > 0;
  };
  let next = Number(document.documentElement.dataset.riftxNextRef || '1');
  const refFor = (el) => {
    let ref = el.getAttribute('data-riftx-ref');
    if (!ref) {
      ref = `e-${next++}`;
      el.setAttribute('data-riftx-ref', ref);
    }
    return ref;
  };
  const textOf = (el) => (el.innerText || el.textContent || '')
    .replace(/\s+/g, ' ').trim().slice(0, 1000) || null;
  const candidates = Array.from(document.querySelectorAll(
    'a[href],button,input,select,textarea,[role="button"],[role="link"],[contenteditable="true"],summary'
  )).filter(visible).slice(0, 300);
  const elements = candidates.map((el) => ({
    ref: refFor(el),
    role: el.getAttribute('role') || ({
      A:'link', BUTTON:'button', INPUT:'textbox', SELECT:'combobox', TEXTAREA:'textbox'
    }[el.tagName] || null),
    name: el.getAttribute('aria-label') || el.getAttribute('name') ||
      el.getAttribute('title') || null,
    text: textOf(el),
    input_type: el.getAttribute('type'),
    disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    href: el.getAttribute('href'),
    frame_id: window.frameElement
      ? (window.frameElement.id || window.frameElement.name || null)
      : null
  }));
  const forms = Array.from(document.forms).slice(0, 50).map((form) => ({
    ref: refFor(form),
    action: form.getAttribute('action'),
    method: form.getAttribute('method'),
    fields: Array.from(form.querySelectorAll('input,select,textarea'))
      .slice(0, 100).map((field) => {
      const id = field.id;
      const label = id
        ? document.querySelector(`label[for="${CSS.escape(id)}"]`)
        : field.closest('label');
      return {
        ref: refFor(field),
        name: field.getAttribute('name'),
        label: label ? textOf(label) : (field.getAttribute('aria-label') || null),
        input_type: field.getAttribute('type') || field.tagName.toLowerCase(),
        required: Boolean(field.required)
      };
    })
  }));
  document.documentElement.dataset.riftxNextRef = String(next);
  return {
    visible_text: (document.body?.innerText || '')
      .replace(/\s+/g, ' ').trim().slice(0, 20000),
    headings: Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
      .filter(visible).slice(0, 100).map(textOf).filter(Boolean),
    elements,
    forms
  };
}"""
