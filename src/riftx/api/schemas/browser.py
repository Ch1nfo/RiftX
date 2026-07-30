"""Managed browser REST and WebSocket schemas."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, Field, JsonValue

from riftx.browser.service import ActBrowser, BrowserView, OpenBrowser
from riftx.domain import (
    BrowserActionStatus,
    BrowserActionType,
    BrowserMode,
    BrowserObservation,
    BrowserOwner,
    BrowserPage,
    BrowserSessionStatus,
    BrowserTakeoverSummary,
)
from riftx.domain.base import new_id


class BrowserSessionCreateRequest(BaseModel):
    run_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    mode: BrowserMode = BrowserMode.MANAGED_EPHEMERAL
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    cdp_endpoint: str | None = None
    headless: bool = False
    include_screenshot: bool = True

    def to_command(self) -> OpenBrowser:
        return OpenBrowser(**self.model_dump())


class BrowserObserveRequest(BaseModel):
    page_id: str | None = Field(default=None, min_length=1)
    include_screenshot: bool = False
    include_network: bool = True


class BrowserActionRequest(BaseModel):
    page_id: str = Field(min_length=1)
    observation_version: int = Field(ge=1)
    action: BrowserActionType
    action_key: str = Field(default_factory=new_id, min_length=1, max_length=255)
    element_ref: str | None = None
    value: str | None = None
    url: str | None = None
    options: dict[str, JsonValue] = Field(default_factory=dict)
    include_screenshot: bool = True

    def to_command(self) -> ActBrowser:
        return ActBrowser(**self.model_dump())


class BrowserSessionResponse(BaseModel):
    id: str
    run_id: str
    agent_session_id: str
    node_id: str
    mode: BrowserMode
    status: BrowserSessionStatus
    owner: BrowserOwner
    browser_type: str
    profile_id: str | None
    current_page_id: str | None
    page_ids: list[str]
    created_at: AwareDatetime
    closed_at: AwareDatetime | None


class BrowserActionResponse(BaseModel):
    id: str
    action_key: str
    browser_session_id: str
    page_id: str
    observation_version: int
    action: BrowserActionType
    element_ref: str | None
    url: str | None
    status: BrowserActionStatus
    result_observation_id: str | None
    download_artifact_id: str | None
    error: str
    created_at: AwareDatetime
    completed_at: AwareDatetime | None


class BrowserViewResponse(BaseModel):
    session: BrowserSessionResponse
    pages: list[BrowserPage]
    observation: BrowserObservation | None = None
    action: BrowserActionResponse | None = None
    takeover_summary: BrowserTakeoverSummary | None = None

    @classmethod
    def from_view(cls, view: BrowserView) -> BrowserViewResponse:
        session = view.session
        action = view.action
        return cls(
            session=BrowserSessionResponse(
                id=session.id,
                run_id=session.run_id,
                agent_session_id=session.agent_session_id,
                node_id=session.node_id,
                mode=session.mode,
                status=session.status,
                owner=session.owner,
                browser_type=session.browser_type,
                profile_id=session.profile_id,
                current_page_id=session.current_page_id,
                page_ids=session.page_ids,
                created_at=session.created_at,
                closed_at=session.closed_at,
            ),
            pages=list(view.pages),
            observation=view.observation,
            action=(
                BrowserActionResponse(
                    id=action.id,
                    action_key=action.action_key,
                    browser_session_id=action.browser_session_id,
                    page_id=action.page_id,
                    observation_version=action.observation_version,
                    action=action.action,
                    element_ref=action.element_ref,
                    url=action.url,
                    status=action.status,
                    result_observation_id=action.result_observation_id,
                    download_artifact_id=action.download_artifact_id,
                    error=action.error,
                    created_at=action.created_at,
                    completed_at=action.completed_at,
                )
                if action is not None
                else None
            ),
            takeover_summary=view.takeover_summary,
        )
