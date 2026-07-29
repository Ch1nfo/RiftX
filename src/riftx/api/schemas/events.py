"""Run event timeline schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from riftx.domain import RunEvent


class RunEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    sequence: int
    event_type: str
    payload: dict[str, object]
    created_at: datetime

    @classmethod
    def from_domain(cls, event: RunEvent) -> "RunEventResponse":
        return cls.model_validate(event)


class RunEventListResponse(BaseModel):
    items: list[RunEventResponse]
    after_sequence: int
