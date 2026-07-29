from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from riftx.persistence import Database
from riftx.persistence.orm import EngagementRecord


async def test_sqlite_round_trip_preserves_aware_utc_timestamps(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    created_at = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)

    async with database.session() as session, session.begin():
        session.add(
            EngagementRecord(
                id="engagement-1",
                name="Test",
                description="",
                authorization_reference=None,
                created_at=created_at,
                updated_at=created_at,
            )
        )

    async with database.session() as session:
        record = await session.scalar(
            select(EngagementRecord).where(EngagementRecord.id == "engagement-1")
        )

    assert record is not None
    assert record.created_at == created_at
    assert record.created_at.tzinfo is UTC
    await database.dispose()
