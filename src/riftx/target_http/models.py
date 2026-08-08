"""Contracts for scope-authorized HTTP exchanges on Runner host networks."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from pydantic import Field, JsonValue, field_validator, model_validator

from riftx.domain import Scope
from riftx.domain.base import DomainModel, new_id


class TargetHttpRequest(DomainModel):
    execution_key: str = Field(min_length=1, max_length=255)
    method: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    header_secret_refs: dict[str, str] = Field(default_factory=dict, max_length=100)
    query: dict[str, str] = Field(default_factory=dict)
    body: str | bytes | None = None
    body_secret_ref: str | None = Field(default=None, min_length=1, max_length=255)
    json_body: JsonValue | None = None
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_secret_refs: dict[str, str] = Field(default_factory=dict, max_length=100)
    proxy: str | None = None
    verify_tls: bool = True
    client_cert_ref: str | None = None
    follow_redirects: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_response_bytes: int = Field(default=10_000_000, ge=1, le=100_000_000)
    save_request: bool = True
    save_response: bool = True

    @field_validator("body_secret_ref")
    @classmethod
    def validate_body_secret_ref(cls, value: str | None) -> str | None:
        return _credential_reference(value) if value is not None else None

    @field_validator("header_secret_refs", "cookie_secret_refs")
    @classmethod
    def validate_secret_refs(cls, value: dict[str, str]) -> dict[str, str]:
        return {name: _credential_reference(reference) for name, reference in value.items()}

    @model_validator(mode="after")
    def validate_body(self) -> TargetHttpRequest:
        method = self.method.strip().upper()
        if not method or not method.isascii() or not method.replace("-", "").isalpha():
            raise ValueError("Target HTTP method is invalid")
        if sum(
            value is not None
            for value in (self.body, self.json_body, self.body_secret_ref)
        ) > 1:
            raise ValueError(
                "Target HTTP accepts body, json_body, or body_secret_ref, not more than one"
            )
        if self.json_body is not None and not isinstance(self.json_body, (dict, list)):
            raise ValueError("Target HTTP json_body must be an object or array")
        public_headers = {name.lower() for name in self.headers}
        secret_headers = {name.lower() for name in self.header_secret_refs}
        if public_headers & secret_headers:
            raise ValueError("Target HTTP Header cannot have both public and secret values")
        if set(self.cookies) & set(self.cookie_secret_refs):
            raise ValueError("Target HTTP Cookie cannot have both public and secret values")
        object.__setattr__(self, "method", method)
        return self

    @property
    def credential_references(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.header_secret_refs.values(),
                    *((self.body_secret_ref,) if self.body_secret_ref is not None else ()),
                    *self.cookie_secret_refs.values(),
                )
            )
        )

    def runner_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude={"body"})
        if isinstance(self.body, bytes):
            payload["body_base64"] = base64.b64encode(self.body).decode("ascii")
            payload["body_text"] = None
        else:
            payload["body_base64"] = None
            payload["body_text"] = self.body
        return payload

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.runner_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_runner_payload(cls, payload: dict[str, Any]) -> TargetHttpRequest:
        values = dict(payload)
        encoded = values.pop("body_base64", None)
        text = values.pop("body_text", None)
        if encoded is not None:
            values["body"] = base64.b64decode(str(encoded), validate=True)
        else:
            values["body"] = text
        return cls.model_validate(values)


class TargetHttpSubmission(DomainModel):
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    request: TargetHttpRequest


class TargetHttpResult(DomainModel):
    request_id: str = Field(default_factory=new_id)
    execution_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status_code: int = Field(ge=100, le=599)
    reason_phrase: str | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    elapsed_ms: int = Field(ge=0)
    content_type: str | None = None
    content_length: int | None = Field(default=None, ge=0)
    body_excerpt: str | None = None
    request_artifact_id: str | None = None
    response_artifact_id: str | None = None
    redirect_location: str | None = None
    tls_summary: dict[str, JsonValue] | None = None
    final_url: str
    redirect_chain: list[str] = Field(default_factory=list)
    truncated: bool = False


class TargetHttpExchange(DomainModel):
    result: TargetHttpResult
    response_body: bytes


class TargetHttpRunnerRequest(DomainModel):
    run_id: str
    session_id: str
    tool_call_id: str
    node_id: str
    scope: Scope
    request: TargetHttpRequest


class TargetHttpRunnerStopOutcome(DomainModel):
    """Per-intent proof returned by a Target HTTP Runner stop boundary."""

    tool_call_id: str = Field(min_length=1)
    confirmed: bool
    reason: str | None = None


def _credential_reference(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 255
        or any(ord(character) < 33 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError("Target HTTP credential reference is invalid")
    return normalized
