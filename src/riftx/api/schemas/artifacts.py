"""Artifact registration and read schemas."""

from datetime import datetime
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import Artifact, ArtifactAccessClass, ArtifactContentTrust


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
    audit_id: str | None
    execution_id: str | None
    name: str
    mime_type: str
    sha256: str
    size: int
    description: str
    access_class: ArtifactAccessClass
    content_trust: ArtifactContentTrust
    created_at: datetime
    content_url: str

    @classmethod
    def from_domain(cls, artifact: Artifact) -> "ArtifactResponse":
        artifact_id = quote(artifact.id, safe="")
        content_url = f"/api/v1/artifacts/{artifact_id}/content"
        if artifact.audit_id is not None:
            content_url = (
                f"/api/v1/audits/{quote(artifact.audit_id, safe='')}/"
                f"artifacts/{artifact_id}/content"
            )
        return cls(
            id=artifact.id,
            run_id=artifact.run_id,
            audit_id=artifact.audit_id,
            execution_id=artifact.execution_id,
            name=artifact.name,
            mime_type=artifact.mime_type,
            sha256=artifact.sha256,
            size=artifact.size,
            description=artifact.description,
            access_class=artifact.access_class,
            content_trust=artifact.content_trust,
            created_at=artifact.created_at,
            content_url=content_url,
        )


class ArtifactListResponse(BaseModel):
    items: list[ArtifactResponse]
    limit: int
    offset: int
