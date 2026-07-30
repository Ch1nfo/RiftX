from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from riftx.domain import Scope
from riftx.runner.target_http import RunnerTargetHttpClient
from riftx.scope import ScopeViolationError
from riftx.target_http.models import TargetHttpRequest, TargetHttpRunnerRequest


class ClientFactory:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = dict(kwargs)
        kwargs.pop("proxy")
        kwargs.pop("cert")
        kwargs.pop("trust_env")
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), **kwargs)


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
                url="https://target.internal/api",
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
    assert observed.url == "https://target.internal/api?page=1"
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
