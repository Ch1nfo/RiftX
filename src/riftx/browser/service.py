"""Control-plane orchestration for durable managed browser sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
)
from riftx.application.services.artifacts import (
    ArtifactApplicationService,
    RegisterArtifactContent,
)
from riftx.browser.models import (
    BrowserActCommand,
    BrowserObserveCommand,
    BrowserOpenCommand,
    BrowserRuntimeExchange,
    BrowserSessionCommand,
)
from riftx.domain import (
    BrowserAction,
    BrowserActionStatus,
    BrowserActionType,
    BrowserMode,
    BrowserObservation,
    BrowserOwner,
    BrowserPage,
    BrowserSession,
    BrowserSessionStatus,
    BrowserTakeoverSummary,
    Run,
)
from riftx.domain.base import new_id, utc_now
from riftx.runner.browser import BrowserRunner
from riftx.scope import ScopeGuard, ScopeTargetKind


class RunRepository(Protocol):
    async def get(self, run_id: str) -> Run | None: ...


class AgentSession(Protocol):
    id: str
    run_id: str


class AgentSessionRepository(Protocol):
    async def get(self, session_id: str) -> AgentSession | None: ...


class RunEventRepository(Protocol):
    async def append(
        self, run_id: str, event_type: str, payload: dict[str, object]
    ) -> object: ...


class BrowserRepository(Protocol):
    async def create_session(self, item: BrowserSession) -> BrowserSession: ...

    async def get_session(self, session_id: str) -> BrowserSession | None: ...

    async def save_session(self, item: BrowserSession) -> BrowserSession: ...

    async def list_sessions_for_run(self, run_id: str) -> Sequence[BrowserSession]: ...

    async def save_pages(self, pages: Sequence[BrowserPage]) -> list[BrowserPage]: ...

    async def list_pages(self, session_id: str) -> Sequence[BrowserPage]: ...

    async def create_observation(self, item: BrowserObservation) -> BrowserObservation: ...

    async def get_observation(self, observation_id: str) -> BrowserObservation | None: ...

    async def latest_observation(
        self, session_id: str, page_id: str | None = None
    ) -> BrowserObservation | None: ...

    async def observations_after(
        self, session_id: str, version: int, *, limit: int = 100
    ) -> Sequence[BrowserObservation]: ...

    async def create_action(self, item: BrowserAction) -> BrowserAction: ...

    async def get_action(self, session_id: str, action_key: str) -> BrowserAction | None: ...

    async def save_action(self, item: BrowserAction) -> BrowserAction: ...

    async def create_takeover_summary(
        self, item: BrowserTakeoverSummary
    ) -> BrowserTakeoverSummary: ...


@dataclass(frozen=True, slots=True)
class OpenBrowser:
    run_id: str
    agent_session_id: str
    url: str
    mode: BrowserMode = BrowserMode.MANAGED_EPHEMERAL
    profile_id: str | None = None
    cdp_endpoint: str | None = None
    headless: bool = False
    include_screenshot: bool = True


@dataclass(frozen=True, slots=True)
class ActBrowser:
    page_id: str
    observation_version: int
    action: BrowserActionType
    action_key: str
    element_ref: str | None = None
    value: str | None = None
    url: str | None = None
    options: dict[str, object] | None = None
    include_screenshot: bool = True


@dataclass(frozen=True, slots=True)
class BrowserView:
    session: BrowserSession
    pages: Sequence[BrowserPage]
    observation: BrowserObservation | None = None
    action: BrowserAction | None = None
    takeover_summary: BrowserTakeoverSummary | None = None


class BrowserApplicationService:
    def __init__(
        self,
        *,
        runs: RunRepository,
        agent_sessions: AgentSessionRepository,
        repository: BrowserRepository,
        runner: BrowserRunner,
        artifacts: ArtifactApplicationService,
        events: RunEventRepository | None = None,
    ) -> None:
        self._runs = runs
        self._agent_sessions = agent_sessions
        self._repository = repository
        self._runner = runner
        self._artifacts = artifacts
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}

    async def open(self, command: OpenBrowser) -> BrowserView:
        run = await self._require_run(command.run_id)
        agent_session = await self._agent_sessions.get(command.agent_session_id)
        if agent_session is None or agent_session.run_id != run.id:
            raise EntityNotFoundError("AgentSession", command.agent_session_id)
        ScopeGuard(run.scope).require(command.url, kind=ScopeTargetKind.URL)
        session_id = new_id()
        self._bind(session_id, run.node_id)
        try:
            exchange = await self._runner.open(
                BrowserOpenCommand(
                    session_id=session_id,
                    run_id=run.id,
                    agent_session_id=command.agent_session_id,
                    node_id=run.node_id,
                    mode=command.mode,
                    url=command.url,
                    profile_id=command.profile_id,
                    cdp_endpoint=command.cdp_endpoint,
                    scope=run.scope,
                    headless=command.headless,
                    include_screenshot=command.include_screenshot,
                )
            )
            await self._repository.create_session(exchange.result.session)
            view = await self._persist_exchange(exchange)
        except Exception:
            try:
                await self._runner.close(BrowserSessionCommand(session_id=session_id))
            except Exception:
                pass
            raise
        await self._event(
            run.id,
            "browser.session_opened",
            {
                "browser_session_id": view.session.id,
                "node_id": view.session.node_id,
                "mode": view.session.mode.value,
                "page_id": view.session.current_page_id,
            },
        )
        return view

    async def get(self, session_id: str) -> BrowserView:
        session = await self._require_session(session_id)
        pages = await self._repository.list_pages(session_id)
        observation = await self._repository.latest_observation(session_id)
        return BrowserView(session=session, pages=pages, observation=observation)

    async def list_for_run(self, run_id: str) -> Sequence[BrowserSession]:
        await self._require_run(run_id)
        return await self._repository.list_sessions_for_run(run_id)

    async def observe(
        self,
        session_id: str,
        *,
        page_id: str | None = None,
        include_screenshot: bool = False,
        include_network: bool = True,
    ) -> BrowserView:
        async with self._session_lock(session_id):
            session = await self._require_active_session(session_id)
            exchange = await self._runner.observe(
                BrowserObserveCommand(
                    session_id=session.id,
                    page_id=page_id,
                    include_screenshot=include_screenshot,
                    include_network=include_network,
                )
            )
            view = await self._persist_exchange(exchange)
        await self._event(
            session.run_id,
            "browser.observed",
            {
                "browser_session_id": session.id,
                "page_id": view.observation.page_id if view.observation else None,
                "observation_version": (
                    view.observation.observation_version if view.observation else None
                ),
            },
        )
        return view

    async def act(self, session_id: str, command: ActBrowser) -> BrowserView:
        lock_key = f"{session_id}:{command.action_key}"
        async with self._keyed_lock(lock_key):
            session = await self._require_active_session(session_id)
            if session.owner is not BrowserOwner.AGENT:
                raise ApplicationConflictError(
                    "browser_agent_write_blocked",
                    f"Browser is owned by {session.owner.value}; Agent writes are disabled",
                )
            existing = await self._repository.get_action(session_id, command.action_key)
            candidate = BrowserAction(
                action_key=command.action_key,
                browser_session_id=session_id,
                page_id=command.page_id,
                observation_version=command.observation_version,
                action=command.action,
                element_ref=command.element_ref,
                value=command.value,
                url=command.url,
                options=command.options or {},
            )
            if existing is not None:
                if _action_fingerprint(existing) != _action_fingerprint(candidate):
                    raise ApplicationConflictError(
                        "browser_action_idempotency_conflict",
                        "Browser action key was reused with different arguments",
                    )
                if existing.status is BrowserActionStatus.COMPLETED:
                    observation = (
                        await self._repository.get_observation(existing.result_observation_id)
                        if existing.result_observation_id
                        else None
                    )
                    return BrowserView(
                        session=session,
                        pages=await self._repository.list_pages(session_id),
                        observation=observation,
                        action=existing,
                    )
                candidate = existing
            latest = await self._repository.latest_observation(session_id, command.page_id)
            if latest is None or latest.observation_version != command.observation_version:
                raise ApplicationConflictError(
                    "browser_observation_stale",
                    "Browser action must use the latest observation version",
                    details={
                        "requested_version": command.observation_version,
                        "latest_version": latest.observation_version if latest else 0,
                    },
                )
            if existing is None:
                await self._repository.create_action(candidate)
            candidate = candidate.model_copy(
                update={"status": BrowserActionStatus.RUNNING, "error": ""}
            )
            await self._repository.save_action(candidate)
            await self._event(
                session.run_id,
                "browser.action_started",
                {
                    "browser_session_id": session.id,
                    "action_id": candidate.id,
                    "action": candidate.action.value,
                    "page_id": candidate.page_id,
                    "observation_version": candidate.observation_version,
                },
            )
            try:
                exchange = await self._runner.act(
                    BrowserActCommand(
                        session_id=session_id,
                        action=candidate,
                        include_screenshot=command.include_screenshot,
                    )
                )
                view = await self._persist_exchange(exchange, existing_action=candidate)
            except Exception as exc:
                failed = candidate.model_copy(
                    update={
                        "status": BrowserActionStatus.FAILED,
                        "error": str(exc)[:8192],
                        "completed_at": utc_now(),
                    }
                )
                await self._repository.save_action(failed)
                await self._event(
                    session.run_id,
                    "browser.action_failed",
                    {
                        "browser_session_id": session.id,
                        "action_id": candidate.id,
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            await self._event(
                session.run_id,
                "browser.action_completed",
                {
                    "browser_session_id": session.id,
                    "action_id": view.action.id if view.action else candidate.id,
                    "observation_version": (
                        view.observation.observation_version if view.observation else None
                    ),
                    "download_artifact_id": (
                        view.action.download_artifact_id if view.action else None
                    ),
                },
            )
            return view

    async def takeover(self, session_id: str) -> BrowserView:
        async with self._session_lock(session_id):
            session = await self._require_active_session(session_id)
            exchange = await self._runner.takeover(
                BrowserSessionCommand(session_id=session.id)
            )
            view = await self._persist_exchange(exchange, persist_observation=False)
        await self._event(
            session.run_id,
            "browser.takeover_started",
            {
                "browser_session_id": session.id,
                "observation_version": view.session.takeover_observation_version,
            },
        )
        return view

    async def release(self, session_id: str) -> BrowserView:
        async with self._session_lock(session_id):
            session = await self._require_active_session(session_id)
            if session.owner is not BrowserOwner.USER:
                raise ApplicationConflictError(
                    "browser_not_taken_over", "Browser is not under user takeover"
                )
            exchange = await self._runner.release(
                BrowserSessionCommand(session_id=session.id)
            )
            view = await self._persist_exchange(exchange)
            if view.takeover_summary is not None:
                await self._repository.create_takeover_summary(view.takeover_summary)
        await self._event(
            session.run_id,
            "browser.takeover_released",
            (
                view.takeover_summary.model_dump(mode="json")
                if view.takeover_summary is not None
                else {"browser_session_id": session.id}
            ),
        )
        return view

    async def close(self, session_id: str) -> BrowserView:
        async with self._session_lock(session_id):
            session = await self._require_session(session_id)
            if session.status in {BrowserSessionStatus.CLOSED, BrowserSessionStatus.LOST}:
                return await self.get(session_id)
            exchange = await self._runner.close(
                BrowserSessionCommand(session_id=session.id)
            )
            view = await self._persist_exchange(exchange, persist_observation=False)
        await self._event(
            session.run_id,
            "browser.session_closed",
            {"browser_session_id": session.id},
        )
        return view

    async def observations_after(
        self, session_id: str, version: int, *, limit: int = 100
    ) -> Sequence[BrowserObservation]:
        await self._require_session(session_id)
        return await self._repository.observations_after(
            session_id, version, limit=limit
        )

    async def _persist_exchange(
        self,
        exchange: BrowserRuntimeExchange,
        *,
        existing_action: BrowserAction | None = None,
        persist_observation: bool = True,
    ) -> BrowserView:
        result = exchange.result
        session = result.session
        pages = [
            page.model_copy(update={"browser_session_id": session.id})
            for page in result.pages
        ]
        observation = result.observation
        action = result.action
        takeover_summary = result.takeover_summary
        if (
            persist_observation
            and observation is not None
            and observation.recent_network_summary
        ):
            network_artifact = await self._artifacts.register_content(
                session.run_id,
                RegisterArtifactContent(
                    content=json.dumps(
                        [
                            item.model_dump(mode="json")
                            for item in observation.recent_network_summary
                        ],
                        ensure_ascii=False,
                        indent=2,
                    ).encode(),
                    name=(
                        f"browser-{session.id}-{observation.page_id}-"
                        f"v{observation.observation_version}-network.json"
                    ),
                    mime_type="application/json",
                    description="Managed browser network activity summary",
                ),
            )
            observation = observation.model_copy(
                update={"network_artifact_id": network_artifact.id}
            )
        if result.attachment is not None:
            artifact = await self._artifacts.register_content(
                session.run_id,
                RegisterArtifactContent(
                    content=exchange.attachment_content,
                    name=result.attachment.name,
                    mime_type=result.attachment.mime_type,
                    description=result.attachment.description,
                ),
            )
            if result.attachment.kind == "screenshot" and observation is not None:
                observation = observation.model_copy(
                    update={"screenshot_artifact_id": artifact.id}
                )
            elif result.attachment.kind == "download" and action is not None:
                action = action.model_copy(update={"download_artifact_id": artifact.id})
            elif result.attachment.kind == "download" and takeover_summary is not None:
                takeover_summary = takeover_summary.model_copy(
                    update={
                        "download_artifact_ids": [
                            *takeover_summary.download_artifact_ids,
                            artifact.id,
                        ]
                    }
                )
        await self._repository.save_session(session)
        await self._repository.save_pages(pages)
        if observation is not None and persist_observation:
            observation = await self._repository.create_observation(observation)
        if action is not None:
            if existing_action is None:
                persisted = await self._repository.get_action(
                    action.browser_session_id, action.action_key
                )
                if persisted is None:
                    await self._repository.create_action(action)
                else:
                    action = action.model_copy(update={"id": persisted.id})
                    await self._repository.save_action(action)
            else:
                action = action.model_copy(update={"id": existing_action.id})
                await self._repository.save_action(action)
        return BrowserView(
            session=session,
            pages=pages,
            observation=observation,
            action=action,
            takeover_summary=takeover_summary,
        )

    async def _require_run(self, run_id: str) -> Run:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run

    async def _require_session(self, session_id: str) -> BrowserSession:
        session = await self._repository.get_session(session_id)
        if session is None:
            raise EntityNotFoundError("BrowserSession", session_id)
        self._bind(session.id, session.node_id)
        return session

    async def _require_active_session(self, session_id: str) -> BrowserSession:
        session = await self._require_session(session_id)
        if session.status is not BrowserSessionStatus.ACTIVE:
            raise ApplicationConflictError(
                "browser_session_not_active",
                f"Browser session is {session.status.value}, not active",
            )
        return session

    def _bind(self, session_id: str, node_id: str) -> None:
        bind = getattr(self._runner, "bind_session", None)
        if bind is not None:
            bind(session_id, node_id)

    def _session_lock(self, session_id: str):
        return self._keyed_lock(f"session:{session_id}")

    def _keyed_lock(self, key: str):
        service = self

        class _LockContext:
            async def __aenter__(self) -> None:
                service._lock_users[key] = service._lock_users.get(key, 0) + 1
                lock = service._locks.setdefault(key, asyncio.Lock())
                await lock.acquire()

            async def __aexit__(self, exc_type, exc, tb) -> None:
                lock = service._locks[key]
                lock.release()
                service._lock_users[key] -= 1
                if service._lock_users[key] == 0:
                    service._lock_users.pop(key, None)
                    service._locks.pop(key, None)

        return _LockContext()

    async def _event(
        self, run_id: str, event_type: str, payload: dict[str, object]
    ) -> None:
        if self._events is not None:
            await self._events.append(run_id, event_type, payload)


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
