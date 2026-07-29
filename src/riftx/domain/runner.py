"""Durable remote Runner identity and command models."""

from pydantic import AwareDatetime, Field

from .base import DomainModel, new_id, utc_now
from .enums import RunnerCommandKind, RunnerCommandStatus


class RunnerCredential(DomainModel):
    node_id: str = Field(min_length=1, max_length=64)
    token_hash: str = Field(min_length=64, max_length=64)
    token_prefix: str = Field(min_length=1, max_length=16)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    rotated_at: AwareDatetime = Field(default_factory=utc_now)
    revoked_at: AwareDatetime | None = None


class RunnerCommand(DomainModel):
    id: str = Field(default_factory=new_id)
    node_id: str = Field(min_length=1, max_length=64)
    kind: RunnerCommandKind
    idempotency_key: str = Field(min_length=1, max_length=255)
    payload: dict[str, object] = Field(default_factory=dict)
    status: RunnerCommandStatus = RunnerCommandStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    lease_id: str | None = None
    lease_expires_at: AwareDatetime | None = None
    result: dict[str, object] = Field(default_factory=dict)
    error: str = ""
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    completed_at: AwareDatetime | None = None
