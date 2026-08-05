"""Shared transaction primitives for serialized persistence mutations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SessionFactory = async_sessionmaker[AsyncSession]


@asynccontextmanager
async def serialized_write(
    session_factory: SessionFactory,
) -> AsyncIterator[AsyncSession]:
    """Serialize a read/decision/write unit on every supported database.

    Server databases use their normal transaction and explicit row locks in the
    caller. SQLite ignores ``SELECT ... FOR UPDATE`` and defers ``BEGIN`` until
    the first write, so ``BEGIN IMMEDIATE`` must run before the caller's first
    read to prevent two lifecycle writers from making a decision on the same
    stale state.
    """

    async with session_factory() as session:
        if session.get_bind().dialect.name == "sqlite":
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()
            return

        async with session.begin():
            yield session


@asynccontextmanager
async def consistent_read(
    session_factory: SessionFactory,
) -> AsyncIterator[AsyncSession]:
    """Read a multi-query aggregate from one database snapshot.

    PostgreSQL's default READ COMMITTED isolation can otherwise observe a newer
    Run after reading an older AuditScan in the same session. SQLite keeps one
    snapshot after an explicit read transaction begins.
    """

    async with session_factory() as session:
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        elif dialect_name == "sqlite":
            await session.execute(text("BEGIN"))
        else:
            raise RuntimeError(
                "RiftX aggregate reads do not define snapshot isolation for "
                f"database dialect {dialect_name!r}"
            )
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()
