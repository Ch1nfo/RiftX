"""Engagement domain model."""

from pydantic import AwareDatetime, Field

from .base import DomainModel, new_id, utc_now


class Engagement(DomainModel):
    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1)
    description: str = ""
    authorization_reference: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
