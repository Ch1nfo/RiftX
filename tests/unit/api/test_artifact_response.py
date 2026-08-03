from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import ClientDisconnect

from riftx.api.artifact_response import ArtifactFDResponse
from riftx.api.routes import artifacts as artifact_routes


class _Lease:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.offset = 0
        self.closed = False
        self.verified = False

    def read(self, max_bytes: int) -> bytes:
        chunk = self.content[self.offset : self.offset + max_bytes]
        self.offset += len(chunk)
        return chunk

    def verify_complete(self) -> None:
        assert self.offset == len(self.content)
        self.verified = True

    def close(self) -> None:
        self.closed = True


def _scope() -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/artifact",
        "raw_path": b"/artifact",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }


async def _receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def test_artifact_fd_response_streams_and_closes_verified_lease() -> None:
    lease = _Lease(b"immutable")
    messages: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    response = ArtifactFDResponse(
        lease,  # type: ignore[arg-type]
        media_type="application/octet-stream",
        headers={"Content-Length": "9"},
    )
    await response(_scope(), _receive, send)  # type: ignore[arg-type]

    assert (
        b"".join(
            cast(bytes, message.get("body", b""))
            for message in messages
            if message["type"] == "http.response.body"
        )
        == b"immutable"
    )
    assert lease.verified is True
    assert lease.closed is True


async def test_artifact_fd_response_closes_lease_on_client_disconnect() -> None:
    lease = _Lease(b"immutable")
    send_calls = 0

    async def send(_: dict[str, object]) -> None:
        nonlocal send_calls
        send_calls += 1
        if send_calls > 1:
            raise OSError("client disconnected")

    response = ArtifactFDResponse(
        lease,  # type: ignore[arg-type]
        media_type="application/octet-stream",
        headers={"Content-Length": "9"},
    )
    with pytest.raises(ClientDisconnect):
        await response(_scope(), _receive, send)  # type: ignore[arg-type]

    assert lease.closed is True


async def test_artifact_fd_response_closes_lease_when_reader_fails() -> None:
    class _FailingLease(_Lease):
        def read(self, max_bytes: int) -> bytes:
            raise RuntimeError("synthetic read failure")

    lease = _FailingLease(b"immutable")

    async def send(_: dict[str, object]) -> None:
        return None

    response = ArtifactFDResponse(
        lease,  # type: ignore[arg-type]
        media_type="application/octet-stream",
        headers={"Content-Length": "9"},
    )
    with pytest.raises(RuntimeError, match="synthetic read failure"):
        await response(_scope(), _receive, send)  # type: ignore[arg-type]

    assert lease.closed is True


async def test_artifact_fd_response_closes_lease_when_final_verification_fails() -> None:
    class _FailingVerificationLease(_Lease):
        def verify_complete(self) -> None:
            raise RuntimeError("synthetic verification failure")

    lease = _FailingVerificationLease(b"immutable")

    async def send(_: dict[str, object]) -> None:
        return None

    response = ArtifactFDResponse(
        lease,  # type: ignore[arg-type]
        media_type="application/octet-stream",
        headers={"Content-Length": "9"},
    )
    with pytest.raises(RuntimeError, match="synthetic verification failure"):
        await response(_scope(), _receive, send)  # type: ignore[arg-type]

    assert lease.closed is True


async def test_artifact_fd_response_closes_lease_when_cancelled_during_send() -> None:
    lease = _Lease(b"immutable")
    body_send_started = asyncio.Event()
    never_complete = asyncio.Event()

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            body_send_started.set()
            await never_complete.wait()

    response = ArtifactFDResponse(
        lease,  # type: ignore[arg-type]
        media_type="application/octet-stream",
        headers={"Content-Length": "9"},
    )
    task = asyncio.create_task(
        response(_scope(), _receive, send)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(body_send_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.closed is True


@pytest.mark.parametrize("cancellation_count", (1, 2), ids=("single", "double"))
async def test_artifact_fd_response_waits_for_cancelled_read_then_closes_lease(
    cancellation_count: int,
) -> None:
    read_started = threading.Event()
    release_read = threading.Event()

    class _BlockingLease(_Lease):
        reading = False
        closed_while_reading = False

        def read(self, max_bytes: int) -> bytes:
            del max_bytes
            self.reading = True
            read_started.set()
            if not release_read.wait(timeout=2):
                raise RuntimeError("test did not release the reader")
            try:
                return b""
            finally:
                self.reading = False

        def close(self) -> None:
            self.closed_while_reading |= self.reading
            super().close()

    lease = _BlockingLease(b"")

    async def send(_: dict[str, object]) -> None:
        return None

    response = ArtifactFDResponse(
        lease,  # type: ignore[arg-type]
        media_type="application/octet-stream",
        headers={"Content-Length": "0"},
    )
    task = asyncio.create_task(
        response(_scope(), _receive, send)  # type: ignore[arg-type]
    )
    started = await asyncio.wait_for(
        asyncio.to_thread(read_started.wait, 1),
        timeout=2,
    )
    assert started is True
    for _ in range(cancellation_count):
        task.cancel()
        await asyncio.sleep(0)

    assert task.done() is False
    assert lease.closed is False
    assert lease.closed_while_reading is False
    release_read.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.closed is True
    assert lease.closed_while_reading is False


@pytest.mark.parametrize("cancellation_count", (1, 2), ids=("single", "double"))
async def test_artifact_fd_response_waits_for_cancelled_verification_then_closes_lease(
    cancellation_count: int,
) -> None:
    verification_started = threading.Event()
    release_verification = threading.Event()

    class _BlockingVerificationLease(_Lease):
        verifying = False
        closed_while_verifying = False

        def verify_complete(self) -> None:
            self.verifying = True
            verification_started.set()
            if not release_verification.wait(timeout=2):
                raise RuntimeError("test did not release verification")
            self.verifying = False
            super().verify_complete()

        def close(self) -> None:
            self.closed_while_verifying |= self.verifying
            super().close()

    lease = _BlockingVerificationLease(b"")

    async def send(_: dict[str, object]) -> None:
        return None

    response = ArtifactFDResponse(
        lease,  # type: ignore[arg-type]
        media_type="application/octet-stream",
        headers={"Content-Length": "0"},
    )
    task = asyncio.create_task(
        response(_scope(), _receive, send)  # type: ignore[arg-type]
    )
    started = await asyncio.wait_for(
        asyncio.to_thread(verification_started.wait, 1),
        timeout=2,
    )
    assert started is True
    for _ in range(cancellation_count):
        task.cancel()
        await asyncio.sleep(0)

    assert task.done() is False
    assert lease.closed is False
    assert lease.closed_while_verifying is False
    release_verification.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.closed is True
    assert lease.closed_while_verifying is False


def test_artifact_response_construction_failure_closes_lease() -> None:
    lease = _Lease(b"immutable")
    artifact = SimpleNamespace(
        size=9,
        name="evidence.bin",
        sha256="0" * 64,
        audit_id="audit-1",
        mime_type="application/\N{COLLISION SYMBOL}",
    )

    with pytest.raises(UnicodeEncodeError):
        artifact_routes._artifact_fd_response(  # noqa: SLF001
            artifact,  # type: ignore[arg-type]
            lease,  # type: ignore[arg-type]
        )

    assert lease.closed is True
