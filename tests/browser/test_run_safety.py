from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.browser.models import (
    BrowserActCommand,
    BrowserObserveCommand,
    BrowserOpenCommand,
    BrowserRuntimeExchange,
    BrowserRuntimeResult,
    BrowserSessionCommand,
)
from riftx.browser.service import ActBrowser, BrowserApplicationService, OpenBrowser
from riftx.domain import (
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
    Objective,
    Run,
    RunStatus,
    Scope,
)


class MutableRunRepository:
    def __init__(self, run: Run) -> None:
        self.run = run

    async def get(self, run_id: str) -> Run | None:
        if self.run.id != run_id:
            return None
        return self.run.model_copy(deep=True)

    def set_status(self, status: RunStatus) -> None:
        self.run = self.run.model_copy(update={"status": status})


class AgentSessionRepository:
    async def get(self, session_id: str):
        if session_id != "agent-session-1":
            return None
        return SimpleNamespace(id=session_id, run_id="run-1")


class InMemoryBrowserRepository:
    def __init__(self) -> None:
        self.sessions: dict[str, BrowserSession] = {}
        self.pages: dict[str, BrowserPage] = {}
        self.observations: list[BrowserObservation] = []
        self.actions: dict[tuple[str, str], BrowserAction] = {}
        self.takeover_summaries: list[BrowserTakeoverSummary] = []

    async def create_session(self, item: BrowserSession) -> BrowserSession:
        if item.id in self.sessions:
            raise RuntimeError("duplicate browser session")
        self.sessions[item.id] = item.model_copy(deep=True)
        return item

    async def get_session(self, session_id: str) -> BrowserSession | None:
        item = self.sessions.get(session_id)
        return item.model_copy(deep=True) if item is not None else None

    async def save_session(self, item: BrowserSession) -> BrowserSession:
        if item.id not in self.sessions:
            raise RuntimeError("missing browser session")
        self.sessions[item.id] = item.model_copy(deep=True)
        return item

    async def list_sessions_for_run(self, run_id: str) -> Sequence[BrowserSession]:
        return [
            item.model_copy(deep=True) for item in self.sessions.values() if item.run_id == run_id
        ]

    async def save_pages(self, pages: Sequence[BrowserPage]) -> list[BrowserPage]:
        for page in pages:
            self.pages[page.id] = page.model_copy(deep=True)
        return list(pages)

    async def list_pages(self, session_id: str) -> Sequence[BrowserPage]:
        return [
            item.model_copy(deep=True)
            for item in self.pages.values()
            if item.browser_session_id == session_id
        ]

    async def create_observation(self, item: BrowserObservation) -> BrowserObservation:
        self.observations.append(item.model_copy(deep=True))
        return item

    async def get_observation(self, observation_id: str) -> BrowserObservation | None:
        return next(
            (item.model_copy(deep=True) for item in self.observations if item.id == observation_id),
            None,
        )

    async def latest_observation(
        self, session_id: str, page_id: str | None = None
    ) -> BrowserObservation | None:
        matching = [
            item
            for item in self.observations
            if item.browser_session_id == session_id
            and (page_id is None or item.page_id == page_id)
        ]
        return matching[-1].model_copy(deep=True) if matching else None

    async def observations_after(
        self, session_id: str, version: int, *, limit: int = 100
    ) -> Sequence[BrowserObservation]:
        return [
            item.model_copy(deep=True)
            for item in self.observations
            if item.browser_session_id == session_id and item.observation_version > version
        ][:limit]

    async def create_action(self, item: BrowserAction) -> BrowserAction:
        self.actions[(item.browser_session_id, item.action_key)] = item.model_copy(deep=True)
        return item

    async def get_action(self, session_id: str, action_key: str) -> BrowserAction | None:
        item = self.actions.get((session_id, action_key))
        return item.model_copy(deep=True) if item is not None else None

    async def save_action(self, item: BrowserAction) -> BrowserAction:
        self.actions[(item.browser_session_id, item.action_key)] = item.model_copy(deep=True)
        return item

    async def create_takeover_summary(self, item: BrowserTakeoverSummary) -> BrowserTakeoverSummary:
        self.takeover_summaries.append(item.model_copy(deep=True))
        return item


class FenceAfterCreateBrowserRepository(InMemoryBrowserRepository):
    def __init__(self, runs: MutableRunRepository) -> None:
        super().__init__()
        self._runs = runs

    async def create_session(self, item: BrowserSession) -> BrowserSession:
        created = await super().create_session(item)
        self._runs.set_status(RunStatus.PAUSING)
        return created


class NoArtifactWrites:
    async def register_content(self, *_args, **_kwargs):
        raise AssertionError("safety tests do not produce browser artifacts")


class ControlledBrowserRunner:
    def __init__(self, *, block_open: bool = False, block_act: bool = False) -> None:
        self.sessions: dict[str, BrowserSession] = {}
        self.open_entered = asyncio.Event()
        self.open_release = asyncio.Event()
        self.act_entered = asyncio.Event()
        self.act_release = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.calls = {
            "open": 0,
            "observe": 0,
            "act": 0,
            "takeover": 0,
            "release": 0,
            "close": 0,
        }
        if not block_open:
            self.open_release.set()
        if not block_act:
            self.act_release.set()

    def seed(self, session: BrowserSession) -> None:
        self.sessions[session.id] = session.model_copy(deep=True)

    async def open(self, command: BrowserOpenCommand) -> BrowserRuntimeExchange:
        self.calls["open"] += 1
        self.open_entered.set()
        await self.open_release.wait()
        session = BrowserSession(
            id=command.session_id,
            run_id=command.run_id,
            agent_session_id=command.agent_session_id,
            node_id=command.node_id,
            mode=command.mode,
            profile_id=command.profile_id,
            cdp_endpoint=command.cdp_endpoint,
            status=BrowserSessionStatus.STARTING,
        )
        session.transition_to(BrowserSessionStatus.ACTIVE)
        session.register_page("page-1")
        self.sessions[session.id] = session.model_copy(deep=True)
        return _exchange(session, version=1)

    async def observe(self, command: BrowserObserveCommand) -> BrowserRuntimeExchange:
        self.calls["observe"] += 1
        session = self.sessions[command.session_id]
        return _exchange(session, version=2)

    async def act(self, command: BrowserActCommand) -> BrowserRuntimeExchange:
        self.calls["act"] += 1
        self.act_entered.set()
        await self.act_release.wait()
        session = self.sessions[command.session_id]
        completed = command.action.model_copy(
            update={
                "status": BrowserActionStatus.COMPLETED,
                "result_observation_id": "observation-2",
            }
        )
        return _exchange(session, version=2, action=completed)

    async def takeover(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        self.calls["takeover"] += 1
        session = self.sessions[command.session_id]
        session.take_over(observation_version=1)
        return _exchange(session, version=1)

    async def release(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        self.calls["release"] += 1
        session = self.sessions[command.session_id]
        session.release()
        return _exchange(session, version=2)

    async def close(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        self.calls["close"] += 1
        self.close_entered.set()
        session = self.sessions.get(command.session_id)
        if session is None:
            raise KeyError("browser is not active on the Runner")
        if session.status is not BrowserSessionStatus.CLOSED:
            session.transition_to(BrowserSessionStatus.CLOSED)
        return _exchange(session, closed=True)


class HangingCloseRunner(ControlledBrowserRunner):
    async def close(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        self.calls["close"] += 1
        self.close_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class BlockingOwnershipRunner(ControlledBrowserRunner):
    def __init__(self) -> None:
        super().__init__()
        self.effect_entered = asyncio.Event()
        self.effect_release = asyncio.Event()

    async def takeover(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        self.calls["takeover"] += 1
        self.effect_entered.set()
        await self.effect_release.wait()
        session = self.sessions[command.session_id]
        session.take_over(observation_version=1)
        return _exchange(session, version=1)

    async def release(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        self.calls["release"] += 1
        self.effect_entered.set()
        await self.effect_release.wait()
        session = self.sessions[command.session_id]
        session.release()
        return _exchange(session, version=2)


def _exchange(
    session: BrowserSession,
    *,
    version: int = 1,
    action: BrowserAction | None = None,
    closed: bool = False,
) -> BrowserRuntimeExchange:
    page = BrowserPage(
        id="page-1",
        browser_session_id=session.id,
        url="https://example.com/",
        title="Example",
        status=BrowserPageStatus.CLOSED if closed else BrowserPageStatus.OPEN,
        last_observation_version=version,
    )
    return BrowserRuntimeExchange(
        result=BrowserRuntimeResult(
            session=session.model_copy(deep=True),
            pages=[page],
            observation=(
                None
                if closed
                else BrowserObservation(
                    id=f"observation-{version}",
                    browser_session_id=session.id,
                    page_id=page.id,
                    url=page.url,
                    title=page.title,
                    visible_text_excerpt="bounded",
                    observation_version=version,
                )
            ),
            action=action,
        )
    )


def _run(status: RunStatus = RunStatus.RUNNING) -> Run:
    return Run(
        kind="general",
        id="run-1",
        engagement_id="engagement-1",
        node_id="local",
        objective=Objective(description="Browser safety"),
        scope=Scope(domains=["example.com"]),
        status=status,
        workspace_path="/tmp/riftx-browser-safety",
    )


def _session(
    *,
    status: BrowserSessionStatus = BrowserSessionStatus.ACTIVE,
    owner: BrowserOwner = BrowserOwner.AGENT,
) -> BrowserSession:
    return BrowserSession(
        id="browser-1",
        run_id="run-1",
        agent_session_id="agent-session-1",
        node_id="local",
        mode=BrowserMode.MANAGED_EPHEMERAL,
        status=status,
        owner=owner,
        current_page_id="page-1",
        page_ids=["page-1"],
    )


async def _service(
    *,
    run_status: RunStatus = RunStatus.RUNNING,
    session: BrowserSession | None = None,
    runner: ControlledBrowserRunner | None = None,
    stop_timeout_seconds: float = 1,
) -> tuple[
    BrowserApplicationService,
    MutableRunRepository,
    InMemoryBrowserRepository,
    ControlledBrowserRunner,
]:
    runs = MutableRunRepository(_run(run_status))
    repository = InMemoryBrowserRepository()
    controlled = runner or ControlledBrowserRunner()
    if session is not None:
        await repository.create_session(session)
        await repository.save_pages(
            [
                BrowserPage(
                    id="page-1",
                    browser_session_id=session.id,
                    url="https://example.com/",
                    title="Example",
                    last_observation_version=1,
                )
            ]
        )
        await repository.create_observation(
            BrowserObservation(
                id="observation-1",
                browser_session_id=session.id,
                page_id="page-1",
                url="https://example.com/",
                title="Example",
                visible_text_excerpt="bounded",
                observation_version=1,
            )
        )
        if session.status is not BrowserSessionStatus.LOST:
            controlled.seed(session)
    service = BrowserApplicationService(
        runs=runs,
        agent_sessions=AgentSessionRepository(),
        repository=repository,
        runner=controlled,
        artifacts=NoArtifactWrites(),  # type: ignore[arg-type]
        stop_timeout_seconds=stop_timeout_seconds,
    )
    return service, runs, repository, controlled


@pytest.mark.parametrize(
    "run_status",
    [
        RunStatus.PAUSING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.CANCELLED,
        RunStatus.COMPLETING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    ],
)
@pytest.mark.parametrize("operation", ["open", "act", "takeover", "release"])
async def test_effectful_operations_are_rejected_by_stopped_run_status(
    run_status: RunStatus,
    operation: str,
) -> None:
    owner = BrowserOwner.USER if operation == "release" else BrowserOwner.AGENT
    service, _, _, runner = await _service(
        run_status=run_status,
        session=_session(owner=owner),
    )

    with pytest.raises(ApplicationConflictError) as captured:
        if operation == "open":
            await service.open(
                OpenBrowser(
                    run_id="run-1",
                    agent_session_id="agent-session-1",
                    url="https://example.com/",
                )
            )
        elif operation == "act":
            await service.act(
                "browser-1",
                ActBrowser(
                    page_id="page-1",
                    observation_version=1,
                    action=BrowserActionType.CLICK,
                    action_key="blocked-action",
                    element_ref="e-1",
                ),
            )
        elif operation == "takeover":
            await service.takeover("browser-1")
        else:
            await service.release("browser-1")

    assert captured.value.code == "run_execution_blocked"
    assert captured.value.details == {"run_id": "run-1", "status": run_status.value}
    assert runner.calls[operation] == 0


async def test_open_persists_starting_before_waiting_for_runner() -> None:
    runner = ControlledBrowserRunner(block_open=True)
    service, _, repository, _ = await _service(runner=runner)
    task = asyncio.create_task(
        service.open(
            OpenBrowser(
                run_id="run-1",
                agent_session_id="agent-session-1",
                url="https://example.com/",
            )
        )
    )
    await runner.open_entered.wait()
    sessions = list(await repository.list_sessions_for_run("run-1"))
    assert len(sessions) == 1
    assert sessions[0].status is BrowserSessionStatus.STARTING

    runner.open_release.set()
    opened = await task
    assert opened.session.status is BrowserSessionStatus.ACTIVE


async def test_run_fence_committed_after_browser_registration_blocks_physical_open() -> None:
    runs = MutableRunRepository(_run())
    repository = FenceAfterCreateBrowserRepository(runs)
    runner = ControlledBrowserRunner()
    service = BrowserApplicationService(
        runs=runs,
        agent_sessions=AgentSessionRepository(),
        repository=repository,
        runner=runner,
        artifacts=NoArtifactWrites(),  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.open(
            OpenBrowser(
                run_id="run-1",
                agent_session_id="agent-session-1",
                url="https://example.com/",
            )
        )

    assert captured.value.code == "run_execution_blocked"
    assert runner.calls["open"] == 0
    sessions = list(await repository.list_sessions_for_run("run-1"))
    assert len(sessions) == 1
    assert sessions[0].status is BrowserSessionStatus.CLOSED


async def test_stop_run_waits_for_inflight_open_and_confirms_closed() -> None:
    runner = ControlledBrowserRunner(block_open=True)
    service, runs, repository, _ = await _service(runner=runner)
    open_task = asyncio.create_task(
        service.open(
            OpenBrowser(
                run_id="run-1",
                agent_session_id="agent-session-1",
                url="https://example.com/",
            )
        )
    )
    await runner.open_entered.wait()
    runs.set_status(RunStatus.PAUSING)
    stop_task = asyncio.create_task(service.stop_run("run-1"))
    await runner.close_entered.wait()
    runner.open_release.set()

    with pytest.raises(ApplicationConflictError) as captured:
        await open_task
    result = await stop_task

    assert captured.value.code == "run_execution_blocked"
    assert result.succeeded is True
    assert result.confirmed_ids == result.attempted_ids
    assert set(result.initial_statuses.values()) == {"starting"}
    assert set(result.confirmed_statuses.values()) == {"closed"}
    durable = next(iter(repository.sessions.values()))
    assert durable.status is BrowserSessionStatus.CLOSED


async def test_run_stop_during_act_closes_session_instead_of_persisting_active() -> None:
    runner = ControlledBrowserRunner(block_act=True)
    service, runs, repository, _ = await _service(session=_session(), runner=runner)
    task = asyncio.create_task(
        service.act(
            "browser-1",
            ActBrowser(
                page_id="page-1",
                observation_version=1,
                action=BrowserActionType.CLICK,
                action_key="racing-action",
                element_ref="e-1",
            ),
        )
    )
    await runner.act_entered.wait()
    runs.set_status(RunStatus.PAUSING)
    runner.act_release.set()

    with pytest.raises(ApplicationConflictError) as captured:
        await task

    assert captured.value.code == "run_execution_blocked"
    assert repository.sessions["browser-1"].status is BrowserSessionStatus.CLOSED
    assert repository.actions[("browser-1", "racing-action")].status is BrowserActionStatus.FAILED


@pytest.mark.parametrize("operation", ["takeover", "release"])
async def test_run_stop_during_ownership_effect_closes_late_result(operation: str) -> None:
    runner = BlockingOwnershipRunner()
    owner = BrowserOwner.USER if operation == "release" else BrowserOwner.AGENT
    service, runs, repository, _ = await _service(session=_session(owner=owner), runner=runner)
    task = asyncio.create_task(
        service.takeover("browser-1") if operation == "takeover" else service.release("browser-1")
    )
    await runner.effect_entered.wait()
    runs.set_status(RunStatus.PAUSING)
    runner.effect_release.set()

    with pytest.raises(ApplicationConflictError) as captured:
        await task

    assert captured.value.code == "run_execution_blocked"
    assert repository.sessions["browser-1"].status is BrowserSessionStatus.CLOSED


async def test_stop_run_can_close_while_browser_action_is_still_waiting() -> None:
    runner = ControlledBrowserRunner(block_act=True)
    service, runs, repository, _ = await _service(session=_session(), runner=runner)
    action_task = asyncio.create_task(
        service.act(
            "browser-1",
            ActBrowser(
                page_id="page-1",
                observation_version=1,
                action=BrowserActionType.CLICK,
                action_key="preempted-action",
                element_ref="e-1",
            ),
        )
    )
    await runner.act_entered.wait()
    runs.set_status(RunStatus.PAUSING)

    result = await asyncio.wait_for(service.stop_run("run-1"), timeout=0.5)
    assert result.succeeded is True
    assert result.confirmed_statuses == {"browser-1": "closed"}
    assert repository.sessions["browser-1"].status is BrowserSessionStatus.CLOSED

    runner.act_release.set()
    with pytest.raises(ApplicationConflictError) as captured:
        await action_task
    assert captured.value.code == "run_execution_blocked"
    assert repository.sessions["browser-1"].status is BrowserSessionStatus.CLOSED


async def test_durable_closed_wins_over_late_active_runner_result() -> None:
    runner = ControlledBrowserRunner(block_act=True)
    service, _, repository, _ = await _service(session=_session(), runner=runner)
    task = asyncio.create_task(
        service.act(
            "browser-1",
            ActBrowser(
                page_id="page-1",
                observation_version=1,
                action=BrowserActionType.CLICK,
                action_key="late-action",
                element_ref="e-1",
            ),
        )
    )
    await runner.act_entered.wait()
    closed = repository.sessions["browser-1"].model_copy(deep=True)
    closed.transition_to(BrowserSessionStatus.CLOSED)
    repository.sessions[closed.id] = closed
    runner.act_release.set()

    with pytest.raises(ApplicationConflictError) as captured:
        await task

    assert captured.value.code == "browser_session_terminal_wins"
    assert repository.sessions["browser-1"].status is BrowserSessionStatus.CLOSED


async def test_lost_session_is_not_reported_as_confirmed_stop() -> None:
    service, _, _, _ = await _service(
        run_status=RunStatus.CANCELLING,
        session=_session(status=BrowserSessionStatus.LOST),
    )

    result = await service.stop_run("run-1")

    assert result.succeeded is False
    assert result.attempted_ids == ("browser-1",)
    assert result.node_ids == {"browser-1": "local"}
    assert result.observed_statuses == {"browser-1": "lost"}
    assert result.confirmed_statuses == {}
    assert "browser-1" in result.failures


async def test_stop_run_times_out_fail_closed_when_runner_never_acknowledges() -> None:
    runner = HangingCloseRunner()
    service, _, repository, _ = await _service(
        run_status=RunStatus.CANCELLING,
        session=_session(),
        runner=runner,
        stop_timeout_seconds=0.01,
    )

    result = await asyncio.wait_for(service.stop_run("run-1"), timeout=0.5)

    assert result.succeeded is False
    assert result.confirmed_statuses == {}
    assert result.observed_statuses == {"browser-1": "lost"}
    assert "stop deadline" in result.failures["browser-1"]
    assert repository.sessions["browser-1"].status is BrowserSessionStatus.LOST


async def test_read_only_observation_and_close_remain_available_when_paused() -> None:
    service, _, _, runner = await _service(
        run_status=RunStatus.PAUSED,
        session=_session(),
    )

    assert (await service.get("browser-1")).session.status is BrowserSessionStatus.ACTIVE
    observed = await service.observe("browser-1")
    assert observed.observation is not None
    assert observed.observation.observation_version == 2
    closed = await service.close("browser-1")
    assert closed.session.status is BrowserSessionStatus.CLOSED
    assert runner.calls["observe"] == 1
    assert runner.calls["close"] == 1
