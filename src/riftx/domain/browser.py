"""Durable browser sessions, observations, actions, and takeover summaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from .base import DomainModel, new_id, utc_now
from .enums import (
    BrowserActionStatus,
    BrowserActionType,
    BrowserMode,
    BrowserOwner,
    BrowserPageStatus,
    BrowserSessionStatus,
)
from .errors import InvalidStateTransitionError

_SESSION_TRANSITIONS: Mapping[BrowserSessionStatus, frozenset[BrowserSessionStatus]] = {
    BrowserSessionStatus.CREATED: frozenset(
        {BrowserSessionStatus.STARTING, BrowserSessionStatus.CLOSED, BrowserSessionStatus.LOST}
    ),
    BrowserSessionStatus.STARTING: frozenset(
        {BrowserSessionStatus.ACTIVE, BrowserSessionStatus.CLOSED, BrowserSessionStatus.LOST}
    ),
    BrowserSessionStatus.ACTIVE: frozenset(
        {BrowserSessionStatus.CLOSED, BrowserSessionStatus.LOST}
    ),
    BrowserSessionStatus.CLOSED: frozenset(),
    BrowserSessionStatus.LOST: frozenset(),
}


class BrowserSession(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1, max_length=64)
    mode: BrowserMode
    status: BrowserSessionStatus = BrowserSessionStatus.CREATED
    owner: BrowserOwner = BrowserOwner.AGENT
    browser_type: str = Field(default="chromium", min_length=1, max_length=64)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    profile_path: str | None = Field(default=None, max_length=4096)
    cdp_endpoint: str | None = Field(default=None, max_length=4096)
    current_page_id: str | None = None
    page_ids: list[str] = Field(default_factory=list)
    takeover_started_at: AwareDatetime | None = None
    takeover_observation_version: int | None = Field(default=None, ge=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    closed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_mode_options(self) -> BrowserSession:
        if self.mode is BrowserMode.MANAGED_PERSISTENT and not self.profile_id:
            raise ValueError("persistent browser sessions require profile_id")
        if self.mode is BrowserMode.ATTACHED_CDP and not self.cdp_endpoint:
            raise ValueError("attached browser sessions require cdp_endpoint")
        if self.mode is not BrowserMode.MANAGED_PERSISTENT and self.profile_id is not None:
            raise ValueError("profile_id is only valid for persistent browser sessions")
        if self.mode is not BrowserMode.ATTACHED_CDP and self.cdp_endpoint is not None:
            raise ValueError("cdp_endpoint is only valid for attached browser sessions")
        return self

    def transition_to(
        self,
        target: BrowserSessionStatus,
        *,
        at: AwareDatetime | None = None,
    ) -> None:
        if target not in _SESSION_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError("BrowserSession", self.status, target)
        self.status = target
        if target in {BrowserSessionStatus.CLOSED, BrowserSessionStatus.LOST}:
            self.closed_at = at or utc_now()

    def register_page(self, page_id: str, *, make_current: bool = True) -> None:
        if page_id not in self.page_ids:
            self.page_ids.append(page_id)
        if make_current:
            self.current_page_id = page_id

    def close_page(self, page_id: str) -> None:
        self.page_ids = [item for item in self.page_ids if item != page_id]
        if self.current_page_id == page_id:
            self.current_page_id = self.page_ids[-1] if self.page_ids else None

    def take_over(self, *, observation_version: int) -> None:
        if self.status is not BrowserSessionStatus.ACTIVE:
            raise InvalidStateTransitionError("BrowserSession", self.status, BrowserOwner.USER)
        if self.owner is BrowserOwner.USER:
            return
        self.owner = BrowserOwner.USER
        self.takeover_started_at = utc_now()
        self.takeover_observation_version = observation_version

    def release(self) -> None:
        if self.status is not BrowserSessionStatus.ACTIVE:
            raise InvalidStateTransitionError("BrowserSession", self.status, BrowserOwner.AGENT)
        self.owner = BrowserOwner.AGENT
        self.takeover_started_at = None
        self.takeover_observation_version = None

    @property
    def agent_can_write(self) -> bool:
        return self.status is BrowserSessionStatus.ACTIVE and self.owner is BrowserOwner.AGENT


class BrowserPage(DomainModel):
    id: str = Field(default_factory=new_id)
    browser_session_id: str = Field(min_length=1)
    url: str = Field(max_length=8192)
    title: str = Field(default="", max_length=2000)
    status: BrowserPageStatus = BrowserPageStatus.OPEN
    last_observation_version: int = Field(default=0, ge=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    closed_at: AwareDatetime | None = None


class InteractiveElement(DomainModel):
    ref: str = Field(pattern=r"^e-[1-9][0-9]*$")
    role: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, max_length=1000)
    input_type: str | None = Field(default=None, max_length=128)
    disabled: bool = False
    href: str | None = Field(default=None, max_length=8192)
    frame_id: str | None = Field(default=None, max_length=255)


class FormFieldSummary(DomainModel):
    ref: str | None = None
    name: str | None = Field(default=None, max_length=255)
    label: str | None = Field(default=None, max_length=1000)
    input_type: str | None = Field(default=None, max_length=128)
    required: bool = False


class FormSummary(DomainModel):
    ref: str
    action: str | None = Field(default=None, max_length=8192)
    method: str | None = Field(default=None, max_length=32)
    fields: list[FormFieldSummary] = Field(default_factory=list, max_length=100)


class NetworkEventSummary(DomainModel):
    sequence: int = Field(ge=1)
    method: str = Field(min_length=1, max_length=32)
    url: str = Field(max_length=8192)
    resource_type: str = Field(default="", max_length=128)
    status_code: int | None = Field(default=None, ge=100, le=599)
    failed: bool = False
    failure_text: str | None = Field(default=None, max_length=2000)
    observed_at: AwareDatetime = Field(default_factory=utc_now)


class BrowserObservation(DomainModel):
    id: str = Field(default_factory=new_id)
    browser_session_id: str = Field(min_length=1)
    page_id: str = Field(min_length=1)
    url: str = Field(max_length=8192)
    title: str = Field(max_length=2000)
    visible_text_excerpt: str = Field(max_length=20_000)
    headings: list[str] = Field(default_factory=list, max_length=100)
    interactive_elements: list[InteractiveElement] = Field(default_factory=list, max_length=300)
    forms: list[FormSummary] = Field(default_factory=list, max_length=50)
    alerts: list[str] = Field(default_factory=list, max_length=50)
    console_errors: list[str] = Field(default_factory=list, max_length=100)
    recent_network_summary: list[NetworkEventSummary] = Field(default_factory=list, max_length=100)
    screenshot_artifact_id: str | None = None
    network_artifact_id: str | None = None
    dom_artifact_id: str | None = None
    observation_version: int = Field(ge=1)
    content_trust: Literal["UNTRUSTED_EXTERNAL_CONTENT"] = "UNTRUSTED_EXTERNAL_CONTENT"
    created_at: AwareDatetime = Field(default_factory=utc_now)


class BrowserAction(DomainModel):
    id: str = Field(default_factory=new_id)
    action_key: str = Field(min_length=1, max_length=255)
    browser_session_id: str = Field(min_length=1)
    page_id: str = Field(min_length=1)
    observation_version: int = Field(ge=1)
    action: BrowserActionType
    element_ref: str | None = None
    value: str | None = Field(default=None, max_length=100_000)
    url: str | None = Field(default=None, max_length=8192)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    status: BrowserActionStatus = BrowserActionStatus.PROPOSED
    result_observation_id: str | None = None
    download_artifact_id: str | None = None
    error: str = ""
    created_at: AwareDatetime = Field(default_factory=utc_now)
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_target(self) -> BrowserAction:
        element_actions = {
            BrowserActionType.CLICK,
            BrowserActionType.FILL,
            BrowserActionType.TYPE,
            BrowserActionType.SELECT,
            BrowserActionType.PRESS,
            BrowserActionType.UPLOAD,
            BrowserActionType.DOWNLOAD,
        }
        if self.action in element_actions and not self.element_ref:
            raise ValueError(f"{self.action.value} requires element_ref")
        if self.action is BrowserActionType.NAVIGATE and not self.url:
            raise ValueError("navigate requires url")
        if self.action in {BrowserActionType.FILL, BrowserActionType.TYPE} and self.value is None:
            raise ValueError(f"{self.action.value} requires value")
        return self


class BrowserTakeoverSummary(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    browser_session_id: str = Field(min_length=1)
    started_observation_version: int = Field(ge=0)
    ended_observation_version: int = Field(ge=0)
    started_at: AwareDatetime | None = None
    released_at: AwareDatetime = Field(default_factory=utc_now)
    url_changes: list[str] = Field(default_factory=list, max_length=100)
    opened_page_ids: list[str] = Field(default_factory=list, max_length=100)
    download_artifact_ids: list[str] = Field(default_factory=list, max_length=100)
    network_summary: list[NetworkEventSummary] = Field(default_factory=list, max_length=100)
    storage_changed: bool = False
    summary: str = Field(max_length=8000)

    @model_validator(mode="after")
    def validate_versions(self) -> BrowserTakeoverSummary:
        if self.ended_observation_version < self.started_observation_version:
            raise ValueError("takeover ended before its starting observation")
        return self
