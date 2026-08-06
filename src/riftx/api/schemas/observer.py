"""Observer Projector HTTP schemas."""

from pydantic import BaseModel, ConfigDict, Field

from riftx.observer import ObserverProjection


class ObserverProjectionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_limit: int = Field(default=100, ge=1, le=100)
    timeline_limit: int = Field(default=100, ge=1, le=1_000)


__all__ = ["ObserverProjection", "ObserverProjectionQuery"]
