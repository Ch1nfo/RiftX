"""Shared primitives for infrastructure-independent domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict


def new_id() -> str:
    """Return a sortable-enough opaque identifier for a new domain entity."""

    return str(uuid4())


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class DomainModel(BaseModel):
    """Base model shared by every RiftX domain object."""

    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        validate_assignment=True,
    )
