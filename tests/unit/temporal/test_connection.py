from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from temporalio.client import Client, TLSConfig

from riftx.api.runtime import APISettings, _create_temporal_connector
from riftx.temporal.connection import (
    TemporalConnectionError,
    TemporalConnectionSettings,
    connect_temporal,
)


@pytest.mark.asyncio
async def test_default_connection_keeps_local_tls_and_auth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    client = object()

    async def fake_connect(target: str, **kwargs: object) -> object:
        calls.append((target, kwargs))
        return client

    monkeypatch.setattr(Client, "connect", fake_connect)

    connected = await connect_temporal(
        TemporalConnectionSettings(target="127.0.0.1:7233", namespace="default")
    )

    assert connected is client
    assert calls == [
        (
            "127.0.0.1:7233",
            {"namespace": "default", "tls": False, "api_key": None},
        )
    ]


@pytest.mark.asyncio
async def test_shared_connection_loads_exact_tls_bytes_and_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_ca = tmp_path / "root-ca.pem"
    client_cert = tmp_path / "client-cert.pem"
    private_key = tmp_path / "client-key.pem"
    root_ca.write_bytes(b"root-ca-bytes\n")
    client_cert.write_bytes(b"client-cert-bytes\n")
    private_key.write_bytes(b"client-private-key-bytes\n")
    captured: dict[str, Any] = {}
    client = object()

    async def fake_connect(target: str, **kwargs: object) -> object:
        captured.update(target=target, **kwargs)
        return client

    monkeypatch.setattr(Client, "connect", fake_connect)

    connected = await connect_temporal(
        TemporalConnectionSettings(
            target="temporal.example:7233",
            namespace="production",
            tls_enabled=True,
            tls_server_root_ca_path=root_ca,
            tls_server_name="temporal.service.internal",
            tls_client_cert_path=client_cert,
            tls_client_private_key_path=private_key,
            api_key=SecretStr("temporal-api-secret"),
        )
    )

    assert connected is client
    assert captured["target"] == "temporal.example:7233"
    assert captured["namespace"] == "production"
    assert captured["api_key"] == "temporal-api-secret"
    tls = captured["tls"]
    assert isinstance(tls, TLSConfig)
    assert tls.server_root_ca_cert == b"root-ca-bytes\n"
    assert tls.domain == "temporal.service.internal"
    assert tls.client_cert == b"client-cert-bytes\n"
    assert tls.client_private_key == b"client-private-key-bytes\n"


@pytest.mark.asyncio
async def test_control_plane_connector_uses_shared_secure_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    client = object()

    async def fake_connect(target: str, **kwargs: object) -> object:
        captured.update(target=target, **kwargs)
        return client

    monkeypatch.setattr(Client, "connect", fake_connect)
    connector = _create_temporal_connector(
        APISettings(
            temporal_address="managed.temporal.example:7233",
            temporal_namespace="managed-namespace",
            temporal_tls_enabled=True,
            temporal_api_key=SecretStr("control-plane-temporal-key"),
        )
    )

    assert await connector() is client
    assert captured == {
        "target": "managed.temporal.example:7233",
        "namespace": "managed-namespace",
        "tls": True,
        "api_key": "control-plane-temporal-key",
    }


@pytest.mark.asyncio
async def test_connection_failure_does_not_disclose_tls_or_api_key_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key_bytes = b"private-key-content-must-not-leak"
    client_cert_bytes = b"client-cert-content-must-not-leak"
    client_cert = tmp_path / "client-cert.pem"
    private_key = tmp_path / "private-key.pem"
    client_cert.write_bytes(client_cert_bytes)
    private_key.write_bytes(private_key_bytes)
    api_key = "api-key-content-must-not-leak"

    async def fake_connect(target: str, **kwargs: object) -> object:
        raise RuntimeError(f"bad credentials {target} {kwargs!r}")

    monkeypatch.setattr(Client, "connect", fake_connect)
    settings = TemporalConnectionSettings(
        target="managed.temporal.example:7233",
        namespace="production",
        tls_enabled=True,
        tls_client_cert_path=client_cert,
        tls_client_private_key_path=private_key,
        api_key=SecretStr(api_key),
    )

    with pytest.raises(TemporalConnectionError) as captured:
        await connect_temporal(settings)

    rendered = f"{captured.value!s} {captured.value!r}"
    assert api_key not in rendered
    assert private_key_bytes.decode() not in rendered
    assert client_cert_bytes.decode() not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_unreadable_tls_file_error_does_not_disclose_configured_path(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "sensitive-certificate-location.pem"

    with pytest.raises(TemporalConnectionError) as captured:
        await connect_temporal(
            TemporalConnectionSettings(
                tls_enabled=True,
                tls_server_root_ca_path=missing_path,
            )
        )

    assert str(missing_path) not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_secure_connection_settings_require_tls_and_complete_mtls_pair() -> None:
    with pytest.raises(ValueError, match="API key authentication requires TLS"):
        TemporalConnectionSettings(api_key=SecretStr("not-rendered"))
    with pytest.raises(ValueError, match="certificate and private key"):
        TemporalConnectionSettings(
            tls_enabled=True,
            tls_client_cert_path=Path("client-cert.pem"),
        )
    with pytest.raises(ValueError, match="settings require TLS"):
        TemporalConnectionSettings(tls_server_name="temporal.internal")
