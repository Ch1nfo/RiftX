"""Three-layer Tool Result models safe for Agent context serialization."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import ExecutionStatus


class OutputStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class RawArtifactReference(BaseModel):
    """Logical artifact reference that never exposes a Runner filesystem path."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str | None = None
    uri: str = Field(pattern=r"^artifact://runs/[^/]+/executions/[^/]+/(stdout|stderr)$")
    stream: OutputStream
    mime_type: str
    size: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    available: bool = True
    error: str | None = None


class ArtifactReadResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    uri: str
    mime_type: str
    data: bytes
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    eof: bool


class StreamPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream: OutputStream
    size: int = Field(ge=0)
    mime_type: str
    text: str = ""
    truncated: bool = False
    binary: bool = False


class ProcessedToolResult(BaseModel):
    """Structured and bounded Agent-facing view over immutable raw artifacts."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    status: ExecutionStatus
    tool_id: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    parser: str
    parser_error: str | None = None
    raw_artifacts: list[RawArtifactReference] = Field(default_factory=list)
    structured_result: dict[str, object] = Field(default_factory=dict)
    context_summary: str
    key_observations: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    statistics: dict[str, object] = Field(default_factory=dict)
    previews: list[StreamPreview] = Field(default_factory=list)
