"""Target HTTP client that executes inside the Runner host network."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import JsonValue

from riftx.domain import (
    RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandStatus,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
)
from riftx.domain.base import new_id
from riftx.scope import ScopeGuard, ScopeTargetKind
from riftx.target_http.errors import (
    TargetHttpRunnerExecutionCancelledError,
    TargetHttpRunnerExecutionUncertainError,
)
from riftx.target_http.models import (
    TargetHttpExchange,
    TargetHttpResult,
    TargetHttpRunnerRequest,
    TargetHttpRunnerStopOutcome,
)

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 10

EffectGuard = Callable[[], Awaitable[None]]


class TargetHttpRunner(Protocol):
    async def execute(
        self,
        launch: TargetHttpRunnerRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> TargetHttpExchange: ...

    async def stop_run(
        self,
        run_id: str,
        *,
        node_id: str,
        tool_call_ids: Sequence[str],
    ) -> list[TargetHttpRunnerStopOutcome]: ...


class TargetHttpCommandControl(Protocol):
    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
        run_id: str,
        origin: RunnerCommandOrigin,
        operation_family: RunnerOperationFamily,
        resource_kind: RunnerResourceKind,
        resource_id: str,
        execution_id: str | None = None,
        output_contract: RunnerOutputContract | None = None,
        target: RunnerPrincipal | None = None,
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


@dataclass(slots=True)
class _ActiveTargetHttpRequest:
    task: asyncio.Task[TargetHttpExchange] | None = None
    client: httpx.AsyncClient | None = None
    client_opened: bool = False
    client_closed: bool = False
    close_error: str | None = None
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RunnerTargetHttpClient:
    """Use Runner-local DNS, routes, VPN, proxy variables, and certificate stores."""

    def __init__(
        self,
        *,
        node_id: str = "local",
        certificate_resolver: ClientCertificateResolver | None = None,
        client_factory: ClientFactory = httpx.AsyncClient,
        stop_timeout_seconds: float = 5.0,
    ) -> None:
        if stop_timeout_seconds <= 0:
            raise ValueError("Target HTTP stop timeout must be positive")
        self._node_id = node_id
        self._certificates = certificate_resolver
        self._client_factory = client_factory
        self._stop_timeout_seconds = stop_timeout_seconds
        self._active: dict[tuple[str, str], _ActiveTargetHttpRequest] = {}
        self._active_lock = asyncio.Lock()

    async def execute(
        self,
        launch: TargetHttpRunnerRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> TargetHttpExchange:
        if launch.node_id != self._node_id:
            raise ValueError("Target HTTP launch targets a different Runner node")
        key = (launch.run_id, launch.tool_call_id)
        active = _ActiveTargetHttpRequest()
        async with self._active_lock:
            if key in self._active:
                raise RuntimeError("Target HTTP intent is already active on this Runner")
            task = asyncio.create_task(
                self._execute_request(launch, active, effect_guard),
                name=f"target-http:{launch.run_id}:{launch.tool_call_id}",
            )
            active.task = task
            self._active[key] = active
        try:
            return await task
        except asyncio.CancelledError as exc:
            safely_closed = not active.client_opened or active.client_closed
            outcome = TargetHttpRunnerStopOutcome(
                tool_call_id=launch.tool_call_id,
                confirmed=task.done() and safely_closed,
                reason=(
                    "target_http_local_task_terminated"
                    if task.done() and safely_closed
                    else active.close_error or "target_http_local_task_stop_unconfirmed"
                ),
            )
            raise TargetHttpRunnerExecutionCancelledError(
                "Target HTTP execution was cancelled",
                stop_outcome=outcome,
            ) from exc
        except Exception as exc:
            if active.client_opened and active.close_error is not None:
                outcome = TargetHttpRunnerStopOutcome(
                    tool_call_id=launch.tool_call_id,
                    confirmed=active.client_closed,
                    reason=(
                        "target_http_local_task_terminated"
                        if active.client_closed
                        else (f"target_http_local_client_close_unconfirmed: {active.close_error}")
                    ),
                )
                raise TargetHttpRunnerExecutionUncertainError(
                    "Target HTTP client could not be proven closed",
                    stop_outcome=outcome,
                ) from exc
            raise
        finally:
            async with self._active_lock:
                if self._active.get(key) is active and (
                    not active.client_opened or active.client_closed
                ):
                    self._active.pop(key, None)

    async def stop_run(
        self,
        run_id: str,
        *,
        node_id: str,
        tool_call_ids: Sequence[str],
    ) -> list[TargetHttpRunnerStopOutcome]:
        intent_ids = tuple(dict.fromkeys(tool_call_ids))
        if node_id != self._node_id:
            return [
                TargetHttpRunnerStopOutcome(
                    tool_call_id=intent_id,
                    confirmed=False,
                    reason="target_http_local_node_mismatch",
                )
                for intent_id in intent_ids
            ]
        async with self._active_lock:
            active = {intent_id: self._active.get((run_id, intent_id)) for intent_id in intent_ids}
        loop = asyncio.get_running_loop()
        stop_deadline = loop.time() + self._stop_timeout_seconds
        tasks = {
            intent_id: item.task
            for intent_id, item in active.items()
            if item is not None and item.task is not None
        }
        for task in tasks.values():
            task.cancel()
        if tasks:
            done, _ = await asyncio.wait(
                tasks.values(),
                timeout=max(0.0, stop_deadline - loop.time()),
            )
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass

        retry_closes: dict[str, asyncio.Task[None]] = {}
        for intent_id, item in active.items():
            if (
                item is not None
                and item.task is not None
                and item.task.done()
                and item.client_opened
                and not item.client_closed
                and item.client is not None
            ):
                retry_closes[intent_id] = asyncio.create_task(
                    self._close_client(item),
                    name=f"target-http-close-retry:{run_id}:{intent_id}",
                )
        if retry_closes:
            _, pending = await asyncio.wait(
                retry_closes.values(),
                timeout=max(0.0, stop_deadline - loop.time()),
            )
            for pending_close_task in pending:
                pending_close_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for close_task in retry_closes.values():
                if close_task.done():
                    try:
                        close_task.result()
                    except BaseException:
                        pass

        outcomes: list[TargetHttpRunnerStopOutcome] = []
        for intent_id in intent_ids:
            item = active[intent_id]
            if item is None or item.task is None:
                outcomes.append(
                    TargetHttpRunnerStopOutcome(
                        tool_call_id=intent_id,
                        confirmed=False,
                        reason="target_http_local_task_not_registered",
                    )
                )
                continue
            safely_closed = not item.client_opened or item.client_closed
            if item.task.done() and safely_closed:
                outcomes.append(
                    TargetHttpRunnerStopOutcome(
                        tool_call_id=intent_id,
                        confirmed=True,
                        reason="target_http_local_task_terminated",
                    )
                )
                continue
            reason = item.close_error or "target_http_local_task_stop_unconfirmed"
            outcomes.append(
                TargetHttpRunnerStopOutcome(
                    tool_call_id=intent_id,
                    confirmed=False,
                    reason=reason,
                )
            )
        async with self._active_lock:
            for intent_id, item in active.items():
                if (
                    item is not None
                    and item.task is not None
                    and item.task.done()
                    and (not item.client_opened or item.client_closed)
                    and self._active.get((run_id, intent_id)) is item
                ):
                    self._active.pop((run_id, intent_id), None)
        return outcomes

    async def _execute_request(
        self,
        launch: TargetHttpRunnerRequest,
        active: _ActiveTargetHttpRequest,
        effect_guard: EffectGuard | None,
    ) -> TargetHttpExchange:
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
        active.client = client
        active.client_opened = True
        started = time.monotonic()
        try:
            async with asyncio.timeout(request.timeout_seconds):
                response, body, final_url, redirects, truncated = await _send(
                    client,
                    launch,
                    guard,
                    effect_guard,
                )
        finally:
            await self._close_client(active)
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

    @staticmethod
    async def _close_client(active: _ActiveTargetHttpRequest) -> None:
        client = active.client
        if client is None:
            return
        async with active.close_lock:
            if active.client_closed:
                return
            try:
                await client.aclose()
            except BaseException as exc:
                active.close_error = f"{type(exc).__name__}: {exc}"
                raise
            else:
                active.client_closed = True


class RemoteTargetHttpClient:
    """Dispatch Target HTTP to an authenticated independently deployed Runner."""

    def __init__(
        self,
        control: TargetHttpCommandControl,
        *,
        stop_timeout_seconds: float = 5.0,
    ) -> None:
        if stop_timeout_seconds <= 0:
            raise ValueError("Remote Target HTTP stop timeout must be positive")
        self._control = control
        self._stop_timeout_seconds = stop_timeout_seconds

    async def execute(
        self,
        launch: TargetHttpRunnerRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> TargetHttpExchange:
        request = launch.request
        if effect_guard is not None:
            await effect_guard()
        command, _ = await self._control.enqueue(
            launch.node_id,
            kind=RunnerCommandKind.TARGET_HTTP,
            idempotency_key=f"target-http:{request.execution_key}",
            run_id=launch.run_id,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=RunnerOperationFamily.TARGET_HTTP,
            resource_kind=RunnerResourceKind.TARGET_HTTP_INTENT,
            resource_id=launch.tool_call_id,
            output_contract=RunnerOutputContract(
                max_result_bytes=64 * 1024,
                max_output_bytes=request.max_response_bytes,
                allowed_streams=("command",),
                result_schema="riftx.runner-result/target-http/v1",
            ),
            payload={
                "launch": {
                    "run_id": launch.run_id,
                    "session_id": launch.session_id,
                    "tool_call_id": launch.tool_call_id,
                    "node_id": launch.node_id,
                    "scope": launch.scope.model_dump(mode="json"),
                    "request": request.runner_payload(),
                },
            },
        )
        try:
            completed = await self._control.wait_command(
                command.id,
                timeout_seconds=request.timeout_seconds + 30,
            )
        except asyncio.CancelledError as exc:
            outcome = await self._stop_interrupted_execution(launch)
            raise TargetHttpRunnerExecutionCancelledError(
                "Remote Target HTTP command wait was cancelled",
                stop_outcome=outcome,
            ) from exc
        except Exception as exc:
            outcome = await self._stop_interrupted_execution(launch)
            raise TargetHttpRunnerExecutionUncertainError(
                (
                    "Remote Target HTTP command wait failed after durable dispatch: "
                    f"{type(exc).__name__}: {exc}"
                ),
                stop_outcome=outcome,
            ) from exc
        if completed.status is not RunnerCommandStatus.COMPLETED:
            # FAILED is not physical stop evidence.  In particular, a replayed
            # delivery can be suppressed after an earlier lease already sent
            # the request.  Persist and await a separate cancellation command
            # before allowing the Control Plane to terminalize the intent.
            outcome = await self._stop_interrupted_execution(launch)
            raise TargetHttpRunnerExecutionUncertainError(
                (
                    "Remote Target HTTP command did not complete safely after durable "
                    f"dispatch ({completed.status.value}): "
                    f"{completed.error or 'no Runner error was provided'}"
                ),
                stop_outcome=outcome,
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

    async def _stop_interrupted_execution(
        self,
        launch: TargetHttpRunnerRequest,
    ) -> TargetHttpRunnerStopOutcome:
        stop_task = asyncio.create_task(
            self.stop_run(
                launch.run_id,
                node_id=launch.node_id,
                tool_call_ids=[launch.tool_call_id],
            ),
            name=(f"target-http-safety-stop:{launch.run_id}:{launch.tool_call_id}"),
        )
        while True:
            try:
                outcomes = await asyncio.shield(stop_task)
                break
            except asyncio.CancelledError:
                if stop_task.done():
                    try:
                        outcomes = stop_task.result()
                    except BaseException as exc:
                        return TargetHttpRunnerStopOutcome(
                            tool_call_id=launch.tool_call_id,
                            confirmed=False,
                            reason=(
                                f"target_http_remote_stop_unconfirmed: {type(exc).__name__}: {exc}"
                            ),
                        )
                    break
                # A repeated Control Plane cancellation must not abandon the
                # durable safety command while its acknowledgement is pending.
                continue
            except Exception as exc:
                return TargetHttpRunnerStopOutcome(
                    tool_call_id=launch.tool_call_id,
                    confirmed=False,
                    reason=(f"target_http_remote_stop_unconfirmed: {type(exc).__name__}: {exc}"),
                )
        if len(outcomes) != 1 or outcomes[0].tool_call_id != launch.tool_call_id:
            return TargetHttpRunnerStopOutcome(
                tool_call_id=launch.tool_call_id,
                confirmed=False,
                reason="target_http_remote_stop_outcome_invalid",
            )
        return outcomes[0]

    async def stop_run(
        self,
        run_id: str,
        *,
        node_id: str,
        tool_call_ids: Sequence[str],
    ) -> list[TargetHttpRunnerStopOutcome]:
        intent_ids = tuple(dict.fromkeys(tool_call_ids))
        commands: dict[str, RunnerCommand] = {}
        outcomes: dict[str, TargetHttpRunnerStopOutcome] = {}
        for intent_id in intent_ids:
            try:
                command, _ = await self._control.enqueue(
                    node_id,
                    kind=RunnerCommandKind.TARGET_HTTP_CANCEL,
                    idempotency_key=_target_http_cancel_key(run_id, intent_id),
                    run_id=run_id,
                    origin=RunnerCommandOrigin.SAFETY_RECONCILER,
                    operation_family=RunnerOperationFamily.SAFETY_STOP,
                    resource_kind=RunnerResourceKind.TARGET_HTTP_INTENT,
                    resource_id=intent_id,
                    output_contract=RunnerOutputContract(
                        result_schema="riftx.runner-result/target-http-stop/v1",
                        stop_ack_schema=RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA,
                    ),
                    payload={
                        "run_id": run_id,
                        "tool_call_ids": [intent_id],
                    },
                )
            except Exception as exc:
                outcomes[intent_id] = TargetHttpRunnerStopOutcome(
                    tool_call_id=intent_id,
                    confirmed=False,
                    reason=f"target_http_remote_stop_enqueue_failed: {type(exc).__name__}: {exc}",
                )
            else:
                commands[intent_id] = command

        async def wait_for_ack(
            intent_id: str,
            command: RunnerCommand,
        ) -> TargetHttpRunnerStopOutcome:
            try:
                completed = await self._control.wait_command(
                    command.id,
                    timeout_seconds=self._stop_timeout_seconds,
                )
            except Exception as exc:
                return TargetHttpRunnerStopOutcome(
                    tool_call_id=intent_id,
                    confirmed=False,
                    reason=(f"target_http_remote_stop_unconfirmed: {type(exc).__name__}: {exc}"),
                )
            if completed.status is not RunnerCommandStatus.COMPLETED:
                return TargetHttpRunnerStopOutcome(
                    tool_call_id=intent_id,
                    confirmed=False,
                    reason=(completed.error or "target_http_remote_stop_runner_command_failed"),
                )
            raw_outcomes = completed.result.get("outcomes")
            if not isinstance(raw_outcomes, list) or len(raw_outcomes) != 1:
                return TargetHttpRunnerStopOutcome(
                    tool_call_id=intent_id,
                    confirmed=False,
                    reason="target_http_remote_stop_runner_ack_invalid",
                )
            try:
                outcome = TargetHttpRunnerStopOutcome.model_validate(raw_outcomes[0])
            except Exception as exc:
                return TargetHttpRunnerStopOutcome(
                    tool_call_id=intent_id,
                    confirmed=False,
                    reason=f"target_http_remote_stop_runner_ack_invalid: {exc}",
                )
            if outcome.tool_call_id != intent_id:
                return TargetHttpRunnerStopOutcome(
                    tool_call_id=intent_id,
                    confirmed=False,
                    reason="target_http_remote_stop_runner_ack_identity_mismatch",
                )
            return outcome

        if commands:
            acknowledged = await asyncio.gather(
                *(wait_for_ack(intent_id, command) for intent_id, command in commands.items())
            )
            outcomes.update({item.tool_call_id: item for item in acknowledged})
        return [outcomes[intent_id] for intent_id in intent_ids]


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

    async def execute(
        self,
        launch: TargetHttpRunnerRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> TargetHttpExchange:
        runner = self._local if launch.node_id == self._local_node_id else self._remote
        return await runner.execute(launch, effect_guard=effect_guard)

    async def stop_run(
        self,
        run_id: str,
        *,
        node_id: str,
        tool_call_ids: Sequence[str],
    ) -> list[TargetHttpRunnerStopOutcome]:
        runner = self._local if node_id == self._local_node_id else self._remote
        return await runner.stop_run(
            run_id,
            node_id=node_id,
            tool_call_ids=tool_call_ids,
        )


async def _send(
    client: httpx.AsyncClient,
    launch: TargetHttpRunnerRequest,
    guard: ScopeGuard,
    effect_guard: EffectGuard | None,
) -> tuple[httpx.Response, bytes, str, list[str], bool]:
    request = launch.request
    current = str(httpx.URL(request.url).copy_merge_params(request.query))
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
            headers=headers,
            cookies=request.cookies if not redirects else None,
            content=content,
            json=json_body,
        )
        if effect_guard is not None:
            await effect_guard()
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


def _target_http_cancel_key(run_id: str, tool_call_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{tool_call_id}".encode()).hexdigest()
    # Every delivery attempt needs its own durable command.  The Runner-side
    # tombstone makes the operation idempotent; reusing a failed command row
    # here would otherwise make a transient offline/journal failure permanent.
    return f"target-http-cancel:{digest[:32]}:{new_id()}"


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
) -> dict[str, JsonValue] | None:
    if response.url.scheme != "https":
        return None
    summary: dict[str, JsonValue] = {
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
