"""Finding read schemas."""

from pydantic import BaseModel

from riftx.domain import Finding


class FindingListResponse(BaseModel):
    items: list[Finding]
    limit: int
    offset: int
