"""Store-owned clocks for durable projected metadata mutations."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]


def next_mutation_at(
    clock: Clock,
    *,
    stored: datetime | None = None,
    lifecycle_timestamps: Iterable[datetime | None] = (),
) -> datetime:
    """Return the next aware UTC mutation stamp owned by the repository writer."""

    candidates = [_aware_utc(clock(), source="clock")]
    if stored is not None:
        candidates.append(
            _aware_utc(stored, source="stored updated_at") + timedelta(microseconds=1)
        )
    candidates.extend(
        _aware_utc(value, source="lifecycle timestamp")
        for value in lifecycle_timestamps
        if value is not None
    )
    return max(candidates)


def _aware_utc(value: datetime, *, source: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"durable mutation {source} must be timezone-aware")
    return value.astimezone(UTC)
