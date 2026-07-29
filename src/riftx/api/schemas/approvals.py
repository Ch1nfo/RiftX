"""Approval request and decision schemas."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, Field

from riftx.domain import Approval, ApprovalStatus


class ApprovalResponse(BaseModel):
    id: str
    run_id: str
    tool_call_id: str
    status: ApprovalStatus
    tool_name: str
    command: list[str]
    cwd: str
    target_summary: str
    env_diff: dict[str, str | None]
    reason: str
    decided_by: str | None
    created_at: AwareDatetime
    decided_at: AwareDatetime | None

    @classmethod
    def from_domain(cls, approval: Approval) -> ApprovalResponse:
        return cls.model_validate(approval.model_dump())


class ApprovalListResponse(BaseModel):
    items: list[ApprovalResponse]


class ApprovalDecisionRequest(BaseModel):
    decided_by: str = Field(default="local-user", min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=4000)
    approve_for_run: bool = False
