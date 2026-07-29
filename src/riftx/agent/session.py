"""Durable OpenAI Agents SDK session backed by RiftX persistence."""

from __future__ import annotations

import json
from typing import Any, cast

from agents.items import TResponseInputItem
from agents.memory.session_settings import SessionSettings
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError
from riftx.domain.base import new_id, utc_now
from riftx.domain.enums import MessageRole
from riftx.persistence.orm import AgentMessageRecord, RunRecord

SessionFactory = async_sessionmaker[AsyncSession]


class RiftXDatabaseSession:
    """Persist SDK input items in the run's ordered message stream."""

    def __init__(
        self,
        run_id: str,
        session_factory: SessionFactory,
        *,
        max_history_items: int | None = None,
    ) -> None:
        if max_history_items is not None and max_history_items < 1:
            raise ValueError("max_history_items must be positive")
        self.session_id = run_id
        self.session_settings = SessionSettings(limit=max_history_items)
        self._session_factory = session_factory

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")

        statement = (
            select(AgentMessageRecord)
            .where(AgentMessageRecord.run_id == self.session_id)
            .order_by(AgentMessageRecord.sequence.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)

        async with self._session_factory() as session:
            records = list((await session.scalars(statement)).all())
        records.reverse()
        return [cast(TResponseInputItem, json.loads(record.content)) for record in records]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        if not items:
            return
        serialized = [_serialize_item(item) for item in items]

        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(RunRecord).where(RunRecord.id == self.session_id).with_for_update()
            )
            if run is None:
                raise EntityNotFoundError("Run", self.session_id)
            current = await session.scalar(
                select(func.max(AgentMessageRecord.sequence)).where(
                    AgentMessageRecord.run_id == self.session_id
                )
            )
            first_sequence = (current or 0) + 1
            now = utc_now()
            session.add_all(
                [
                    AgentMessageRecord(
                        id=new_id(),
                        run_id=self.session_id,
                        role=_item_role(payload).value,
                        message_type=_item_type(payload),
                        content=content,
                        sequence=first_sequence + offset,
                        created_at=now,
                    )
                    for offset, (payload, content) in enumerate(serialized)
                ]
            )
            await session.flush()

    async def pop_item(self) -> TResponseInputItem | None:
        async with self._session_factory() as session, session.begin():
            record = await session.scalar(
                select(AgentMessageRecord)
                .where(AgentMessageRecord.run_id == self.session_id)
                .order_by(AgentMessageRecord.sequence.desc())
                .limit(1)
                .with_for_update()
            )
            if record is None:
                return None
            item = cast(TResponseInputItem, json.loads(record.content))
            await session.delete(record)
            await session.flush()
            return item

    async def clear_session(self) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(AgentMessageRecord).where(AgentMessageRecord.run_id == self.session_id)
            )

    async def trim_history(self, max_items: int) -> tuple[int, int]:
        """Keep the latest items and return ``(removed, retained)``."""

        if max_items < 1:
            raise ValueError("max_items must be positive")
        async with self._session_factory() as session, session.begin():
            records = list(
                (
                    await session.scalars(
                        select(AgentMessageRecord)
                        .where(AgentMessageRecord.run_id == self.session_id)
                        .order_by(AgentMessageRecord.sequence.desc())
                    )
                ).all()
            )
            retained = min(len(records), max_items)
            stale_ids = [record.id for record in records[max_items:]]
            if stale_ids:
                await session.execute(
                    delete(AgentMessageRecord).where(AgentMessageRecord.id.in_(stale_ids))
                )
            return len(stale_ids), retained


def _serialize_item(item: TResponseInputItem) -> tuple[dict[str, Any], str]:
    if isinstance(item, BaseModel):
        payload = item.model_dump(mode="json")
    elif isinstance(item, dict):
        payload = dict(item)
    else:
        raise TypeError(f"unsupported session item type: {type(item).__name__}")
    return payload, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _item_type(item: dict[str, Any]) -> str:
    value = item.get("type", "message")
    return value if isinstance(value, str) and value else "message"


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
