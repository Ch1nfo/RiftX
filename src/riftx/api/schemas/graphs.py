"""Strict query and response schemas for Run-scoped Graph views."""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator

from riftx.application.graphs import GraphViewKind, GraphViewPage

_TYPE_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_GRAPH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+~-]{0,511}")


class GraphViewQuery(BaseModel):
    """The complete allowlist of client-controlled Graph query fields."""

    model_config = ConfigDict(extra="forbid")

    view: GraphViewKind
    node_type: str | None = Field(default=None, max_length=64)
    edge_type: str | None = Field(default=None, max_length=64)
    focus: str | None = Field(default=None, max_length=512)
    search: str | None = Field(default=None, min_length=1, max_length=256)
    limit: int = Field(default=100, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)

    @field_validator("node_type", "edge_type")
    @classmethod
    def validate_type_token(cls, value: str | None) -> str | None:
        if value is not None and _TYPE_TOKEN.fullmatch(value) is None:
            raise ValueError("Graph filter is invalid")
        return value

    @field_validator("focus")
    @classmethod
    def validate_focus(cls, value: str | None) -> str | None:
        if value is not None and (_GRAPH_ID.fullmatch(value) is None or _has_unsafe_unicode(value)):
            raise ValueError("Graph filter is invalid")
        return value

    @field_validator("search")
    @classmethod
    def validate_search(cls, value: str | None) -> str | None:
        if value is not None and _has_unsafe_unicode(value):
            raise ValueError("Graph filter is invalid")
        return value


def _has_unsafe_unicode(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


__all__ = ["GraphViewPage", "GraphViewQuery"]
