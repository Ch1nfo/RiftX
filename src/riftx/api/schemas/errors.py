"""Stable API error envelope."""

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, object] | list[object] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail
