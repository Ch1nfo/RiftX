"""Unit tests for the CLI HTTP and SSE client."""

from __future__ import annotations

import json

import httpx
import pytest

from riftx.cli.client import APIClient, RiftXAPIError, parse_sse_lines


def test_api_client_uses_shared_run_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/runs" and request.method == "POST":
            return httpx.Response(201, json={"id": "run-1", "status": "created"})
        if request.url.path.endswith("/message"):
            return httpx.Response(202, json={"accepted": True, "run": {"id": "run-1"}})
        return httpx.Response(404, json={"error": {"code": "missing", "message": "missing"}})

    with APIClient(
        "http://control-plane/",
        transport=httpx.MockTransport(handler),
    ) as client:
        created = client.create_run({"objective": "test"})
        queued = client.append_message("run-1", "continue")

    assert created["id"] == "run-1"
    assert queued["accepted"] is True
    assert requests[0].url == httpx.URL("http://control-plane/api/v1/runs")
    assert json.loads(requests[0].content) == {"objective": "test"}
    assert requests[1].url.path == "/api/v1/runs/run-1/message"
    assert json.loads(requests[1].content) == {"message": "continue"}


def test_api_client_preserves_unified_error_details() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "run_not_controllable",
                    "message": "Run is complete",
                    "details": {"status": "completed"},
                }
            },
        )

    with APIClient("http://control-plane", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RiftXAPIError) as captured:
            client.pause_run("run-1")

    assert captured.value.status_code == 409
    assert captured.value.code == "run_not_controllable"
    assert captured.value.details == {"status": "completed"}


def test_api_client_streams_sse_with_resume_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["last-event-id"] == "41"
        assert request.url.params["follow"] == "false"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                ": heartbeat\n\n"
                "id: 42\n"
                "event: run.status_changed\n"
                'data: {"sequence":42,"event_type":"run.status_changed",'
                '"payload":{"to":"running"}}\n\n'
            ),
        )

    with APIClient("http://control-plane", transport=httpx.MockTransport(handler)) as client:
        events = list(client.stream_events("run-1", last_event_id="41", follow=False))

    assert len(events) == 1
    assert events[0].id == "42"
    assert events[0].event == "run.status_changed"
    assert events[0].data == {
        "sequence": 42,
        "event_type": "run.status_changed",
        "payload": {"to": "running"},
    }


def test_parse_sse_lines_handles_multiline_and_raw_data() -> None:
    events = list(
        parse_sse_lines(
            iter(
                [
                    "id: 7",
                    "event: note",
                    "data: first",
                    "data: second",
                    "",
                ]
            )
        )
    )
    assert events[0].data == "first\nsecond"
