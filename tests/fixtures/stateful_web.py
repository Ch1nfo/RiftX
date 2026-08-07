"""Resettable localhost target for stateful Web authorization tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StatefulWebRequest:
    method: str
    path: str
    actor: str | None
    status_code: int


@dataclass(slots=True)
class StatefulWebTarget:
    server: asyncio.AbstractServer
    base_url: str
    requests: list[StatefulWebRequest]

    @classmethod
    async def start(cls) -> StatefulWebTarget:
        requests: list[StatefulWebRequest] = []
        sessions: dict[str, str] = {}
        users = {
            "alice": "fixture-password-alice",
            "bob": "fixture-password-bob",
        }
        objects = {
            "object-a": {"owner": "alice", "value": "alice-private-object"},
            "object-b": {"owner": "bob", "value": "bob-private-object"},
        }

        async def handle(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            try:
                header_bytes = await reader.readuntil(b"\r\n\r\n")
                header_lines = header_bytes.decode("iso-8859-1").split("\r\n")
                method, path, _protocol = header_lines[0].split(" ", maxsplit=2)
                headers = {
                    name.strip().lower(): value.strip()
                    for line in header_lines[1:]
                    if ":" in line
                    for name, value in [line.split(":", maxsplit=1)]
                }
                content_length = int(headers.get("content-length", "0"))
                body = await reader.readexactly(content_length) if content_length else b""
                actor = _session_actor(headers.get("cookie"), sessions)
                status_code = 404
                response_headers: dict[str, str] = {}
                payload: dict[str, object] = {"error": "not_found"}

                if method == "POST" and path == "/login":
                    credentials = _json_object(body)
                    username = credentials.get("username")
                    password = credentials.get("password")
                    if (
                        isinstance(username, str)
                        and isinstance(password, str)
                        and users.get(username) == password
                    ):
                        actor = username
                        token = f"fixture-session-{username}"
                        sessions[token] = username
                        status_code = 200
                        response_headers["Set-Cookie"] = (
                            f"riftx_session={token}; Path=/; HttpOnly; SameSite=Strict"
                        )
                        payload = {"authenticated": True, "user": username}
                    else:
                        status_code = 401
                        payload = {"authenticated": False}
                elif method == "GET" and path.startswith("/objects/"):
                    object_id = path.removeprefix("/objects/")
                    item = objects.get(object_id)
                    if actor is None:
                        status_code = 401
                        payload = {"error": "authentication_required"}
                    elif item is None:
                        status_code = 404
                    else:
                        # Deliberate fixture flaw: authenticated users can cross
                        # the object ownership boundary. C2 will evidence it.
                        status_code = 200
                        payload = {"id": object_id, **item}

                requests.append(
                    StatefulWebRequest(
                        method=method,
                        path=path,
                        actor=actor,
                        status_code=status_code,
                    )
                )
                await _respond(writer, status_code, payload, response_headers)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        return cls(
            server=server,
            base_url=f"http://127.0.0.1:{port}",
            requests=requests,
        )

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


def session_cookie(response_headers: dict[str, str]) -> str:
    raw = next(
        (value for name, value in response_headers.items() if name.lower() == "set-cookie"),
        "",
    )
    cookie = raw.split(";", maxsplit=1)[0]
    if not cookie.startswith("riftx_session=") or len(cookie) <= len("riftx_session="):
        raise ValueError("Stateful Web fixture login omitted its session cookie")
    return cookie.removeprefix("riftx_session=")


def _session_actor(cookie_header: str | None, sessions: dict[str, str]) -> str | None:
    if cookie_header is None:
        return None
    cookies = {
        name.strip(): value.strip()
        for item in cookie_header.split(";")
        if "=" in item
        for name, value in [item.split("=", maxsplit=1)]
    }
    return sessions.get(cookies.get("riftx_session", ""))


def _json_object(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


async def _respond(
    writer: asyncio.StreamWriter,
    status_code: int,
    payload: dict[str, object],
    headers: dict[str, str],
) -> None:
    reasons = {200: "OK", 401: "Unauthorized", 404: "Not Found"}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    lines = [
        f"HTTP/1.1 {status_code} {reasons[status_code]}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Connection: close",
        *(f"{name}: {value}" for name, value in headers.items()),
        "",
        "",
    ]
    writer.write("\r\n".join(lines).encode("ascii") + body)
    await writer.drain()


__all__ = ["StatefulWebRequest", "StatefulWebTarget", "session_cookie"]
