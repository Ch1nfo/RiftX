"""Human approval requests and per-Run grants."""

from __future__ import annotations

from pydantic import AwareDatetime, Field

from .base import DomainModel, new_id, utc_now
from .enums import ApprovalLevel, ApprovalMode, ApprovalStatus
from .errors import InvalidStateTransitionError


class Approval(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    tool_call_id: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    tool_name: str = ""
    command: list[str] = Field(default_factory=list)
    cwd: str = ""
    target_summary: str = ""
    env_diff: dict[str, str | None] = Field(default_factory=dict)
    reason: str = ""
    decided_by: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    decided_at: AwareDatetime | None = None

    def decide(
        self,
        status: ApprovalStatus,
        *,
        decided_by: str,
        reason: str | None = None,
        at: AwareDatetime | None = None,
    ) -> None:
        if self.status is not ApprovalStatus.PENDING or status not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.CANCELLED,
        }:
            raise InvalidStateTransitionError("Approval", self.status, status)
        self.status = status
        self.decided_by = decided_by
        if reason is not None:
            self.reason = reason
        self.decided_at = at or utc_now()


class ApprovalGrant(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    tool_id: str
    created_by: str
    created_at: AwareDatetime = Field(default_factory=utc_now)


def requires_approval(
    mode: ApprovalMode,
    level: ApprovalLevel,
    *,
    granted_for_run: bool = False,
) -> bool:
    """Apply the three documented approval modes to one effectful tool call."""

    if granted_for_run or mode is ApprovalMode.AUTO:
        return False
    if mode is ApprovalMode.MANUAL:
        return True
    return level in {ApprovalLevel.SENSITIVE, ApprovalLevel.ALWAYS}
