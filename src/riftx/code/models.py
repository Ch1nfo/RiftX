"""Bounded model-facing results for native code navigation tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CodeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    type: Literal["file", "directory", "symlink", "special"]
    size: int = Field(ge=0)
    content_digest: str | None = None


class CodeListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["workspace", "audit_snapshot"]
    source_digest: str | None = None
    path: str
    entries: list[CodeEntry]
    truncated: bool = False


class CodeReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["workspace", "audit_snapshot"]
    source_digest: str | None = None
    path: str
    size: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    eof: bool
    encoding: Literal["utf-8", "utf-8-lossy", "base64"]
    content: str
    content_digest: str | None = None


class CodeReadManyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[CodeReadResult]
    total_bytes: int = Field(ge=0)
    truncated: bool = False


class CodeGrepMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    line_number: int = Field(ge=1)
    line: str


class CodeGrepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["workspace", "audit_snapshot"]
    source_digest: str | None = None
    query: str
    matches: list[CodeGrepMatch]
    files_scanned: int = Field(ge=0)
    bytes_scanned: int = Field(ge=0)
    skipped_binary_files: int = Field(ge=0)
    skipped_large_files: int = Field(ge=0)
    truncated: bool = False


__all__ = [
    "CodeEntry",
    "CodeGrepMatch",
    "CodeGrepResult",
    "CodeListResult",
    "CodeReadManyResult",
    "CodeReadResult",
]
