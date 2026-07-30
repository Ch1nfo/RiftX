"""Stable Runtime Hook request, result, and audit contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from riftx.domain.base import DomainModel, new_id, utc_now


class HookPoint(StrEnum):
    BEFORE_CONTEXT_COMPILE = "before_context_compile"
    AFTER_CONTEXT_COMPILE = "after_context_compile"
    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_CALL = "after_model_call"
    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    AFTER_TOOL_EXECUTION = "after_tool_execution"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"
    MEMORY_CANDIDATE = "memory_candidate"
    MEMORY_WRITTEN = "memory_written"
    TERMINAL_OPEN = "terminal_open"
    TERMINAL_OWNER_CHANGED = "terminal_owner_changed"
    TERMINAL_CLOSE = "terminal_close"


class HookDecision(StrEnum):
    CONTINUE = "continue"
    MODIFY = "modify"
    BLOCK = "block"
    REQUIRE_APPROVAL = "require_approval"
    ABSTAIN = "abstain"


class HookFailurePolicy(StrEnum):
    WARN = "warn"
    BLOCK = "block"


class HookRequest(DomainModel):
    id: str = Field(default_factory=new_id)
    point: HookPoint
    run_id: str = Field(min_length=1)
    session_id: str | None = None
    cycle_id: str | None = None
    step_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class HookResult(DomainModel):
    decision: HookDecision = HookDecision.ABSTAIN
    modified_payload: dict[str, object] | None = None
    additional_context: str | None = Field(default=None, max_length=8192)
    reason: str | None = None
    emitted_events: list[dict[str, object]] = Field(default_factory=list)


class HookAuditRecord(DomainModel):
    id: str = Field(default_factory=new_id)
    request_id: str = Field(min_length=1)
    hook_id: str = Field(min_length=1)
    point: HookPoint
    run_id: str = Field(min_length=1)
    decision: HookDecision
    priority: int
    duration_ms: float = Field(ge=0)
    input_digest: str = Field(min_length=1)
    output_digest: str | None = None
    modified_fields: list[str] = Field(default_factory=list)
    reason: str | None = None
    error: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)


class HookDispatchResult(DomainModel):
    decision: HookDecision
    payload: dict[str, object]
    additional_context: list[str] = Field(default_factory=list)
    emitted_events: list[dict[str, object]] = Field(default_factory=list)
    audits: list[HookAuditRecord] = Field(default_factory=list)
