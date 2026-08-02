"""Generated Run report schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from riftx.application.services import GenerateReports
from riftx.domain import Report, ReportFormat


class GenerateReportsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formats: list[ReportFormat] = Field(
        default_factory=lambda: [
            ReportFormat.MARKDOWN,
            ReportFormat.HTML,
            ReportFormat.JSON,
        ],
        min_length=1,
        max_length=3,
    )

    def to_command(self) -> GenerateReports:
        return GenerateReports(formats=self.formats)


class ReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    format: ReportFormat
    artifact_id: str
    finding_ids: list[str]
    created_at: datetime
    content_url: str

    @classmethod
    def from_domain(cls, report: Report) -> "ReportResponse":
        return cls.model_validate(
            {
                **report.model_dump(mode="json"),
                "content_url": f"/api/v1/artifacts/{report.artifact_id}/content",
            }
        )


class ReportListResponse(BaseModel):
    items: list[ReportResponse]
    limit: int
    offset: int
