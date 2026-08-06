from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from riftx.domain import Scope
from riftx.runner.target_http import RunnerTargetHttpClient
from riftx.scope import ScopeViolationError
from riftx.target_http.errors import TargetHttpRunnerExecutionUncertainError
from riftx.target_http.models import TargetHttpRequest, TargetHttpRunnerRequest


class ClientFactory:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.kwargs = None
        self.client = None

    def __call__(self, **kwargs):
        self.kwargs = dict(kwargs)
        kwargs.pop("proxy")
        kwargs.pop("cert")
        kwargs.pop("trust_env")
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler), **kwargs)
        return self.client


def launch(request: TargetHttpRequest, scope: Scope | None = None) -> TargetHttpRunnerRequest:
    return TargetHttpRunnerRequest(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        node_id="local",
        scope=scope or Scope(domains=["target.internal"]),
        request=request,
    )


async def test_runner_uses_host_network_options_and_structured_request() -> None:
    observed: httpx.Request | None = None

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal observed
        observed = request
        return httpx.Response(
            201,
            headers={"content-type": "application/json", "x-test": "ok"},
            json={"created": True},
        )

    factory = ClientFactory(handle)
    runner = RunnerTargetHttpClient(client_factory=factory)
    exchange = await runner.execute(
        launch(
            TargetHttpRequest(
                execution_key="execution-key",
                method="post",
                url="https://target.internal/api?token=private",
                headers={"X-Test": "value"},
                query={"page": "1"},
                json_body={"name": "RiftX"},
                cookies={"session": "authorized-test-cookie"},
                proxy="http://127.0.0.1:8080",
                verify_tls=False,
            )
        )
    )

    assert observed is not None
    assert observed.method == "POST"
    assert observed.url == "https://target.internal/api?token=private&page=1"
    assert observed.headers["x-test"] == "value"
    assert observed.headers["cookie"] == "session=authorized-test-cookie"
    assert observed.read() == b'{"name":"RiftX"}'
    assert factory.kwargs["proxy"] == "http://127.0.0.1:8080"
    assert factory.kwargs["verify"] is False
    assert factory.kwargs["trust_env"] is True
    assert exchange.result.status_code == 201
    assert exchange.result.body_excerpt == '{"created":true}'
    assert exchange.result.content_length == len(exchange.response_body)
    assert exchange.result.tls_summary == {
        "verified": False,
        "client_certificate_used": False,
    }


async def test_redirect_is_rechecked_against_run_scope() -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "http://outside.internal/admin"})

    runner = RunnerTargetHttpClient(client_factory=ClientFactory(handle))
    with pytest.raises(ScopeViolationError, match="outside authorized scope"):
        await runner.execute(
            launch(
                TargetHttpRequest(
                    execution_key="key",
                    method="GET",
                    url="http://target.internal/start",
                    follow_redirects=True,
                )
            )
        )
    assert calls == 1


async def test_authorized_redirect_follows_and_post_303_becomes_get() -> None:
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/submit":
            return httpx.Response(303, headers={"location": "/result"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="done")

    runner = RunnerTargetHttpClient(client_factory=ClientFactory(handle))
    exchange = await runner.execute(
        launch(
            TargetHttpRequest(
                execution_key="key",
                method="POST",
                url="http://target.internal/submit",
                body="payload",
                follow_redirects=True,
            )
        )
    )
    assert methods == ["POST", "GET"]
    assert exchange.result.final_url == "http://target.internal/result"
    assert exchange.result.redirect_chain == ["http://target.internal/result"]


async def test_response_limit_preserves_bounded_binary_artifact() -> None:
    runner = RunnerTargetHttpClient(
        client_factory=ClientFactory(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"x" * 4096,
            )
        )
    )
    exchange = await runner.execute(
        launch(
            TargetHttpRequest(
                execution_key="key",
                method="GET",
                url="http://target.internal/file",
                max_response_bytes=1024,
            )
        )
    )
    assert exchange.result.truncated is True
    assert exchange.result.body_excerpt is None
    assert exchange.response_body == b"x" * 1024


async def test_private_ip_is_allowed_only_through_target_scope() -> None:
    runner = RunnerTargetHttpClient(
        client_factory=ClientFactory(
            lambda _: httpx.Response(200, headers={"content-type": "text/plain"}, text="local")
        )
    )
    exchange = await runner.execute(
        launch(
            TargetHttpRequest(
                execution_key="key",
                method="GET",
                url="http://10.20.30.40/status",
            ),
            scope=Scope(cidrs=["10.20.30.0/24"]),
        )
    )
    assert exchange.result.body_excerpt == "local"


class CertificateResolver:
    def __init__(self, certificate: Path, key: Path) -> None:
        self.value = (str(certificate), str(key))

    async def resolve(self, reference: str) -> tuple[str, str]:
        assert reference == "client-cert-1"
        return self.value


async def test_client_certificate_reference_resolves_on_runner(tmp_path: Path) -> None:
    certificate = tmp_path / "client.pem"
    key = tmp_path / "client.key"
    certificate.write_text("fixture")
    key.write_text("fixture")
    factory = ClientFactory(lambda _: httpx.Response(204))
    runner = RunnerTargetHttpClient(
        certificate_resolver=CertificateResolver(certificate, key),
        client_factory=factory,
    )
    exchange = await runner.execute(
        launch(
            TargetHttpRequest(
                execution_key="key",
                method="GET",
                url="https://target.internal/",
                client_cert_ref="client-cert-1",
            )
        )
    )
    assert factory.kwargs["cert"] == (str(certificate), str(key))
    assert exchange.result.tls_summary["client_certificate_used"] is True


def test_binary_request_body_has_unambiguous_runner_transport() -> None:
    request = TargetHttpRequest(
        execution_key="key",
        method="PUT",
        url="http://target.internal/blob",
        body=b"\x00\xffbinary",
    )
    restored = TargetHttpRequest.from_runner_payload(request.runner_payload())
    assert restored == request


class BlockingClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    def build_request(self, method, url, **kwargs) -> httpx.Request:
        return httpx.Request(
            method,
            url,
            headers=kwargs.get("headers"),
            content=kwargs.get("content"),
        )

    async def send(self, request, *, stream, follow_redirects):
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking Target HTTP request unexpectedly resumed")

    async def aclose(self) -> None:
        self.closed.set()


class SlowCloseClient(BlockingClient):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        self.closed.set()


class FailingCloseClient:
    def __init__(self, *, failures: int | None = None) -> None:
        self.failures = failures
        self.close_attempts = 0
        self.closed = False

    def build_request(self, method, url, **kwargs) -> httpx.Request:
        return httpx.Request(
            method,
            url,
            headers=kwargs.get("headers"),
            content=kwargs.get("content"),
        )

    async def send(self, request, *, stream, follow_redirects):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/plain"},
            content=b"response",
        )

    async def aclose(self) -> None:
        self.close_attempts += 1
        if self.failures is None or self.close_attempts <= self.failures:
            raise OSError("socket close could not be confirmed")
        self.closed = True


class SlowByteStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def __aiter__(self):
        while True:
            await asyncio.sleep(0.01)
            yield b"x"

    async def aclose(self) -> None:
        self.closed.set()


async def test_local_stop_cancels_awaits_and_closes_active_http_client() -> None:
    client = BlockingClient()
    runner = RunnerTargetHttpClient(
        client_factory=lambda **_: client,
        stop_timeout_seconds=1,
    )
    execution = asyncio.create_task(
        runner.execute(
            launch(
                TargetHttpRequest(
                    execution_key="active-key",
                    method="GET",
                    url="https://target.internal/slow",
                )
            )
        )
    )
    await asyncio.wait_for(client.started.wait(), timeout=1)

    outcomes = await runner.stop_run(
        "run-1",
        node_id="local",
        tool_call_ids=["tool-call-1"],
    )

    assert len(outcomes) == 1
    assert outcomes[0].confirmed is True
    assert outcomes[0].reason == "target_http_local_task_terminated"
    assert client.closed.is_set()
    with pytest.raises(asyncio.CancelledError):
        await execution


async def test_local_stop_stays_unconfirmed_until_client_close_finishes() -> None:
    client = SlowCloseClient()
    runner = RunnerTargetHttpClient(
        client_factory=lambda **_: client,
        stop_timeout_seconds=0.01,
    )
    execution = asyncio.create_task(
        runner.execute(
            launch(
                TargetHttpRequest(
                    execution_key="slow-close-key",
                    method="GET",
                    url="https://target.internal/slow-close",
                )
            )
        )
    )
    await asyncio.wait_for(client.started.wait(), timeout=1)

    outcomes = await runner.stop_run(
        "run-1",
        node_id="local",
        tool_call_ids=["tool-call-1"],
    )

    assert client.close_started.is_set()
    assert client.closed.is_set() is False
    assert outcomes[0].confirmed is False
    assert outcomes[0].reason == "target_http_local_task_stop_unconfirmed"

    client.release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert client.closed.is_set()


async def test_total_deadline_stops_a_response_that_keeps_slowly_streaming() -> None:
    stream = SlowByteStream()
    factory = ClientFactory(lambda _: httpx.Response(200, stream=stream))
    runner = RunnerTargetHttpClient(client_factory=factory)
    loop = asyncio.get_running_loop()
    started = loop.time()

    with pytest.raises(TimeoutError):
        await runner.execute(
            launch(
                TargetHttpRequest(
                    execution_key="slow-stream-key",
                    method="GET",
                    url="https://target.internal/slow-stream",
                    timeout_seconds=0.05,
                )
            )
        )

    assert loop.time() - started < 0.2
    assert stream.closed.is_set()
    assert factory.client is not None and factory.client.is_closed


async def test_total_deadline_does_not_return_before_http_client_close() -> None:
    client = SlowCloseClient()
    runner = RunnerTargetHttpClient(client_factory=lambda **_: client)
    execution = asyncio.create_task(
        runner.execute(
            launch(
                TargetHttpRequest(
                    execution_key="deadline-close-key",
                    method="GET",
                    url="https://target.internal/deadline-close",
                    timeout_seconds=0.01,
                )
            )
        )
    )
    await asyncio.wait_for(client.started.wait(), timeout=1)
    await asyncio.wait_for(client.close_started.wait(), timeout=1)

    assert execution.done() is False
    client.release_close.set()
    with pytest.raises(TimeoutError):
        await execution
    assert client.closed.is_set()


async def test_client_close_failure_surfaces_unconfirmed_execution_outcome() -> None:
    client = FailingCloseClient()
    runner = RunnerTargetHttpClient(
        client_factory=lambda **_: client,
        stop_timeout_seconds=0.05,
    )

    with pytest.raises(TargetHttpRunnerExecutionUncertainError) as caught:
        await runner.execute(
            launch(
                TargetHttpRequest(
                    execution_key="close-failure-key",
                    method="GET",
                    url="https://target.internal/close-failure",
                )
            )
        )

    assert caught.value.stop_outcome.confirmed is False
    assert "socket close could not be confirmed" in (caught.value.stop_outcome.reason or "")

    outcomes = await runner.stop_run(
        "run-1",
        node_id="local",
        tool_call_ids=["tool-call-1"],
    )
    assert outcomes[0].confirmed is False
    assert "socket close could not be confirmed" in (outcomes[0].reason or "")
    assert client.close_attempts == 2


async def test_stop_run_can_retry_and_confirm_a_transient_client_close_failure() -> None:
    client = FailingCloseClient(failures=1)
    runner = RunnerTargetHttpClient(
        client_factory=lambda **_: client,
        stop_timeout_seconds=0.05,
    )

    with pytest.raises(TargetHttpRunnerExecutionUncertainError):
        await runner.execute(
            launch(
                TargetHttpRequest(
                    execution_key="close-retry-key",
                    method="GET",
                    url="https://target.internal/close-retry",
                )
            )
        )

    outcomes = await runner.stop_run(
        "run-1",
        node_id="local",
        tool_call_ids=["tool-call-1"],
    )
    assert outcomes[0].confirmed is True
    assert outcomes[0].reason == "target_http_local_task_terminated"
    assert client.close_attempts == 2
    assert client.closed is True


async def test_effect_guard_blocks_before_any_network_send_and_closes_client() -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async def blocked() -> None:
        raise RuntimeError("run stopped")

    factory = ClientFactory(handle)
    runner = RunnerTargetHttpClient(client_factory=factory)
    with pytest.raises(RuntimeError, match="run stopped"):
        await runner.execute(
            launch(
                TargetHttpRequest(
                    execution_key="blocked-key",
                    method="GET",
                    url="https://target.internal/blocked",
                )
            ),
            effect_guard=blocked,
        )

    assert calls == 0
    assert factory.client is not None and factory.client.is_closed


async def test_effect_guard_is_rechecked_before_each_redirect_request() -> None:
    sends = 0
    guard_checks = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(302, headers={"location": "/next"})

    async def guard() -> None:
        nonlocal guard_checks
        guard_checks += 1
        if guard_checks == 2:
            raise RuntimeError("run stopped between redirects")

    runner = RunnerTargetHttpClient(client_factory=ClientFactory(handle))
    with pytest.raises(RuntimeError, match="stopped between redirects"):
        await runner.execute(
            launch(
                TargetHttpRequest(
                    execution_key="redirect-stop-key",
                    method="GET",
                    url="https://target.internal/start",
                    follow_redirects=True,
                )
            ),
            effect_guard=guard,
        )

    assert guard_checks == 2
    assert sends == 1
