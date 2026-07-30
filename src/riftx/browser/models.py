"""Runner-neutral contracts for durable browser operations."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from riftx.domain import (
    BrowserAction,
    BrowserMode,
    BrowserObservation,
    BrowserPage,
    BrowserSession,
    BrowserTakeoverSummary,
    Scope,
)
from riftx.domain.base import DomainModel


class BrowserOperation(StrEnum):
    OPEN = "open"
    OBSERVE = "observe"
    ACT = "act"
    TAKEOVER = "takeover"
    RELEASE = "release"
    CLOSE = "close"


class BrowserOpenCommand(DomainModel):
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    mode: BrowserMode = BrowserMode.MANAGED_EPHEMERAL
    url: str = Field(min_length=1)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    cdp_endpoint: str | None = None
    scope: Scope
    headless: bool = False
    include_screenshot: bool = True

    @model_validator(mode="after")
    def validate_mode_options(self) -> BrowserOpenCommand:
        if self.mode is BrowserMode.MANAGED_PERSISTENT and not self.profile_id:
            raise ValueError("persistent browser sessions require profile_id")
        if self.mode is BrowserMode.ATTACHED_CDP and not self.cdp_endpoint:
            raise ValueError("attached browser sessions require cdp_endpoint")
        if self.mode is not BrowserMode.MANAGED_PERSISTENT and self.profile_id is not None:
            raise ValueError("profile_id is only valid for persistent browser sessions")
        if self.mode is not BrowserMode.ATTACHED_CDP and self.cdp_endpoint is not None:
            raise ValueError("cdp_endpoint is only valid for attached browser sessions")
        return self


class BrowserObserveCommand(DomainModel):
    session_id: str = Field(min_length=1)
    page_id: str | None = Field(default=None, min_length=1)
    include_screenshot: bool = False
    include_network: bool = True


class BrowserActCommand(DomainModel):
    session_id: str = Field(min_length=1)
    action: BrowserAction
    include_screenshot: bool = True


class BrowserSessionCommand(DomainModel):
    session_id: str = Field(min_length=1)


class BrowserAttachment(DomainModel):
    kind: str = Field(pattern=r"^(screenshot|download|dom)$")
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class BrowserRuntimeResult(DomainModel):
    session: BrowserSession
    pages: list[BrowserPage] = Field(default_factory=list)
    observation: BrowserObservation | None = None
    action: BrowserAction | None = None
    takeover_summary: BrowserTakeoverSummary | None = None
    attachment: BrowserAttachment | None = None


class BrowserRuntimeExchange(DomainModel):
    result: BrowserRuntimeResult
    attachment_content: bytes = b""

    @model_validator(mode="after")
    def validate_attachment(self) -> BrowserRuntimeExchange:
        if self.attachment_content and self.result.attachment is None:
            raise ValueError("browser attachment bytes require attachment metadata")
        if self.result.attachment is not None and not self.attachment_content:
            raise ValueError("browser attachment metadata requires attachment bytes")
        return self
