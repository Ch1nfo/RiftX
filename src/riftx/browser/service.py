"""Control-plane orchestration for durable managed browser sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    ServiceUnavailableError,
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
    RunStatus,
)
from riftx.domain.base import new_id, utc_now
from riftx.runner.browser import BrowserRunner
from riftx.scope import ScopeGuard, ScopeTargetKind

_BROWSER_EFFECT_BLOCKED_RUN_STATUSES = {
    RunStatus.PAUSING,
    RunStatus.PAUSED,
    RunStatus.CANCELLING,
    RunStatus.CANCELLED,
    RunStatus.COMPLETING,
    RunStatus.COMPLETED,
    RunStatus.FAILED,
}

_BROWSER_STOP_CANDIDATE_STATUSES = {
    BrowserSessionStatus.CREATED,
    BrowserSessionStatus.STARTING,
    BrowserSessionStatus.ACTIVE,
    BrowserSessionStatus.LOST,
}


class RunRepository(Protocol):
    async def get(self, run_id: str) -> Run | None: ...


class AgentSession(Protocol):
    id: str
    run_id: str


class AgentSessionRepository(Protocol):
    async def get(self, session_id: str) -> AgentSession | None: ...


class RunEventRepository(Protocol):
    async def append(self, run_id: str, event_type: str, payload: dict[str, object]) -> object: ...


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
    options: dict[str, JsonValue] | None = None
    include_screenshot: bool = True


@dataclass(frozen=True, slots=True)
class BrowserView:
    session: BrowserSession
    pages: Sequence[BrowserPage]
    observation: BrowserObservation | None = None
    action: BrowserAction | None = None
    takeover_summary: BrowserTakeoverSummary | None = None


@dataclass(frozen=True, slots=True)
class BrowserRunStopResult:
    """Per-session evidence from a bounded safety stop for one Run."""

    run_id: str
    attempted_ids: tuple[str, ...]
    node_ids: dict[str, str]
    initial_statuses: dict[str, str]
    observed_statuses: dict[str, str]
    confirmed_statuses: dict[str, str]
    failures: dict[str, str]

    @property
    def confirmed_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.confirmed_statuses))

    @property
    def succeeded(self) -> bool:
        return not self.failures


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
        stop_timeout_seconds: float = 5.0,
    ) -> None:
        if stop_timeout_seconds < 0:
            raise ValueError("stop_timeout_seconds must not be negative")
        self._runs = runs
        self._agent_sessions = agent_sessions
        self._repository = repository
        self._runner = runner
        self._artifacts = artifacts
        self._events = events
        self._stop_timeout_seconds = stop_timeout_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}
        self._opening_done: dict[str, asyncio.Event] = {}

    async def open(self, command: OpenBrowser) -> BrowserView:
        run = await self._require_effects_allowed(command.run_id)
        agent_session = await self._agent_sessions.get(command.agent_session_id)
        if agent_session is None or agent_session.run_id != run.id:
            raise EntityNotFoundError("AgentSession", command.agent_session_id)
        ScopeGuard(run.scope).require(command.url, kind=ScopeTargetKind.URL)
        session_id = new_id()
        self._bind(session_id, run.node_id)
        starting = BrowserSession(
            id=session_id,
            run_id=run.id,
            agent_session_id=command.agent_session_id,
            node_id=run.node_id,
            mode=command.mode,
            profile_id=command.profile_id,
            cdp_endpoint=command.cdp_endpoint,
        )
        starting.transition_to(BrowserSessionStatus.STARTING)
        opening_done = asyncio.Event()

        # Registration and stop enumeration share a short Run lock.  STARTING
        # is therefore durable before the potentially long Runner await, and a
        # stop that fenced the Run cannot miss an in-flight open.
        async with self._run_lock(run.id):
            run = await self._require_effects_allowed(run.id)
            await self._repository.create_session(starting)
            self._opening_done[session_id] = opening_done
        try:
            # Re-read the persistent fence after STARTING is committed.  This
            # closes the cross-service/process window where a Run stop could
            # finish between the in-lock check and the physical browser open.
            try:
                await self._require_effects_allowed(run.id)
            except BaseException:
                current = await self._repository.get_session(session_id)
                if current is not None and current.status is BrowserSessionStatus.STARTING:
                    current.transition_to(BrowserSessionStatus.CLOSED)
                    await self._repository.save_session(current)
                raise
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
            async with self._session_lock(session_id):
                await self._require_post_runner_effect_allowed(
                    session_id, run.id, allow_starting=True
                )
                view = await self._persist_exchange(exchange)
                await self._require_post_persist_effect_allowed(session_id, run.id)
        except BaseException:
            await self._cleanup_failed_open(session_id)
            raise
        finally:
            opening_done.set()
            self._opening_done.pop(session_id, None)
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

    async def stop_run(self, run_id: str) -> BrowserRunStopResult:
        """Close every possibly-live browser and return confirmation evidence.

        CLOSED is the only confirmed stop state.  LOST remains a failure unless
        a later Runner close resolves it to CLOSED.
        """

        async with self._run_lock(run_id):
            await self._require_run(run_id)
            candidates = [
                session
                for session in await self._repository.list_sessions_for_run(run_id)
                if session.status in _BROWSER_STOP_CANDIDATE_STATUSES
            ]
            opening_events = {
                session.id: self._opening_done.get(session.id) for session in candidates
            }

        outcomes = await asyncio.gather(
            *(
                self._stop_session_for_run(
                    session,
                    opening_done=opening_events[session.id],
                )
                for session in candidates
            )
        )
        attempted_ids = tuple(sorted(session.id for session in candidates))
        by_id = {session.id: (session, final, failure) for session, final, failure in outcomes}
        node_ids = {session_id: by_id[session_id][0].node_id for session_id in attempted_ids}
        initial_statuses = {
            session_id: by_id[session_id][0].status.value for session_id in attempted_ids
        }
        observed_statuses: dict[str, str] = {}
        for session_id in attempted_ids:
            final = by_id[session_id][1]
            observed_statuses[session_id] = final.status.value if final is not None else "missing"
        confirmed_statuses = {
            session_id: final.status.value
            for session_id in attempted_ids
            if (final := by_id[session_id][1]) is not None and final.stop_confirmed
        }
        failures = {
            session_id: failure
            for session_id in attempted_ids
            if (failure := by_id[session_id][2]) is not None
        }
        return BrowserRunStopResult(
            run_id=run_id,
            attempted_ids=attempted_ids,
            node_ids=node_ids,
            initial_statuses=initial_statuses,
            observed_statuses=observed_statuses,
            confirmed_statuses=confirmed_statuses,
            failures=failures,
        )

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
        async with self._session_lock(session_id):
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
            async with self._session_lock(session_id):
                session = await self._require_effect_session(session_id)
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
                async with self._session_lock(session_id):
                    await self._require_post_runner_effect_allowed(session_id, session.run_id)
                    view = await self._persist_exchange(exchange, existing_action=candidate)
                    await self._require_post_persist_effect_allowed(session_id, session.run_id)
            except Exception as exc:
                async with self._session_lock(session_id):
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
            session = await self._require_effect_session(session_id)
        exchange = await self._runner.takeover(BrowserSessionCommand(session_id=session.id))
        async with self._session_lock(session_id):
            await self._require_post_runner_effect_allowed(session.id, session.run_id)
            view = await self._persist_exchange(exchange, persist_observation=False)
            await self._require_post_persist_effect_allowed(session.id, session.run_id)
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
            session = await self._require_effect_session(session_id)
            if session.owner is not BrowserOwner.USER:
                raise ApplicationConflictError(
                    "browser_not_taken_over", "Browser is not under user takeover"
                )
        exchange = await self._runner.release(BrowserSessionCommand(session_id=session.id))
        async with self._session_lock(session_id):
            await self._require_post_runner_effect_allowed(session.id, session.run_id)
            view = await self._persist_exchange(exchange)
            await self._require_post_persist_effect_allowed(session.id, session.run_id)
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
            if session.status is BrowserSessionStatus.CLOSED:
                return await self.get(session_id)
            view = await self._close_locked(session.id)
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
        return await self._repository.observations_after(session_id, version, limit=limit)

    async def _persist_exchange(
        self,
        exchange: BrowserRuntimeExchange,
        *,
        existing_action: BrowserAction | None = None,
        persist_observation: bool = True,
    ) -> BrowserView:
        result = exchange.result
        session = result.session
        await self._require_runtime_snapshot_writable(session)
        pages = [
            page.model_copy(update={"browser_session_id": session.id}) for page in result.pages
        ]
        observation = result.observation
        action = result.action
        takeover_summary = result.takeover_summary
        if persist_observation and observation is not None and observation.recent_network_summary:
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
                observation = observation.model_copy(update={"screenshot_artifact_id": artifact.id})
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
        # Artifact writes above are awaited and may race an out-of-process
        # safety stop.  Re-read immediately before the mutable session write;
        # a terminal durable state always wins over a stale ACTIVE snapshot.
        await self._require_runtime_snapshot_writable(session)
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

    async def _stop_session_for_run(
        self,
        initial: BrowserSession,
        *,
        opening_done: asyncio.Event | None,
    ) -> tuple[BrowserSession, BrowserSession | None, str | None]:
        failure = await self._attempt_stop_close(initial.id)

        if opening_done is not None:
            try:
                await asyncio.wait_for(opening_done.wait(), timeout=self._stop_timeout_seconds)
            except TimeoutError:
                failure = (
                    "TimeoutError: browser open did not finish before the stop "
                    "confirmation deadline"
                )

        current = await self._repository.get_session(initial.id)
        if current is not None and not current.stop_confirmed and opening_done is not None:
            retry_failure = await self._attempt_stop_close(initial.id)
            if retry_failure is not None:
                failure = retry_failure
            current = await self._repository.get_session(initial.id)

        if current is None:
            return initial, None, "browser session disappeared before stop confirmation"
        if current.stop_confirmed:
            return initial, current, None
        return (
            initial,
            current,
            failure or (f"stop was not confirmed; browser session remains {current.status.value}"),
        )

    async def _attempt_stop_close(self, session_id: str) -> str | None:
        try:
            await asyncio.wait_for(self.close(session_id), timeout=self._stop_timeout_seconds)
            return None
        except TimeoutError:
            failure = "TimeoutError: Runner did not confirm browser close before the stop deadline"
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"

        # A timed-out close may have been cancelled before _close_locked could
        # record uncertainty.  LOST is deliberately unconfirmed and prevents a
        # late ACTIVE Runner snapshot from winning.
        try:
            async with self._session_lock(session_id):
                await self._mark_lost_locked(session_id)
        except Exception as exc:
            failure = f"{failure}; reconcile failed: {type(exc).__name__}: {exc}"
        return failure

    async def _cleanup_failed_open(self, session_id: str) -> None:
        async with self._session_lock(session_id):
            current = await self._repository.get_session(session_id)
            if current is None or current.stop_confirmed:
                return
            try:
                await self._close_locked(session_id, force_runner=True)
            except Exception:
                # _close_locked durably records LOST for a possibly-live
                # session.  Preserve the original open/effect error.
                return

    async def _close_locked(
        self,
        session_id: str,
        *,
        force_runner: bool = False,
    ) -> BrowserView:
        before = await self._require_session(session_id)
        if before.status is BrowserSessionStatus.CLOSED and not force_runner:
            return await self.get(session_id)
        try:
            exchange = await self._runner.close(
                BrowserSessionCommand(
                    session_id=session_id,
                    session=before.model_copy(deep=True),
                )
            )
        except Exception as exc:
            await self._mark_lost_locked(session_id)
            raise ServiceUnavailableError(
                "browser_close_unconfirmed",
                "Runner did not confirm that the browser session closed",
                details={
                    "browser_session_id": session_id,
                    "node_id": before.node_id,
                    "status": ((await self._require_session(session_id)).status.value),
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            ) from exc
        if exchange.result.session.status is not BrowserSessionStatus.CLOSED:
            await self._mark_lost_locked(session_id)
            current = await self._require_session(session_id)
            raise ServiceUnavailableError(
                "browser_close_unconfirmed",
                "Runner returned without confirming that the browser session closed",
                details={
                    "browser_session_id": session_id,
                    "node_id": current.node_id,
                    "status": current.status.value,
                    "runner_status": exchange.result.session.status.value,
                },
            )
        view = await self._persist_exchange(exchange, persist_observation=False)
        current = await self._require_session(session_id)
        if not current.stop_confirmed:
            await self._mark_lost_locked(session_id)
            raise ServiceUnavailableError(
                "browser_close_unconfirmed",
                "Browser close acknowledgement was not durably persisted",
                details={
                    "browser_session_id": session_id,
                    "node_id": current.node_id,
                    "status": current.status.value,
                },
            )
        return view

    async def _mark_lost_locked(self, session_id: str) -> BrowserSession:
        current = await self._require_session(session_id)
        if current.status in {
            BrowserSessionStatus.CLOSED,
            BrowserSessionStatus.LOST,
        }:
            return current
        current.transition_to(BrowserSessionStatus.LOST)
        return await self._repository.save_session(current)

    async def _require_runtime_snapshot_writable(self, snapshot: BrowserSession) -> BrowserSession:
        current = await self._repository.get_session(snapshot.id)
        if current is None:
            raise EntityNotFoundError("BrowserSession", snapshot.id)
        if current.run_id != snapshot.run_id or current.node_id != snapshot.node_id:
            raise ApplicationConflictError(
                "browser_session_identity_mismatch",
                "Runner returned browser state for a different Run or node",
            )
        if (
            current.status is BrowserSessionStatus.CLOSED
            and snapshot.status is not BrowserSessionStatus.CLOSED
        ) or (
            current.status is BrowserSessionStatus.LOST
            and snapshot.status not in {BrowserSessionStatus.LOST, BrowserSessionStatus.CLOSED}
        ):
            raise ApplicationConflictError(
                "browser_session_terminal_wins",
                "A stopped browser session cannot be reactivated by a stale Runner result",
                details={
                    "browser_session_id": current.id,
                    "durable_status": current.status.value,
                    "runner_status": snapshot.status.value,
                },
            )
        return current

    async def _require_run(self, run_id: str) -> Run:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run

    async def _blocked_run(self, run_id: str) -> Run | None:
        run = await self._require_run(run_id)
        return run if run.status in _BROWSER_EFFECT_BLOCKED_RUN_STATUSES else None

    async def _require_effects_allowed(self, run_id: str) -> Run:
        run = await self._require_run(run_id)
        if run.status in _BROWSER_EFFECT_BLOCKED_RUN_STATUSES:
            raise self._effect_blocked_error(run)
        return run

    async def _require_effect_session(self, session_id: str) -> BrowserSession:
        session = await self._require_active_session(session_id)
        await self._require_effects_allowed(session.run_id)
        return session

    async def _require_post_runner_effect_allowed(
        self,
        session_id: str,
        run_id: str,
        *,
        allow_starting: bool = False,
    ) -> None:
        current = await self._require_session(session_id)
        blocked = await self._blocked_run(run_id)
        allowed_statuses = {BrowserSessionStatus.ACTIVE}
        if allow_starting:
            allowed_statuses.add(BrowserSessionStatus.STARTING)
        if blocked is None and current.status in allowed_statuses:
            return

        cleanup_failure: str | None = None
        try:
            await self._close_locked(session_id, force_runner=True)
        except Exception as exc:
            cleanup_failure = f"{type(exc).__name__}: {exc}"
        if blocked is not None:
            error = self._effect_blocked_error(blocked)
            if cleanup_failure is not None:
                error.details["browser_cleanup_failure"] = cleanup_failure
            raise error
        raise ApplicationConflictError(
            "browser_session_terminal_wins",
            "Browser operation finished after the session had already stopped",
            details={
                "browser_session_id": session_id,
                "status": current.status.value,
                **(
                    {"browser_cleanup_failure": cleanup_failure}
                    if cleanup_failure is not None
                    else {}
                ),
            },
        )

    async def _require_post_persist_effect_allowed(self, session_id: str, run_id: str) -> None:
        blocked = await self._blocked_run(run_id)
        if blocked is None:
            return
        cleanup_failure: str | None = None
        try:
            await self._close_locked(session_id)
        except Exception as exc:
            cleanup_failure = f"{type(exc).__name__}: {exc}"
        error = self._effect_blocked_error(blocked)
        if cleanup_failure is not None:
            error.details["browser_cleanup_failure"] = cleanup_failure
        raise error

    @staticmethod
    def _effect_blocked_error(run: Run) -> ApplicationConflictError:
        return ApplicationConflictError(
            "run_execution_blocked",
            f"Run {run.id!r} cannot perform browser effects while it is {run.status.value}",
            details={"run_id": run.id, "status": run.status.value},
        )

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

    def _run_lock(self, run_id: str):
        return self._keyed_lock(f"run:{run_id}")

    def _keyed_lock(self, key: str):
        service = self

        class _LockContext:
            async def __aenter__(self) -> None:
                service._lock_users[key] = service._lock_users.get(key, 0) + 1
                lock = service._locks.setdefault(key, asyncio.Lock())
                try:
                    await lock.acquire()
                except BaseException:
                    service._lock_users[key] -= 1
                    if service._lock_users[key] == 0:
                        service._lock_users.pop(key, None)
                        service._locks.pop(key, None)
                    raise

            async def __aexit__(self, exc_type, exc, tb) -> None:
                lock = service._locks[key]
                lock.release()
                service._lock_users[key] -= 1
                if service._lock_users[key] == 0:
                    service._lock_users.pop(key, None)
                    service._locks.pop(key, None)

        return _LockContext()

    async def _event(self, run_id: str, event_type: str, payload: dict[str, object]) -> None:
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
