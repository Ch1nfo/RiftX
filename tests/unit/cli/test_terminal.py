"""Unit tests for authenticated terminal WebSocket connections."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any
from urllib.parse import parse_qs, urlsplit

from rich.console import Console

from riftx.cli import terminal
from riftx.cli.client import APIClient


class _FakeWebSocket:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = [json.dumps(response) for response in responses]
        self.sent: list[dict[str, object]] = []

    def __enter__(self) -> _FakeWebSocket:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self) -> str:
        return self._responses.pop(0)

    def close(self) -> None:
        return None


def _assert_authenticated_connect(
    call: tuple[str, dict[str, Any]],
    *,
    token: str,
    cursor: int,
) -> None:
    url, kwargs = call
    parsed = urlsplit(url)

    assert kwargs["additional_headers"] == {"Authorization": f"Bearer {token}"}
    assert parse_qs(parsed.query) == {"cursor": [str(cursor)]}
    assert token not in url
    assert "bearer" not in parsed.query.lower()
    assert "authorization" not in parsed.query.lower()


def test_attach_terminal_sends_bearer_header_without_leaking_token_in_url(
    monkeypatch: Any,
) -> None:
    token = "attach-local-operator-secret"
    websocket = _FakeWebSocket([{"type": "state", "session": {"status": "closed"}}])
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_connect(url: str, **kwargs: Any) -> _FakeWebSocket:
        calls.append((url, kwargs))
        return websocket

    monkeypatch.setattr(terminal, "connect", fake_connect)

    with APIClient("http://127.0.0.1:8787", admin_token=token) as client:
        terminal.attach_terminal(
            client,
            "terminal-1",
            Console(file=StringIO()),
            take_over=False,
            cursor=17,
        )

    assert len(calls) == 1
    _assert_authenticated_connect(calls[0], token=token, cursor=17)


def test_control_terminal_sends_bearer_header_without_leaking_token_in_url(
    monkeypatch: Any,
) -> None:
    token = "control-local-operator-secret"
    websocket = _FakeWebSocket(
        [{"type": "state", "session": {"id": "terminal-1", "owner": "user"}}]
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_connect(url: str, **kwargs: Any) -> _FakeWebSocket:
        calls.append((url, kwargs))
        return websocket

    monkeypatch.setattr(terminal, "connect", fake_connect)

    with APIClient("http://localhost:8787", admin_token=token) as client:
        result = terminal.control_terminal(client, "terminal-1", "takeover")

    assert result["owner"] == "user"
    assert websocket.sent == [{"type": "takeover"}]
    assert len(calls) == 1
    _assert_authenticated_connect(calls[0], token=token, cursor=0)
