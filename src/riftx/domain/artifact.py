"""Immutable references to files produced by a run."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .base import DomainModel, new_id, utc_now

ARTIFACT_INGEST_PROVENANCE_SCHEMA_VERSION: Literal["riftx.artifact-ingest-provenance/v1"] = (
    "riftx.artifact-ingest-provenance/v1"
)


class ArtifactAccessClass(StrEnum):
    """Server-owned visibility partition for one immutable Artifact."""

    PUBLIC_EXPORT = "public_export"
    AUDIT_INTERNAL = "audit_internal"
    RESTRICTED_SENSITIVE = "restricted_sensitive"


class ArtifactContentTrust(StrEnum):
    """How consumers must treat the bytes referenced by an Artifact."""

    GENERATED = "generated"
    UNTRUSTED_SOURCE = "untrusted_source"
    UNTRUSTED_TOOL_OUTPUT = "untrusted_tool_output"


class ArtifactIngestMethod(StrEnum):
    """How Artifact bytes entered the private Artifact store."""

    LEGACY_MIGRATED = "legacy_migrated"
    LOCAL_NOFOLLOW_FD = "local_nofollow_fd"
    CONTROL_PLANE_BYTES = "control_plane_bytes"
    AUTHENTICATED_CHUNK_STREAM = "authenticated_chunk_stream"


class ArtifactIngestProvenance(DomainModel):
    """Versioned, non-secret provenance for one immutable byte ingestion."""

    schema_version: Literal["riftx.artifact-ingest-provenance/v1"] = (
        ARTIFACT_INGEST_PROVENANCE_SCHEMA_VERSION
    )
    method: ArtifactIngestMethod = ArtifactIngestMethod.LEGACY_MIGRATED
    producer_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    producer_execution_id: str | None = Field(default=None, min_length=1, max_length=64)


def _validate_storage_component(
    value: str,
    *,
    label: str,
    max_length: int,
) -> str:
    if not value or value in {".", ".."}:
        raise ValueError(f"{label} must be a non-empty safe path component")
    if "/" in value or "\\" in value:
        raise ValueError(f"{label} must not contain a path separator")
    if any(not 0x20 <= ord(character) <= 0x7E for character in value):
        raise ValueError(f"{label} must contain printable ASCII characters only")
    if len(value) > max_length:
        raise ValueError(f"{label} exceeds its maximum length")
    return value


def canonical_artifact_storage_key(
    run_id: str,
    artifact_id: str,
    name: str,
) -> str:
    """Return the sole canonical relative locator for an Artifact."""

    safe_run_id = _validate_storage_component(run_id, label="run_id", max_length=64)
    safe_artifact_id = _validate_storage_component(
        artifact_id,
        label="artifact_id",
        max_length=64,
    )
    safe_name = _validate_storage_component(name, label="name", max_length=255)
    return f"runs/{safe_run_id}/artifacts/{safe_artifact_id}/{safe_name}"


class Artifact(DomainModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    execution_id: str | None = Field(default=None, min_length=1, max_length=64)
    audit_id: str | None = Field(default=None, min_length=1, max_length=128)
    access_class: ArtifactAccessClass = ArtifactAccessClass.PUBLIC_EXPORT
    content_trust: ArtifactContentTrust = ArtifactContentTrust.UNTRUSTED_TOOL_OUTPUT
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, repr=False)
    storage_key: str = Field(min_length=1, max_length=4096, repr=False)
    ingest_provenance: ArtifactIngestProvenance = Field(default_factory=ArtifactIngestProvenance)
    mime_type: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    description: str = ""
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def supply_legacy_compatible_storage_key(cls, value: Any) -> Any:
        """Derive a key only when old trusted callers omit the new field.

        An explicitly persisted NULL is left intact so reconstruction fails
        closed instead of silently repairing corrupt state.
        """

        if isinstance(value, dict) and "storage_key" not in value:
            payload = dict(value)
            artifact_id = payload.get("id")
            if artifact_id is None:
                artifact_id = new_id()
                payload["id"] = artifact_id
            try:
                payload["storage_key"] = canonical_artifact_storage_key(
                    payload["run_id"],
                    artifact_id,
                    payload["name"],
                )
            except (KeyError, TypeError):
                return value
            return payload
        return value

    @model_validator(mode="after")
    def validate_access_and_storage(self) -> Self:
        expected_key = canonical_artifact_storage_key(self.run_id, self.id, self.name)
        if self.storage_key != expected_key:
            raise ValueError("storage_key is not canonical for this Artifact")
        if self.audit_id is None and self.access_class is not ArtifactAccessClass.PUBLIC_EXPORT:
            raise ValueError("non-public Artifact access requires an Audit owner")
        if self.audit_id is not None:
            if "access_class" not in self.model_fields_set:
                raise ValueError("Audit-owned Artifacts require an explicit access_class")
            if "ingest_provenance" not in self.model_fields_set:
                raise ValueError("Audit-owned Artifacts require explicit ingest provenance")
            if self.ingest_provenance.method is ArtifactIngestMethod.LEGACY_MIGRATED:
                raise ValueError("Audit-owned Artifacts cannot use legacy ingest provenance")
        producer_execution_id = self.ingest_provenance.producer_execution_id
        if producer_execution_id is not None and producer_execution_id != self.execution_id:
            raise ValueError("producer_execution_id must equal the Artifact execution_id")
        return self

    @field_validator("mime_type")
    @classmethod
    def validate_safe_mime_type(cls, value: str) -> str:
        if value != value.strip() or any(not 0x20 <= ord(character) <= 0x7E for character in value):
            raise ValueError("mime_type must contain printable ASCII header characters")
        return value


__all__ = [
    "ARTIFACT_INGEST_PROVENANCE_SCHEMA_VERSION",
    "Artifact",
    "ArtifactAccessClass",
    "ArtifactContentTrust",
    "ArtifactIngestMethod",
    "ArtifactIngestProvenance",
    "canonical_artifact_storage_key",
]
