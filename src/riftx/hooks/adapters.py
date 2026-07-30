"""Python, argv Command, and HTTP Runtime Hook adapters."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence

import httpx

from .models import HookRequest, HookResult

PythonHookCallable = Callable[[HookRequest], HookResult | Awaitable[HookResult]]


class PythonHook:
    def __init__(self, callback: PythonHookCallable) -> None:
        self._callback = callback

    async def __call__(self, request: HookRequest) -> HookResult:
        result = self._callback(request)
        if inspect.isawaitable(result):
            result = await result
        return HookResult.model_validate(result)


class CommandHook:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not argv or any(not item for item in argv):
            raise ValueError("Command Hook requires a non-empty argv")
        self._argv = tuple(argv)
        self._environment = dict(environment) if environment is not None else None

    async def __call__(self, request: HookRequest) -> HookResult:
        process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment,
        )
        try:
            stdout, stderr = await process.communicate(
                json.dumps(request.model_dump(mode="json")).encode()
            )
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            detail = stderr.decode(errors="replace")[:2000]
            raise RuntimeError(
                f"Command Hook exited with {process.returncode}: {detail}"
            )
        return HookResult.model_validate_json(stdout)


class HTTPHook:
    def __init__(self, url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client

    async def __call__(self, request: HookRequest) -> HookResult:
        if self._client is not None:
            response = await self._client.post(
                self._url,
                json=request.model_dump(mode="json"),
            )
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._url,
                    json=request.model_dump(mode="json"),
                )
        response.raise_for_status()
        return HookResult.model_validate(response.json())
