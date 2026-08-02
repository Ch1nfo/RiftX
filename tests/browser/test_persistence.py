from pathlib import Path

from riftx.domain import (
    BrowserAction,
    BrowserActionStatus,
    BrowserActionType,
    BrowserMode,
    BrowserObservation,
    BrowserPage,
    BrowserSession,
    BrowserSessionStatus,
    Engagement,
    Objective,
    Run,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyBrowserRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
from riftx.runtime.types import AgentSession


async def test_browser_repository_round_trip_preserves_versions_and_idempotency(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'browser.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    agent_sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    browsers = SQLAlchemyBrowserRepository(database.session_factory)

    await engagements.create(Engagement(id="engagement-1", name="Browser test"))
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Test browser persistence"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    await agent_sessions.create(
        AgentSession(id="agent-session-1", run_id="run-1", model_profile="default")
    )

    browser = BrowserSession(
        id="browser-1",
        run_id="run-1",
        agent_session_id="agent-session-1",
        node_id="local",
        mode=BrowserMode.MANAGED_EPHEMERAL,
        status=BrowserSessionStatus.ACTIVE,
        current_page_id="page-1",
        page_ids=["page-1"],
    )
    page = BrowserPage(
        id="page-1",
        browser_session_id="browser-1",
        url="https://example.com/",
        title="Example",
    )
    await browsers.create_session(browser)
    await browsers.save_pages([page])
    observation = BrowserObservation(
        id="observation-1",
        browser_session_id="browser-1",
        page_id="page-1",
        url="https://example.com/",
        title="Example",
        visible_text_excerpt="bounded",
        observation_version=1,
    )
    await browsers.create_observation(observation)
    action = BrowserAction(
        id="action-1",
        action_key="click-1",
        browser_session_id="browser-1",
        page_id="page-1",
        observation_version=1,
        action=BrowserActionType.CLICK,
        element_ref="e-1",
    )
    await browsers.create_action(action)
    await browsers.save_action(
        action.model_copy(
            update={
                "status": BrowserActionStatus.COMPLETED,
                "result_observation_id": observation.id,
            }
        )
    )

    restored = await browsers.get_session("browser-1")
    latest = await browsers.latest_observation("browser-1")
    restored_action = await browsers.get_action("browser-1", "click-1")
    assert restored is not None
    assert restored.current_page_id == "page-1"
    assert latest is not None
    assert latest.observation_version == 1
    assert latest.content_trust == "UNTRUSTED_EXTERNAL_CONTENT"
    assert restored_action is not None
    assert restored_action.status is BrowserActionStatus.COMPLETED
    assert restored_action.result_observation_id == "observation-1"
    await database.dispose()
