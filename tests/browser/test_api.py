from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from riftx.api import create_app
from riftx.api.runtime import APISettings
from riftx.browser.service import BrowserView
from riftx.domain import (
    BrowserMode,
    BrowserObservation,
    BrowserOwner,
    BrowserPage,
    BrowserSession,
    BrowserSessionStatus,
)


class FakeBrowserService:
    def __init__(self) -> None:
        self.opened = None
        self.session = BrowserSession(
            id="browser-1",
            run_id="run-1",
            agent_session_id="agent-session-1",
            node_id="local",
            mode=BrowserMode.ATTACHED_CDP,
            status=BrowserSessionStatus.ACTIVE,
            owner=BrowserOwner.AGENT,
            cdp_endpoint="http://127.0.0.1:9222/secret",
            profile_path="/runner/private/profile",
            current_page_id="page-1",
            page_ids=["page-1"],
        )
        self.page = BrowserPage(
            id="page-1",
            browser_session_id="browser-1",
            url="https://example.com/",
            title="Example",
            last_observation_version=1,
        )
        self.observation = BrowserObservation(
            browser_session_id="browser-1",
            page_id="page-1",
            url="https://example.com/",
            title="Example",
            visible_text_excerpt="bounded text",
            observation_version=1,
        )

    def view(self) -> BrowserView:
        return BrowserView(
            session=self.session,
            pages=[self.page],
            observation=self.observation,
        )

    async def open(self, command):
        self.opened = command
        return self.view()

    async def get(self, session_id: str):
        assert session_id == "browser-1"
        return self.view()

    async def close(self, session_id: str):
        self.session.status = BrowserSessionStatus.CLOSED
        return self.view()

    async def observe(self, session_id: str, **kwargs):
        return self.view()

    async def act(self, session_id: str, command):
        return self.view()

    async def takeover(self, session_id: str):
        self.session.owner = BrowserOwner.USER
        return self.view()

    async def release(self, session_id: str):
        self.session.owner = BrowserOwner.AGENT
        return self.view()

    async def observations_after(self, session_id: str, version: int, *, limit: int = 100):
        return []


def test_browser_routes_expose_bounded_state_without_runner_secrets(tmp_path: Path) -> None:
    service = FakeBrowserService()
    settings = APISettings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        web_dist_path=tmp_path / "web",
        cors_origins=(),
    )
    control_plane = SimpleNamespace(settings=settings, browser_service=service)
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/browser/sessions",
            json={
                "run_id": "run-1",
                "agent_session_id": "agent-session-1",
                "url": "https://example.com/",
                "mode": "attached_cdp",
                "cdp_endpoint": "http://127.0.0.1:9222",
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["observation"]["visible_text_excerpt"] == "bounded text"
        assert payload["session"]["owner"] == "agent"
        assert "profile_path" not in payload["session"]
        assert "cdp_endpoint" not in payload["session"]
        assert service.opened.headless is False

        takeover = client.post("/api/v1/browser/sessions/browser-1/takeover")
        assert takeover.status_code == 200
        assert takeover.json()["session"]["owner"] == "user"
