"""Bounded read models used by the Observer Supervisor."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from .orm import BrowserSessionRecord, TerminalSessionRecord
from .transactions import SessionFactory


class SQLAlchemyActiveTakeoverReader:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def active_for_run(self, run_id: str, *, limit: int = 100) -> Sequence[str]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        terminal_statement = (
            select(TerminalSessionRecord.id)
            .where(
                TerminalSessionRecord.run_id == run_id,
                TerminalSessionRecord.takeover_started_at.is_not(None),
            )
            .order_by(TerminalSessionRecord.id)
            .limit(limit)
        )
        browser_statement = (
            select(BrowserSessionRecord.id)
            .where(
                BrowserSessionRecord.run_id == run_id,
                BrowserSessionRecord.takeover_started_at.is_not(None),
            )
            .order_by(BrowserSessionRecord.id)
            .limit(limit)
        )
        async with self._session_factory() as session:
            terminal_ids = await session.scalars(terminal_statement)
            browser_ids = await session.scalars(browser_statement)
        return tuple(
            sorted(
                [*(f"terminal:{item}" for item in terminal_ids),
                 *(f"browser:{item}" for item in browser_ids)]
            )[:limit]
        )
