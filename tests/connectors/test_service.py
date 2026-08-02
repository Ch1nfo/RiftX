from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.connectors import ConnectorHttpCapture, ConnectorSource
from riftx.connectors.service import ConnectorApplicationService
from riftx.domain import Objective, Run, Scope


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
        self.messages: list[str] = []

    async def get_run(self, run_id: str):
        assert run_id == self.run.id
        return self.run

    async def append_user_message(self, run_id: str, message: str):
        self.messages.append(message)
        return self.run


class FakeArtifacts:
    def __init__(self) -> None:
        self.items = []

    async def register_content(self, run_id: str, command):
        self.items.append(command)
        return SimpleNamespace(id=f"artifact-{len(self.items)}")


class FakeSubmissions:
    def __init__(self) -> None:
        self.items = {}

    async def get(self, source, capture_id):
        return self.items.get((source, capture_id))

    async def create(self, item):
        self.items[(item.source, item.capture_id)] = item
        return item


def capture(*, capture_id: str = "capture-1", url: str = "https://example.com/api"):
    return ConnectorHttpCapture(
        capture_id=capture_id,
        source=ConnectorSource.BROWSER,
        method="POST",
        url=url,
        request_body_base64=base64.b64encode(b"request").decode(),
        response_status=200,
        response_body_base64=base64.b64encode(b"response").decode(),
    )


async def test_connector_ingests_artifacts_without_starting_the_agent() -> None:
    runs = FakeRuns()
    artifacts = FakeArtifacts()
    submissions = FakeSubmissions()
    service = ConnectorApplicationService(
        runs=runs,
        submissions=submissions,
        artifacts=artifacts,  # type: ignore[arg-type]
    )
    first = await service.ingest("run-1", capture())
    replay = await service.ingest("run-1", capture())

    assert first.submission.id == replay.submission.id
    assert len(artifacts.items) == 3
    assert artifacts.items[0].mime_type == "message/http"
    assert artifacts.items[1].mime_type == "message/http"
    assert artifacts.items[2].mime_type == "application/json"
    assert runs.messages == []


async def test_connector_rejects_scope_escape_and_capture_id_conflict() -> None:
    service = ConnectorApplicationService(
        runs=FakeRuns(),
        submissions=FakeSubmissions(),  # type: ignore[arg-type]
        artifacts=FakeArtifacts(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="outside authorized scope"):
        await service.ingest("run-1", capture(url="https://outside.invalid/"))

    await service.ingest("run-1", capture())
    with pytest.raises(ApplicationConflictError, match="different content"):
        await service.ingest("run-1", capture(url="https://example.com/different"))
