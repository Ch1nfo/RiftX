"""Unified HTTP capture contracts shared by browser and Burp connectors."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from enum import StrEnum

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from riftx.domain.base import DomainModel, new_id, utc_now

_MAX_BODY_BYTES = 25_000_000


class ConnectorSource(StrEnum):
    BROWSER = "browser"
    BURP = "burp"


class HttpHeader(DomainModel):
    name: str = Field(min_length=1, max_length=1024)
    value: str = Field(max_length=64_000)

    @field_validator("name", "value")
    @classmethod
    def reject_line_breaks(cls, value: str) -> str:
        if "\r" in value or "\n" in value or "\x00" in value:
            raise ValueError("HTTP headers cannot contain line breaks or NUL bytes")
        return value


class ConnectorHttpCapture(DomainModel):
    capture_id: str = Field(min_length=1, max_length=255)
    source: ConnectorSource
    method: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1, max_length=8192)
    http_version: str = Field(default="HTTP/1.1", min_length=1, max_length=32)
    request_headers: list[HttpHeader] = Field(default_factory=list, max_length=1000)
    request_body_base64: str | None = None
    raw_request_base64: str | None = None
    response_status: int | None = Field(default=None, ge=100, le=599)
    response_reason: str | None = Field(default=None, max_length=1000)
    response_headers: list[HttpHeader] = Field(default_factory=list, max_length=1000)
    response_body_base64: str | None = None
    raw_response_base64: str | None = None
    observed_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_and_validate(self) -> ConnectorHttpCapture:
        method = self.method.strip().upper()
        if not method.isascii() or not method.replace("-", "").isalpha():
            raise ValueError("HTTP method is invalid")
        object.__setattr__(self, "method", method)
        _ = self.request_body
        _ = self.response_body
        _ = self.raw_request
        _ = self.raw_response
        return self

    @property
    def request_body(self) -> bytes:
        return _decode_body(self.request_body_base64, "request")

    @property
    def response_body(self) -> bytes:
        return _decode_body(self.response_body_base64, "response")

    @property
    def raw_request(self) -> bytes | None:
        return _decode_optional_body(self.raw_request_base64, "raw request")

    @property
    def raw_response(self) -> bytes | None:
        return _decode_optional_body(self.raw_response_base64, "raw response")

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"capture_id", "observed_at"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def request_bytes(self) -> bytes:
        if self.raw_request is not None:
            return self.raw_request
        target = self.url
        lines = [f"{self.method} {target} {self.http_version}"]
        lines.extend(f"{item.name}: {item.value}" for item in self.request_headers)
        return "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + self.request_body

    @property
    def response_bytes(self) -> bytes | None:
        if self.raw_response is not None:
            return self.raw_response
        if self.response_status is None:
            return None
        status = f"{self.http_version} {self.response_status} {self.response_reason or ''}".rstrip()
        lines = [status]
        lines.extend(f"{item.name}: {item.value}" for item in self.response_headers)
        return "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + self.response_body

    def safe_summary(self) -> dict[str, JsonValue]:
        return {
            "capture_id": self.capture_id,
            "source": self.source.value,
            "method": self.method,
            "url": self.url,
            "http_version": self.http_version,
            "request_header_names": [item.name for item in self.request_headers],
            "request_body_bytes": len(self.request_body),
            "raw_request_bytes": len(self.raw_request or b""),
            "response_status": self.response_status,
            "response_header_names": [item.name for item in self.response_headers],
            "response_body_bytes": len(self.response_body),
            "raw_response_bytes": len(self.raw_response or b""),
            "observed_at": self.observed_at.isoformat(),
        }


class ConnectorSubmission(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    capture_id: str = Field(min_length=1, max_length=255)
    source: ConnectorSource
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_artifact_id: str = Field(min_length=1)
    response_artifact_id: str | None = None
    manifest_artifact_id: str = Field(min_length=1)
    summary: dict[str, JsonValue]
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ConnectorReceipt(DomainModel):
    submission: ConnectorSubmission
    created_run: bool = False
    webui_path: str
    events_path: str
    cancel_path: str


def encode_body(content: bytes | None) -> str | None:
    return base64.b64encode(content).decode("ascii") if content is not None else None


def _decode_optional_body(value: str | None, label: str) -> bytes | None:
    if value is None:
        return None
    return _decode_body(value, label)


def _decode_body(value: str | None, label: str) -> bytes:
    if value is None:
        return b""
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} body is not valid base64") from exc
    if len(content) > _MAX_BODY_BYTES:
        raise ValueError(f"{label} body exceeds {_MAX_BODY_BYTES} bytes")
    return content
