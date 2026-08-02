"""Immutable references to files produced by a run."""

from pydantic import AwareDatetime, Field

from .base import DomainModel, new_id, utc_now


class Artifact(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    execution_id: str | None = None
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    description: str = ""
    created_at: AwareDatetime = Field(default_factory=utc_now)
