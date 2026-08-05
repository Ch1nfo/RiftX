from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
    RunKind,
    TrustProfile,
)


class FakeBrowserService:
    def __init__(self) -> None:
        self.opened = None
        self.calls = {
            "open": 0,
            "get": 0,
            "close": 0,
            "observe": 0,
            "act": 0,
            "takeover": 0,
            "release": 0,
            "observations_after": 0,
        }
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
        self.calls["open"] += 1
        self.opened = command
        return self.view()

    async def get(self, session_id: str, *, expected_run_id: str | None = None):
        assert session_id == "browser-1"
        assert expected_run_id in {None, self.session.run_id}
        self.calls["get"] += 1
        return self.view()

    async def resolve_run_id(self, session_id: str):
        assert session_id == "browser-1"
        return self.session.run_id

    async def close(self, session_id: str):
        self.calls["close"] += 1
        self.session.status = BrowserSessionStatus.CLOSED
        return self.view()

    async def observe(self, session_id: str, **kwargs):
        self.calls["observe"] += 1
        return self.view()

    async def act(self, session_id: str, command):
        self.calls["act"] += 1
        return self.view()

    async def takeover(self, session_id: str):
        self.calls["takeover"] += 1
        self.session.owner = BrowserOwner.USER
        return self.view()

    async def release(self, session_id: str):
        self.calls["release"] += 1
        self.session.owner = BrowserOwner.AGENT
        return self.view()

    async def observations_after(self, session_id: str, version: int, *, limit: int = 100):
        self.calls["observations_after"] += 1
        return []


class FakeRunService:
    def __init__(self, kind: RunKind = RunKind.GENERAL) -> None:
        self.kind = kind

    async def get_run(self, run_id: str):
        assert run_id == "run-1"
        return SimpleNamespace(kind=self.kind)

    async def resolve_kind(self, run_id: str):
        assert run_id == "run-1"
        return self.kind


class FakeAuditService:
    async def get_by_run_authorized(self, run_id: str, **_: object):
        assert run_id == "run-1"
        return SimpleNamespace(run=SimpleNamespace(kind=RunKind.CODE_AUDIT))


def test_browser_routes_expose_bounded_state_without_runner_secrets(tmp_path: Path) -> None:
    service = FakeBrowserService()
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "local-principal.json",
        admin_token="test-only-local-operator-token-0001",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        web_dist_path=tmp_path / "web",
        cors_origins=(),
    )
    control_plane = SimpleNamespace(
        settings=settings,
        browser_service=service,
        run_service=FakeRunService(),
        audit_service=object(),
    )
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]

    with TestClient(
        app,
        headers={"Authorization": "Bearer test-only-local-operator-token-0001"},
    ) as client:
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


@pytest.mark.parametrize(
    ("method", "path", "payload", "effect"),
    [
        (
            "POST",
            "/api/v1/browser/sessions",
            {
                "run_id": "run-1",
                "agent_session_id": "agent-session-1",
                "url": "https://example.com/",
            },
            "open",
        ),
        ("DELETE", "/api/v1/browser/sessions/browser-1", None, "close"),
        ("POST", "/api/v1/browser/sessions/browser-1/observe", {}, "observe"),
        (
            "POST",
            "/api/v1/browser/sessions/browser-1/actions",
            {
                "page_id": "page-1",
                "observation_version": 1,
                "action": "click",
                "action_key": "blocked-action",
            },
            "act",
        ),
        ("POST", "/api/v1/browser/sessions/browser-1/takeover", None, "takeover"),
        ("POST", "/api/v1/browser/sessions/browser-1/release", None, "release"),
    ],
)
def test_code_audit_browser_http_mutations_are_rejected_before_service_effect(
    tmp_path: Path,
    method: str,
    path: str,
    payload: dict[str, object] | None,
    effect: str,
) -> None:
    service = FakeBrowserService()
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "local-principal.json",
        admin_token="test-only-local-operator-token-0001",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        web_dist_path=tmp_path / "web",
        cors_origins=(),
    )
    control_plane = SimpleNamespace(
        settings=settings,
        browser_service=service,
        run_service=FakeRunService(RunKind.CODE_AUDIT),
        audit_service=FakeAuditService(),
    )
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]

    with TestClient(
        app,
        headers={"Authorization": "Bearer test-only-local-operator-token-0001"},
    ) as client:
        response = client.request(method, path, json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_kind_operation_unsupported"
    assert service.calls[effect] == 0


def test_code_audit_browser_stream_reports_kind_error_before_starting_tasks(
    tmp_path: Path,
) -> None:
    service = FakeBrowserService()
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "local-principal.json",
        admin_token="test-only-local-operator-token-0001",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        web_dist_path=tmp_path / "web",
        cors_origins=(),
    )
    control_plane = SimpleNamespace(
        settings=settings,
        browser_service=service,
        run_service=FakeRunService(RunKind.CODE_AUDIT),
        audit_service=FakeAuditService(),
    )
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]

    with TestClient(
        app,
        headers={"Authorization": "Bearer test-only-local-operator-token-0001"},
    ) as client:
        with client.websocket_connect("/api/v1/browser/sessions/browser-1/stream") as websocket:
            error = websocket.receive_json()
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()

    assert error["type"] == "error"
    assert error["code"] == "run_kind_operation_unsupported"
    assert closed.value.code == 4409
    assert service.calls["get"] == 0
    assert service.calls["observations_after"] == 0
