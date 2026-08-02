"""Agent-facing, bounded managed-browser tool contracts."""

from __future__ import annotations

from pydantic import BaseModel, Field, JsonValue

from riftx.browser.service import ActBrowser, BrowserApplicationService, BrowserView, OpenBrowser
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


class OpenBrowserToolInput(BaseModel):
    run_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    mode: BrowserMode = BrowserMode.MANAGED_EPHEMERAL
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    cdp_endpoint: str | None = None


class ObserveBrowserToolInput(BaseModel):
    browser_session_id: str = Field(min_length=1)
    page_id: str | None = Field(default=None, min_length=1)
    include_screenshot: bool = False
    include_network: bool = True


class ActBrowserToolInput(BaseModel):
    browser_session_id: str = Field(min_length=1)
    page_id: str = Field(min_length=1)
    observation_version: int = Field(ge=1)
    action: BrowserActionType
    action_key: str = Field(default_factory=new_id, min_length=1, max_length=255)
    element_ref: str | None = None
    value: str | None = None
    url: str | None = None
    options: dict[str, JsonValue] = Field(default_factory=dict)


class BrowserSessionToolInput(BaseModel):
    browser_session_id: str = Field(min_length=1)


class BrowserToolSession(BaseModel):
    id: str
    run_id: str
    node_id: str
    mode: BrowserMode
    status: BrowserSessionStatus
    owner: BrowserOwner
    current_page_id: str | None
    page_ids: list[str]


class BrowserToolAction(BaseModel):
    id: str
    action_key: str
    page_id: str
    observation_version: int
    action: BrowserActionType
    status: BrowserActionStatus
    result_observation_id: str | None
    download_artifact_id: str | None
    error: str


class BrowserToolResult(BaseModel):
    session: BrowserToolSession
    pages: list[BrowserPage]
    observation: BrowserObservation | None = None
    action: BrowserToolAction | None = None
    takeover_summary: BrowserTakeoverSummary | None = None

    @classmethod
    def from_view(cls, view: BrowserView) -> BrowserToolResult:
        session = view.session
        action = view.action
        return cls(
            session=BrowserToolSession(
                id=session.id,
                run_id=session.run_id,
                node_id=session.node_id,
                mode=session.mode,
                status=session.status,
                owner=session.owner,
                current_page_id=session.current_page_id,
                page_ids=session.page_ids,
            ),
            pages=list(view.pages),
            observation=view.observation,
            action=(
                BrowserToolAction(
                    id=action.id,
                    action_key=action.action_key,
                    page_id=action.page_id,
                    observation_version=action.observation_version,
                    action=action.action,
                    status=action.status,
                    result_observation_id=action.result_observation_id,
                    download_artifact_id=action.download_artifact_id,
                    error=action.error,
                )
                if action is not None
                else None
            ),
            takeover_summary=view.takeover_summary,
        )


class BrowserTools:
    """Small tool facade used by agent runtimes after dynamic tool selection."""

    def __init__(self, service: BrowserApplicationService) -> None:
        self._service = service

    async def open_browser(self, item: OpenBrowserToolInput) -> BrowserToolResult:
        view = await self._service.open(
            OpenBrowser(
                run_id=item.run_id,
                agent_session_id=item.agent_session_id,
                url=item.url,
                mode=item.mode,
                profile_id=item.profile_id,
                cdp_endpoint=item.cdp_endpoint,
            )
        )
        return BrowserToolResult.from_view(view)

    async def observe_browser(self, item: ObserveBrowserToolInput) -> BrowserToolResult:
        view = await self._service.observe(
            item.browser_session_id,
            page_id=item.page_id,
            include_screenshot=item.include_screenshot,
            include_network=item.include_network,
        )
        return BrowserToolResult.from_view(view)

    async def act_browser(self, item: ActBrowserToolInput) -> BrowserToolResult:
        view = await self._service.act(
            item.browser_session_id,
            ActBrowser(
                page_id=item.page_id,
                observation_version=item.observation_version,
                action=item.action,
                action_key=item.action_key,
                element_ref=item.element_ref,
                value=item.value,
                url=item.url,
                options=item.options,
            ),
        )
        return BrowserToolResult.from_view(view)

    async def takeover_browser(self, item: BrowserSessionToolInput) -> BrowserToolResult:
        view = await self._service.takeover(item.browser_session_id)
        return BrowserToolResult.from_view(view)

    async def release_browser(self, item: BrowserSessionToolInput) -> BrowserToolResult:
        view = await self._service.release(item.browser_session_id)
        return BrowserToolResult.from_view(view)

    async def close_browser(self, item: BrowserSessionToolInput) -> BrowserToolResult:
        view = await self._service.close(item.browser_session_id)
        return BrowserToolResult.from_view(view)
