from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from riftx.code import (
    ControlledLSPContract,
    ControlledLSPError,
    ControlledLSPFile,
    ControlledLSPGatewayClient,
    ControlledLSPRequest,
)


def _request() -> ControlledLSPRequest:
    return ControlledLSPRequest(
        operation="symbol_search",
        source="workspace",
        source_digest=None,
        input_digest="a" * 64,
        files=[
            ControlledLSPFile(
                path="src/app.py",
                language="python",
                content_digest="b" * 64,
                content="def handle():\n    return 1\n",
            )
        ],
        query="handle",
        max_results=10,
    )


async def test_gateway_client_authenticates_and_validates_bounded_response() -> None:
    request = _request()

    async def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url.path == "/v1/analyze"
        assert incoming.headers["authorization"] == f"Bearer {'t' * 32}"
        payload = json.loads(incoming.content)
        assert payload["files"][0]["path"] == "src/app.py"
        assert "/Users/" not in incoming.content.decode()
        return httpx.Response(
            200,
            json={
                "schema_version": "riftx.controlled-lsp-response/v1",
                "request_digest": request.request_digest(),
                "backend_id": "trusted-lsp",
                "backend_version": "1.0.0",
                "contract": ControlledLSPContract().model_dump(mode="json"),
                "status": "unsupported",
                "result": {},
            },
        )

    http = httpx.AsyncClient(
        base_url="http://riftx-controlled-lsp",
        transport=httpx.MockTransport(handler),
    )
    gateway = ControlledLSPGatewayClient(
        Path("/tmp/riftx-lsp.sock"),
        backend_id="trusted-lsp",
        backend_version="1.0.0",
        token="t" * 32,
        client=http,
    )

    response = await gateway.analyze(request)

    assert response.status == "unsupported"
    await http.aclose()


async def test_gateway_client_rejects_oversized_response() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))

    http = httpx.AsyncClient(
        base_url="http://riftx-controlled-lsp",
        transport=httpx.MockTransport(handler),
    )
    gateway = ControlledLSPGatewayClient(
        Path("/tmp/riftx-lsp.sock"),
        backend_id="trusted-lsp",
        backend_version="1.0.0",
        token="t" * 32,
        client=http,
    )

    with pytest.raises(ControlledLSPError, match="exceeded limit"):
        await gateway.analyze(_request())
    await http.aclose()


def test_controlled_lsp_contract_cannot_enable_project_execution() -> None:
    with pytest.raises(ValidationError):
        ControlledLSPContract.model_validate(
            {
                **ControlledLSPContract().model_dump(mode="json"),
                "build_install_test_hooks": "enabled",
            }
        )


def test_gateway_client_rejects_weak_token() -> None:
    with pytest.raises(ValueError, match="strong process secret"):
        ControlledLSPGatewayClient(
            Path("/tmp/riftx-lsp.sock"),
            backend_id="trusted-lsp",
            backend_version="1.0.0",
            token="weak-token",
        )


async def test_gateway_rejects_socket_in_writable_parent() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="rx-lsp-") as directory:
        unsafe = Path(directory)
        os.chmod(unsafe, 0o777)
        socket_path = unsafe / "lsp.sock"
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        )
        gateway = ControlledLSPGatewayClient(
            socket_path,
            backend_id="trusted-lsp",
            backend_version="1.0.0",
            token="t" * 32,
            client=http,
        )
        try:
            with pytest.raises(ControlledLSPError, match="not trusted"):
                gateway._validate_socket()
        finally:
            server.close()
            await http.aclose()
