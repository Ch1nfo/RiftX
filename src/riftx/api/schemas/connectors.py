"""Unified browser/Burp connector API schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from riftx.connectors import ConnectorHttpCapture, ConnectorReceipt

from .runs import CreateRunRequest


class ConnectorSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture: ConnectorHttpCapture
    run_id: str | None = Field(default=None, min_length=1)
    new_run: CreateRunRequest | None = None

    @model_validator(mode="after")
    def validate_target(self) -> ConnectorSubmissionRequest:
        if (self.run_id is None) == (self.new_run is None):
            raise ValueError("provide exactly one of run_id or new_run")
        return self


class ConnectorReceiptResponse(BaseModel):
    receipt: ConnectorReceipt


class ConnectorWebUIResponse(BaseModel):
    run_id: str
    url: str
