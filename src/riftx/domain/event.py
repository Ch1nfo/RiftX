"""Durable event timeline entries."""

from pydantic import AwareDatetime, Field

from .base import DomainModel, new_id, utc_now


class RunEvent(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: AwareDatetime = Field(default_factory=utc_now)
