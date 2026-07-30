from __future__ import annotations

from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import ArtifactApplicationService
from riftx.browser.service import ActBrowser, BrowserApplicationService, OpenBrowser
from riftx.domain import (
    BrowserAction,
    BrowserActionStatus,
    BrowserActionType,
    BrowserObservation,
    BrowserOwner,
    BrowserPage,
    Engagement,
    InteractiveElement,
    NetworkEventSummary,
    Objective,
    Run,
    Scope,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyBrowserRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import RunnerBrowserManager, RunnerPaths
from riftx.runtime.types import AgentSession


class ServiceEngineSession:
    profile_path = None

    def __init__(self, session_id: str, url: str) -> None:
        self.session_id = session_id
        self.url = url
        self.owner_downloads = []

    async def pages(self):
        return [
            BrowserPage(
                id="page-1",
                browser_session_id=self.session_id,
                url=self.url,
                title="Example",
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
    ):
        page = (await self.pages())[0]
        page.last_observation_version = version
        return (
            page,
            BrowserObservation(
                browser_session_id=browser_session_id,
                page_id=page_id,
                url=self.url,
                title="Example",
                visible_text_excerpt="bounded",
                interactive_elements=[
                    InteractiveElement(
                        ref="e-1", role="button", text="Continue"
                    )
                ],
                recent_network_summary=(
                    [
                        NetworkEventSummary(
                            sequence=version,
                            method="GET",
                            url=self.url,
                            status_code=200,
                        )
                    ]
                    if include_network
                    else []
                ),
                observation_version=version,
            ),
            b"png" if include_screenshot else b"",
        )

    async def act(self, action: BrowserAction):
        if action.action is BrowserActionType.CLICK:
            self.url = "https://example.com/next"
        return None, b""

    async def storage_digest(self):
        return "storage"

    async def download_count(self):
        return len(self.owner_downloads)

    async def downloads_since(self, index: int):
        return self.owner_downloads[index:]

    async def close(self):
        return None


class ServiceEngine:
    def __init__(self) -> None:
        self.sessions = {}

    async def open(self, command):
        session = ServiceEngineSession(command.session_id, command.url)
        self.sessions[command.session_id] = session
        return session


async def test_browser_service_persists_artifacts_actions_and_takeover_ownership(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'service.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    agent_sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    browser_repository = SQLAlchemyBrowserRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Browser"))
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Browse target"),
            scope=Scope(domains=["example.com"]),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    await agent_sessions.create(
        AgentSession(id="agent-session-1", run_id="run-1", model_profile="default")
    )
    paths = RunnerPaths(tmp_path / "runner")
    artifacts = ArtifactApplicationService(
        run_repository=runs,
        execution_repository=SQLAlchemyExecutionRepository(database.session_factory),
        artifact_repository=SQLAlchemyArtifactRepository(database.session_factory),
        event_repository=events,
        paths=paths,
    )
    service = BrowserApplicationService(
        runs=runs,
        agent_sessions=agent_sessions,
        repository=browser_repository,
        runner=RunnerBrowserManager(
            node_id="local", paths=paths, engine=ServiceEngine()
        ),
        artifacts=artifacts,
        events=events,
    )

    opened = await service.open(
        OpenBrowser(
            run_id="run-1",
            agent_session_id="agent-session-1",
            url="https://example.com/",
        )
    )
    assert opened.observation is not None
    assert opened.observation.screenshot_artifact_id is not None
    assert opened.observation.network_artifact_id is not None

    acted = await service.act(
        opened.session.id,
        ActBrowser(
            page_id="page-1",
            observation_version=1,
            action=BrowserActionType.CLICK,
            action_key="click-1",
            element_ref="e-1",
        ),
    )
    assert acted.action is not None
    assert acted.action.status is BrowserActionStatus.COMPLETED
    assert acted.observation is not None
    assert acted.observation.observation_version == 2

    taken = await service.takeover(opened.session.id)
    assert taken.session.owner is BrowserOwner.USER
    with pytest.raises(ApplicationConflictError, match="Agent writes are disabled"):
        await service.act(
            opened.session.id,
            ActBrowser(
                page_id="page-1",
                observation_version=2,
                action=BrowserActionType.CLICK,
                action_key="blocked",
                element_ref="e-1",
            ),
        )
    released = await service.release(opened.session.id)
    assert released.session.owner is BrowserOwner.AGENT
    assert released.takeover_summary is not None

    timeline = await events.list_after("run-1")
    assert "browser.session_opened" in [item.event_type for item in timeline]
    assert "browser.action_completed" in [item.event_type for item in timeline]
    assert "browser.takeover_released" in [item.event_type for item in timeline]
    await database.dispose()
