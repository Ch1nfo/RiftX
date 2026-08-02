"""Strict API schemas for Target HTTP metadata History/Inspector."""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator

from riftx.application.traffic import (
    TrafficExchangeDetail,
    TrafficExchangePage,
    TrafficStatusClass,
)

_METHOD = re.compile(r"[A-Z][A-Z-]{0,31}")
_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,4096}")


class TrafficExchangeListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str | None = Field(default=None, max_length=32)
    status_class: TrafficStatusClass | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)

    @field_validator("method")
    @classmethod
    def validate_method(cls, value: str | None) -> str | None:
        if value is not None and _METHOD.fullmatch(value) is None:
            raise ValueError("Traffic method filter is invalid")
        return value

    @field_validator("cursor")
    @classmethod
    def validate_cursor(cls, value: str | None) -> str | None:
        if value is not None and _CURSOR.fullmatch(value) is None:
            raise ValueError("Traffic cursor is invalid")
        return value


def validate_exchange_id(value: str) -> str:
    if (
        not value
        or len(value) > 256
        or value != value.strip()
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("Traffic exchange identity is invalid")
    return value


__all__ = [
    "TrafficExchangeDetail",
    "TrafficExchangeListQuery",
    "TrafficExchangePage",
    "validate_exchange_id",
]
