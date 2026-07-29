"""Artifact registration and read schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import Artifact


class RegisterArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    mime_type: str | None = Field(default=None, min_length=1, max_length=255)
    description: str = ""
    execution_id: str | None = Field(default=None, min_length=1)


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    execution_id: str | None
    name: str
    mime_type: str
    sha256: str
    size: int
    description: str
    created_at: datetime
    content_url: str

    @classmethod
    def from_domain(cls, artifact: Artifact) -> "ArtifactResponse":
        return cls.model_validate(
            {
                **artifact.model_dump(mode="json", exclude={"path"}),
                "content_url": f"/api/v1/artifacts/{artifact.id}/content",
            }
        )


class ArtifactListResponse(BaseModel):
    items: list[ArtifactResponse]
    limit: int
    offset: int
