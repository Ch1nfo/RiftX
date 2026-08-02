"""OpenAI Agents SDK session adapter backed by RiftX's durable transcript."""

from __future__ import annotations

import json
from typing import Any, cast

from agents.items import TResponseInputItem
from agents.memory.session_settings import SessionSettings
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain import (
    MessageRole,
    MessageType,
    MessageVisibility,
    TranscriptMessageDraft,
)
from riftx.domain.base import utc_now
from riftx.persistence.orm import AgentSessionRecord, RunRecord
from riftx.persistence.transcript_repositories import SQLAlchemyTranscriptRepository
from riftx.runtime.types import SessionStatus

SessionFactory = async_sessionmaker[AsyncSession]


class RiftXDatabaseSession:
    """Agents SDK compatibility layer over the authoritative RiftX transcript."""

    def __init__(
        self,
        run_id: str,
        session_factory: SessionFactory,
        *,
        agent_session_id: str | None = None,
        agent_id: str = "primary",
        max_history_items: int | None = None,
    ) -> None:
        if max_history_items is not None and max_history_items < 1:
            raise ValueError("max_history_items must be positive")
        self.run_id = run_id
        self.session_id = agent_session_id or run_id
        self.agent_id = agent_id
        self.session_settings = SessionSettings(limit=max_history_items)
        self._session_factory = session_factory
        self._transcript = SQLAlchemyTranscriptRepository(session_factory)

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        messages = await self._transcript.list_by_session(self.session_id, limit=limit)
        return [cast(TResponseInputItem, _message_payload(message)) for message in messages]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        if not items:
            return
        await self._ensure_agent_session()
        drafts = [_item_to_draft(item, agent_id=self.agent_id) for item in items]
        await self._transcript.append_many(self.session_id, drafts)

    async def pop_item(self) -> TResponseInputItem | None:
        message = await self._transcript.pop(self.session_id)
        if message is None:
            return None
        return cast(TResponseInputItem, _message_payload(message))

    async def clear_session(self) -> None:
        await self._transcript.clear(self.session_id)

    async def trim_history(self, max_items: int) -> tuple[int, int]:
        """Keep the latest items and return ``(removed, retained)``."""

        return await self._transcript.trim(self.session_id, max_items)

    async def _ensure_agent_session(self) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(RunRecord).where(RunRecord.id == self.run_id).with_for_update()
            )
            if run is None:
                raise EntityNotFoundError("Run", self.run_id)
            existing = await session.get(AgentSessionRecord, self.session_id)
            if existing is not None:
                if existing.run_id != self.run_id:
                    raise RepositoryConflictError(
                        f"agent session {self.session_id!r} belongs to another run"
                    )
                return
            session.add(
                AgentSessionRecord(
                    id=self.session_id,
                    run_id=self.run_id,
                    parent_session_id=None,
                    agent_type=self.agent_id,
                    model_profile=run.model_profile or "default",
                    status=SessionStatus.ACTIVE.value,
                    latest_checkpoint_id=None,
                    provider_state_id=None,
                    turn_count=0,
                    model_call_count=0,
                    tool_call_count=0,
                    created_at=utc_now(),
                    closed_at=None,
                )
            )
            await session.flush()


def _item_to_draft(item: TResponseInputItem, *, agent_id: str) -> TranscriptMessageDraft:
    payload, serialized = _serialize_item(item)
    role = _item_role(payload)
    message_type = _message_type(payload, role)
    visibility = (
        MessageVisibility.USER_VISIBLE
        if message_type in {MessageType.USER_MESSAGE, MessageType.ASSISTANT_MESSAGE}
        else MessageVisibility.AGENT_ONLY
    )
    tool_call_id = payload.get("call_id")
    return TranscriptMessageDraft(
        agent_id=agent_id,
        role=role,
        message_type=message_type,
        content=serialized,
        structured_content=payload,
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
        visibility=visibility,
    )


def _serialize_item(item: TResponseInputItem) -> tuple[dict[str, Any], str]:
    if isinstance(item, BaseModel):
        payload = item.model_dump(mode="json")
    elif isinstance(item, dict):
        payload = dict(item)
    else:
        raise TypeError(f"unsupported session item type: {type(item).__name__}")
    return payload, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _message_payload(message: object) -> dict[str, Any]:
    structured = getattr(message, "structured_content", None)
    if isinstance(structured, dict):
        return dict(structured)
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        return {}
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("SDK transcript payload must be a JSON object")
    return parsed


def _item_type(item: dict[str, Any]) -> str:
    value = item.get("type", "message")
    return value if isinstance(value, str) and value else "message"


def _message_type(item: dict[str, Any], role: MessageRole) -> MessageType:
    item_type = _item_type(item)
    if item_type in {"function_call", "computer_call", "tool_call"}:
        return MessageType.TOOL_CALL
    if item_type.endswith("_output") or item_type in {"tool_result", "computer_call_output"}:
        return MessageType.TOOL_RESULT_REFERENCE
    if item_type in {"handoff_request", "subagent_delegation"}:
        return MessageType.SUBAGENT_DELEGATION
    if item_type in {"handoff_output", "subagent_result"}:
        return MessageType.SUBAGENT_RESULT
    if role is MessageRole.USER:
        return MessageType.USER_MESSAGE
    return MessageType.ASSISTANT_MESSAGE


def _item_role(item: dict[str, Any]) -> MessageRole:
    value = item.get("role")
    if isinstance(value, str):
        try:
            return MessageRole(value)
        except ValueError:
            pass
    item_type = _item_type(item)
    if item_type.endswith("_output") or item_type in {"tool_result", "computer_call_output"}:
        return MessageRole.TOOL
    return MessageRole.ASSISTANT
