from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from riftx.api import create_app
from riftx.api.runtime import APISettings
from riftx.connectors import (
    ConnectorReceipt,
    ConnectorSource,
    ConnectorSubmission,
)
from riftx.domain import Objective, Run, RunKind, Scope, TrustProfile


class FakeRuns:
    def __init__(self) -> None:
        self.run = Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Analyze capture"),
            scope=Scope(domains=["example.com"]),
            workspace_path="/tmp/run-1",
        )
        self.created_command = None
        self.list_kwargs = None

    async def get_run(self, run_id: str):
        return self.run

    async def create_run(self, command):
        self.created_command = command
        return self.run

    async def list_runs(self, **kwargs):
        self.list_kwargs = kwargs
        return [self.run]

    async def cancel(self, run_id: str):
        return self.run


class FakeConnector:
    def __init__(self) -> None:
        self.calls = []

    async def ingest(self, run_id, capture, *, created_run=False):
        self.calls.append((run_id, capture, created_run))
        return ConnectorReceipt(
            submission=ConnectorSubmission(
                run_id=run_id,
                capture_id=capture.capture_id,
                source=capture.source,
                fingerprint=capture.fingerprint,
                request_artifact_id="request-artifact",
                response_artifact_id="response-artifact",
                manifest_artifact_id="manifest-artifact",
                summary=capture.safe_summary(),
            ),
            created_run=created_run,
            webui_path=f"/runs/{run_id}",
            events_path=f"/api/v1/connectors/runs/{run_id}/events",
            cancel_path=f"/api/v1/connectors/runs/{run_id}/cancel",
        )


def test_connector_api_targets_existing_or_new_runs_and_exposes_controls(
    tmp_path: Path,
) -> None:
    runs = FakeRuns()
    connector = FakeConnector()
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
        connector_service=connector,
        run_service=runs,
        tool_service=SimpleNamespace(node_id="local"),
    )
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]
    capture = {
        "capture_id": "capture-1",
        "source": ConnectorSource.BROWSER.value,
        "method": "GET",
        "url": "https://example.com/api",
        "response_status": 200,
    }

    with TestClient(
        app,
        headers={"Authorization": "Bearer test-only-local-operator-token-0001"},
    ) as client:
        existing = client.post(
            "/api/v1/connectors/submissions",
            json={"run_id": "run-1", "capture": capture},
        )
        assert existing.status_code == 201
        assert existing.json()["receipt"]["created_run"] is False

        new_run = client.post(
            "/api/v1/connectors/submissions",
            json={
                "new_run": {
                    "objective": "Analyze selected browser request",
                    "engagement": {"name": "Browser capture"},
                },
                "capture": {**capture, "capture_id": "capture-2"},
            },
        )
        assert new_run.status_code == 201
        assert new_run.json()["receipt"]["created_run"] is True
        assert runs.created_command.scope.domains == ["example.com"]

        listed = client.get("/api/v1/connectors/runs")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["kind"] == "general"
        assert runs.list_kwargs == {
            "status": None,
            "kind": RunKind.GENERAL,
            "limit": 100,
            "offset": 0,
        }

        events = client.get(
            "/api/v1/connectors/runs/run-1/events?after_sequence=7",
            follow_redirects=False,
        )
        assert events.status_code == 307
        assert "after_sequence=7" in events.headers["location"]

        cancelled = client.post("/api/v1/connectors/runs/run-1/cancel")
        assert cancelled.status_code == 202

        webui = client.get("/api/v1/connectors/runs/run-1/webui")
        assert webui.status_code == 200
        assert webui.json()["url"].endswith("/runs/run-1")
