"""Target HTTP client that executes inside the Runner host network."""

from __future__ import annotations

import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from riftx.domain import RunnerCommand, RunnerCommandKind, RunnerCommandStatus
from riftx.scope import ScopeGuard, ScopeTargetKind
from riftx.target_http.models import (
    TargetHttpExchange,
    TargetHttpResult,
    TargetHttpRunnerRequest,
)

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 10


class TargetHttpRunner(Protocol):
    async def execute(self, launch: TargetHttpRunnerRequest) -> TargetHttpExchange: ...


class TargetHttpCommandControl(Protocol):
    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> tuple[RunnerCommand, bool]: ...

    async def wait_command(
        self,
        command_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.1,
    ) -> RunnerCommand: ...

    async def read_command_output(self, command_id: str) -> bytes: ...


class ClientCertificateResolver(Protocol):
    async def resolve(self, reference: str) -> str | tuple[str, str]: ...


ClientFactory = Callable[..., httpx.AsyncClient]


class RunnerTargetHttpClient:
    """Use Runner-local DNS, routes, VPN, proxy variables, and certificate stores."""

    def __init__(
        self,
        *,
        node_id: str = "local",
        certificate_resolver: ClientCertificateResolver | None = None,
        client_factory: ClientFactory = httpx.AsyncClient,
    ) -> None:
        self._node_id = node_id
        self._certificates = certificate_resolver
        self._client_factory = client_factory

    async def execute(self, launch: TargetHttpRunnerRequest) -> TargetHttpExchange:
        if launch.node_id != self._node_id:
            raise ValueError("Target HTTP launch targets a different Runner node")
        request = launch.request
        guard = ScopeGuard(launch.scope)
        guard.require(request.url, kind=ScopeTargetKind.URL)
        certificate = None
        if request.client_cert_ref is not None:
            if self._certificates is None:
                raise ValueError("Target HTTP client certificate reference cannot be resolved")
            certificate = await self._certificates.resolve(request.client_cert_ref)
            _validate_certificate_paths(certificate)
        client = self._client_factory(
            verify=request.verify_tls,
            cert=certificate,
            proxy=request.proxy,
            timeout=httpx.Timeout(request.timeout_seconds),
            follow_redirects=False,
            trust_env=True,
        )
        started = time.monotonic()
        try:
            response, body, final_url, redirects, truncated = await _send(client, launch, guard)
        finally:
            await client.aclose()
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        headers = {key: value for key, value in response.headers.items()}
        content_type = response.headers.get("content-type")
        declared_length = _content_length(response.headers.get("content-length"))
        result = TargetHttpResult(
            execution_key=request.execution_key,
            request_hash=request.fingerprint,
            status_code=response.status_code,
            reason_phrase=response.reason_phrase or None,
            response_headers=headers,
            elapsed_ms=elapsed_ms,
            content_type=content_type,
            content_length=declared_length if declared_length is not None else len(body),
            body_excerpt=_body_excerpt(body, content_type),
            redirect_location=response.headers.get("location"),
            tls_summary=_tls_summary(response, request.verify_tls, request.client_cert_ref),
            final_url=final_url,
            redirect_chain=redirects,
            truncated=truncated,
        )
        return TargetHttpExchange(result=result, response_body=body)


class RemoteTargetHttpClient:
    """Dispatch Target HTTP to an authenticated independently deployed Runner."""

    def __init__(self, control: TargetHttpCommandControl) -> None:
        self._control = control

    async def execute(self, launch: TargetHttpRunnerRequest) -> TargetHttpExchange:
        request = launch.request
        command, _ = await self._control.enqueue(
            launch.node_id,
            kind=RunnerCommandKind.TARGET_HTTP,
            idempotency_key=f"target-http:{request.execution_key}",
            payload={
                "launch": {
                    "run_id": launch.run_id,
                    "session_id": launch.session_id,
                    "tool_call_id": launch.tool_call_id,
                    "node_id": launch.node_id,
                    "scope": launch.scope.model_dump(mode="json"),
                    "request": request.runner_payload(),
                },
                "max_response_bytes": request.max_response_bytes,
            },
        )
        completed = await self._control.wait_command(
            command.id,
            timeout_seconds=request.timeout_seconds + 30,
        )
        if completed.status is not RunnerCommandStatus.COMPLETED:
            raise RuntimeError(
                f"Remote Target HTTP command failed: {completed.error or 'unknown error'}"
            )
        raw_result = completed.result.get("result")
        if not isinstance(raw_result, dict):
            raise RuntimeError("Remote Target HTTP command omitted its structured result")
        result = TargetHttpResult.model_validate(raw_result)
        if (
            result.execution_key != request.execution_key
            or result.request_hash != request.fingerprint
        ):
            raise RuntimeError("Remote Target HTTP result identity does not match its request")
        body = await self._control.read_command_output(completed.id)
        if len(body) > request.max_response_bytes:
            raise RuntimeError("Remote Target HTTP response exceeds its declared limit")
        return TargetHttpExchange(result=result, response_body=body)


class NodeTargetHttpRouter:
    def __init__(
        self,
        *,
        local_node_id: str,
        local: TargetHttpRunner,
        remote: TargetHttpRunner,
    ) -> None:
        self._local_node_id = local_node_id
        self._local = local
        self._remote = remote

    async def execute(self, launch: TargetHttpRunnerRequest) -> TargetHttpExchange:
        runner = self._local if launch.node_id == self._local_node_id else self._remote
        return await runner.execute(launch)


async def _send(
    client: httpx.AsyncClient,
    launch: TargetHttpRunnerRequest,
    guard: ScopeGuard,
) -> tuple[httpx.Response, bytes, str, list[str], bool]:
    request = launch.request
    current = request.url
    method = request.method
    headers = dict(request.headers)
    content = request.body
    json_body = request.json_body
    redirects: list[str] = []
    for _ in range(_MAX_REDIRECTS + 1):
        guard.require(current, kind=ScopeTargetKind.URL)
        outbound = client.build_request(
            method,
            current,
            params=request.query if not redirects else None,
            headers=headers,
            cookies=request.cookies if not redirects else None,
            content=content,
            json=json_body,
        )
        response = await client.send(outbound, stream=True, follow_redirects=False)
        if not request.follow_redirects or response.status_code not in _REDIRECT_CODES:
            try:
                body, truncated = await _bounded_body(response, request.max_response_bytes)
            finally:
                await response.aclose()
            return response, body, str(response.url), redirects, truncated
        location = response.headers.get("location")
        await response.aclose()
        if not location:
            raise RuntimeError("Target HTTP redirect omitted Location")
        destination = urljoin(current, location)
        guard.require(destination, kind=ScopeTargetKind.URL)
        redirects.append(destination)
        if _origin(current) != _origin(destination):
            for name in tuple(headers):
                if name.lower() in {"authorization", "cookie", "proxy-authorization"}:
                    headers.pop(name)
        if response.status_code == 303 or (
            response.status_code in {301, 302} and method not in {"GET", "HEAD"}
        ):
            method = "GET"
            content = None
            json_body = None
            for name in tuple(headers):
                if name.lower() in {"content-length", "content-type", "transfer-encoding"}:
                    headers.pop(name)
        current = destination
    raise RuntimeError(f"Target HTTP exceeded {_MAX_REDIRECTS} redirects")


async def _bounded_body(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    body = bytearray()
    async for part in response.aiter_bytes():
        remaining = limit + 1 - len(body)
        if remaining <= 0:
            break
        body.extend(part[:remaining])
        if len(body) > limit:
            return bytes(body[:limit]), True
    return bytes(body), False


def _validate_certificate_paths(value: str | tuple[str, str]) -> None:
    paths = (value,) if isinstance(value, str) else value
    if not paths or any(not Path(path).is_file() for path in paths):
        raise ValueError("Target HTTP client certificate files are unavailable on the Runner")


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
    )


def _content_length(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _body_excerpt(body: bytes, content_type: str | None) -> str | None:
    if not body:
        return None
    mime = (content_type or "").lower()
    if not (mime.startswith("text/") or "json" in mime or "xml" in mime):
        return None
    return body[:8192].decode("utf-8", errors="replace")


def _tls_summary(
    response: httpx.Response,
    verified: bool,
    client_cert_ref: str | None,
) -> dict[str, object] | None:
    if response.url.scheme != "https":
        return None
    summary: dict[str, object] = {
        "verified": verified,
        "client_certificate_used": client_cert_ref is not None,
    }
    stream = response.extensions.get("network_stream")
    ssl_object = stream.get_extra_info("ssl_object") if stream is not None else None
    if isinstance(ssl_object, ssl.SSLObject | ssl.SSLSocket):
        summary["protocol"] = ssl_object.version()
        cipher = ssl_object.cipher()
        summary["cipher"] = cipher[0] if cipher else None
    return summary
