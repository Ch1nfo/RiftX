"""Shared, secret-safe Temporal client connection assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr
from temporalio.client import Client, TLSConfig

from riftx.config import TemporalConfig


class TemporalConnectionError(ConnectionError):
    """A sanitized Temporal connection or credential-file failure."""


@dataclass(frozen=True, slots=True)
class TemporalConnectionSettings:
    target: str = "127.0.0.1:7233"
    namespace: str = "default"
    tls_enabled: bool = False
    tls_server_root_ca_path: Path | None = field(default=None, repr=False)
    tls_server_name: str | None = field(default=None, repr=False)
    tls_client_cert_path: Path | None = field(default=None, repr=False)
    tls_client_private_key_path: Path | None = field(default=None, repr=False)
    api_key: SecretStr | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.api_key is not None and not self.api_key.get_secret_value().strip():
            raise ValueError("Temporal API key must not be empty")
        if (self.tls_client_cert_path is None) != (self.tls_client_private_key_path is None):
            raise ValueError(
                "Temporal client certificate and private key must be configured together"
            )
        if not self.tls_enabled and any(
            value is not None
            for value in (
                self.tls_server_root_ca_path,
                self.tls_server_name,
                self.tls_client_cert_path,
                self.tls_client_private_key_path,
            )
        ):
            raise ValueError("Temporal TLS certificate and server-name settings require TLS")
        if not self.tls_enabled and self.api_key is not None:
            raise ValueError("Temporal API key authentication requires TLS")

    @classmethod
    def from_config(cls, config: TemporalConfig) -> TemporalConnectionSettings:
        return cls(
            target=config.target,
            namespace=config.namespace,
            tls_enabled=config.tls_enabled,
            tls_server_root_ca_path=config.tls_server_root_ca_path,
            tls_server_name=config.tls_server_name,
            tls_client_cert_path=config.tls_client_cert_path,
            tls_client_private_key_path=config.tls_client_private_key_path,
            api_key=config.api_key,
        )


async def connect_temporal(settings: TemporalConnectionSettings) -> Client:
    """Connect using the same validated TLS/auth assembly in every process."""

    configuration_failed = False
    try:
        tls = _build_tls_config(settings)
        api_key = settings.api_key.get_secret_value() if settings.api_key is not None else None
    except TemporalConnectionError:
        raise
    except Exception:
        configuration_failed = True
    if configuration_failed:
        raise TemporalConnectionError("Temporal TLS configuration failed")
    try:
        return await Client.connect(
            settings.target,
            namespace=settings.namespace,
            tls=tls,
            api_key=api_key,
        )
    except Exception:
        # SDK/transport messages are not under RiftX's control and may echo
        # authentication or TLS inputs. Keep both user-facing details and
        # unhandled Worker tracebacks free of those values.
        pass
    raise TemporalConnectionError("Temporal client connection failed")


def _build_tls_config(settings: TemporalConnectionSettings) -> bool | TLSConfig:
    if not settings.tls_enabled:
        return False
    if all(
        value is None
        for value in (
            settings.tls_server_root_ca_path,
            settings.tls_server_name,
            settings.tls_client_cert_path,
            settings.tls_client_private_key_path,
        )
    ):
        # True delegates trust verification to the operating-system roots.
        return True
    return TLSConfig(
        server_root_ca_cert=_read_tls_file(
            settings.tls_server_root_ca_path,
            "server root CA",
        ),
        domain=settings.tls_server_name,
        client_cert=_read_tls_file(settings.tls_client_cert_path, "client certificate"),
        client_private_key=_read_tls_file(
            settings.tls_client_private_key_path,
            "client private key",
        ),
    )


def _read_tls_file(path: Path | None, label: str) -> bytes | None:
    if path is None:
        return None
    read_failed = False
    try:
        content = path.expanduser().read_bytes()
    except OSError:
        read_failed = True
    if read_failed:
        raise TemporalConnectionError(f"Temporal TLS {label} file could not be read")
    return content
