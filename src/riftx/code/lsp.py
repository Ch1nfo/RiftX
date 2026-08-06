"""Content-only transport contract for trusted LSP gateways."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Literal, Protocol, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_REQUEST_BYTES = 12 * 1024 * 1024
_BACKEND_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


class ControlledLSPError(RuntimeError):
    """A controlled gateway was unavailable or returned an invalid response."""


class ControlledLSPContract(BaseModel):
    """Fixed contract required before RiftX accepts LSP-derived results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["riftx.controlled-lsp-contract/v1"] = (
        "riftx.controlled-lsp-contract/v1"
    )
    content_only: Literal[True] = True
    provided_files_only: Literal[True] = True
    project_configuration: Literal["disabled"] = "disabled"
    plugins_and_commands: Literal["disabled"] = "disabled"
    build_install_test_hooks: Literal["disabled"] = "disabled"
    network_access: Literal["disabled"] = "disabled"


class ControlledLSPFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=4096)
    language: str = Field(min_length=1, max_length=32)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(max_length=512 * 1024)


class ControlledLSPRequest(BaseModel):
    """Bounded source content; intentionally contains no Run ID or absolute root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["riftx.controlled-lsp-request/v1"] = (
        "riftx.controlled-lsp-request/v1"
    )
    operation: Literal[
        "symbol_search",
        "find_references",
        "call_hierarchy",
        "diagnostics",
    ]
    source: Literal["workspace", "audit_snapshot"]
    source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[ControlledLSPFile] = Field(max_length=20_000)
    query: str | None = Field(default=None, min_length=1, max_length=1024)
    symbol: str | None = Field(default=None, min_length=1, max_length=512)
    direction: Literal["incoming", "outgoing", "both"] | None = None
    case_sensitive: bool = False
    include_declarations: bool = True
    max_results: int = Field(ge=1, le=200)

    @model_validator(mode="after")
    def validate_operation_arguments(self) -> Self:
        if self.operation == "symbol_search":
            if self.query is None or self.symbol is not None or self.direction is not None:
                raise ValueError("symbol_search requires only query")
        elif self.operation == "find_references":
            if self.symbol is None or self.query is not None or self.direction is not None:
                raise ValueError("find_references requires only symbol")
        elif self.operation == "call_hierarchy":
            if self.symbol is None or self.direction is None or self.query is not None:
                raise ValueError("call_hierarchy requires symbol and direction")
        elif any(value is not None for value in (self.query, self.symbol, self.direction)):
            raise ValueError("diagnostics does not accept symbol arguments")
        return self

    def request_digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


class ControlledLSPResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["riftx.controlled-lsp-response/v1"] = (
        "riftx.controlled-lsp-response/v1"
    )
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend_id: str = Field(min_length=1, max_length=64)
    backend_version: str = Field(min_length=1, max_length=128)
    contract: ControlledLSPContract
    status: Literal["ok", "unsupported"]
    result: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_backend_identity(self) -> Self:
        if not _BACKEND_ID_PATTERN.fullmatch(self.backend_id):
            raise ValueError("invalid controlled LSP backend ID")
        if self.backend_version != self.backend_version.strip():
            raise ValueError("invalid controlled LSP backend version")
        if self.status == "unsupported" and self.result:
            raise ValueError("unsupported responses must not include result data")
        return self


class ControlledLSPBackend(Protocol):
    backend_id: str
    backend_version: str

    async def analyze(self, request: ControlledLSPRequest) -> ControlledLSPResponse: ...

    async def aclose(self) -> None: ...


class ControlledLSPGatewayClient:
    """Authenticate to one operator-managed gateway through a Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        backend_id: str,
        backend_version: str,
        token: str,
        timeout_seconds: float = 15,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not socket_path.is_absolute() or ".." in socket_path.parts:
            raise ValueError("controlled LSP socket path must be absolute and normalized")
        if not _BACKEND_ID_PATTERN.fullmatch(backend_id):
            raise ValueError("invalid controlled LSP backend ID")
        if not backend_version or backend_version != backend_version.strip():
            raise ValueError("invalid controlled LSP backend version")
        if (
            len(token) < 32
            or len(token) > 4096
            or token != token.strip()
            or not token.isascii()
            or not token.isprintable()
        ):
            raise ValueError("controlled LSP token must be a strong process secret")
        self.backend_id = backend_id
        self.backend_version = backend_version
        self._socket_path = socket_path
        self._authorization = f"Bearer {token}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="http://riftx-controlled-lsp",
            transport=httpx.AsyncHTTPTransport(uds=os.fspath(socket_path)),
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )

    async def analyze(self, request: ControlledLSPRequest) -> ControlledLSPResponse:
        if self._owns_client:
            self._validate_socket()
        body = request.model_dump_json().encode("utf-8")
        if len(body) > _MAX_REQUEST_BYTES:
            raise ControlledLSPError("controlled LSP request exceeded limit")
        try:
            async with self._client.stream(
                "POST",
                "/v1/analyze",
                headers={
                    "Authorization": self._authorization,
                    "Content-Type": "application/json",
                },
                content=body,
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise ControlledLSPError("controlled LSP response exceeded limit")
                    chunks.append(chunk)
        except ControlledLSPError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise ControlledLSPError("controlled LSP gateway unavailable") from exc
        try:
            payload = json.loads(b"".join(chunks))
            return ControlledLSPResponse.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ControlledLSPError("controlled LSP gateway response is invalid") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _validate_socket(self) -> None:
        try:
            parent = os.lstat(self._socket_path.parent)
            metadata = os.lstat(self._socket_path)
        except OSError as exc:
            raise ControlledLSPError("controlled LSP socket is unavailable") from exc
        expected_uid = os.geteuid()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != expected_uid
            or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ControlledLSPError("controlled LSP socket is not trusted")


__all__ = [
    "ControlledLSPBackend",
    "ControlledLSPContract",
    "ControlledLSPError",
    "ControlledLSPFile",
    "ControlledLSPGatewayClient",
    "ControlledLSPRequest",
    "ControlledLSPResponse",
]
