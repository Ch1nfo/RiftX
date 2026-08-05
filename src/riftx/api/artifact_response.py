"""ASGI response that owns and closes one verified Artifact descriptor."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from riftx.runner import OpenedArtifactContent

_STREAM_CHUNK_SIZE = 1024 * 1024


class ArtifactFDResponse(StreamingResponse):
    """Stream exactly one verified fd and close it on every ASGI exit path."""

    def __init__(
        self,
        lease: OpenedArtifactContent,
        *,
        media_type: str,
        headers: Mapping[str, str],
        status_code: int = 200,
    ) -> None:
        self._lease = lease
        super().__init__(
            self._stream(),
            status_code=status_code,
            media_type=media_type,
            headers=dict(headers),
        )

    async def _stream(self) -> AsyncIterator[bytes]:
        while chunk := await _complete_blocking_operation(
            lambda: self._lease.read(_STREAM_CHUNK_SIZE)
        ):
            yield chunk
        await _complete_blocking_operation(self._lease.verify_complete)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> Any:
        try:
            return await super().__call__(scope, receive, send)
        finally:
            self._lease.close()


async def _complete_blocking_operation[T](operation: Callable[[], T]) -> T:
    """Defer cancellation until the response-owned descriptor worker settles."""

    worker = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # The response owner consumes the outcome after the worker settles.
            pass
    try:
        result = worker.result()
    except asyncio.CancelledError:
        raise
    except Exception:
        if cancelled:
            raise asyncio.CancelledError() from None
        raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


__all__ = ["ArtifactFDResponse"]
