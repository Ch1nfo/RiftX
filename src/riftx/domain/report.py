"""Generated report metadata."""

from pydantic import AwareDatetime, Field

from .base import DomainModel, new_id, utc_now
from .enums import ReportFormat


class Report(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    format: ReportFormat
    artifact_id: str
    finding_ids: list[str] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=utc_now)
