"""Safe application-layer records and views for the Run Action read model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from riftx.domain import ApprovalLevel, ApprovalStatus, ExecutionStatus
from riftx.runtime.types import ToolCallStatus


class ActionLifecycle(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


class ActionCorrelationQuality(StrEnum):
    EXACT = "exact"
    LEGACY = "legacy"
    PARTIAL = "partial"


class ActionAttemptOrderQuality(StrEnum):
    EXACT = "exact"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class ActionStopConfirmation(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


class ActionPartialReason(StrEnum):
    """Stable, value-free reason codes safe to expose on the read API."""

    INTENT_SCOPE_MISMATCH = "intent_scope_mismatch"
    INTENT_STATUS_UNKNOWN = "intent_status_unknown"
    INTENT_APPROVAL_LEVEL_UNKNOWN = "intent_approval_level_unknown"
    APPROVAL_RUNTIME_MISSING = "approval_runtime_missing"
    APPROVAL_PUBLIC_MISSING = "approval_public_missing"
    APPROVAL_SHARED_ID_MISMATCH = "approval_shared_id_mismatch"
    APPROVAL_SCOPE_MISMATCH = "approval_scope_mismatch"
    APPROVAL_STATUS_UNKNOWN = "approval_status_unknown"
    APPROVAL_STATUS_MISMATCH = "approval_status_mismatch"
    APPROVAL_INTENT_STATUS_MISMATCH = "approval_intent_status_mismatch"
    APPROVAL_EXECUTION_STATUS_MISMATCH = "approval_execution_status_mismatch"
    APPROVAL_ACTOR_UNTRUSTED = "approval_actor_untrusted"
    APPROVAL_DECISION_TIME_MISMATCH = "approval_decision_time_mismatch"
    EXECUTION_SCOPE_MISMATCH = "execution_scope_mismatch"
    EXECUTION_SESSION_MISMATCH = "execution_session_mismatch"
    EXECUTION_STATUS_UNKNOWN = "execution_status_unknown"
    EXECUTION_ATTEMPT_ORDER_UNKNOWN = "execution_attempt_order_unknown"
    EXECUTION_ATTEMPT_ORDER_AMBIGUOUS = "execution_attempt_order_ambiguous"
    EXECUTION_ATTEMPTS_TRUNCATED = "execution_attempts_truncated"
    EXECUTION_CURRENT_AMBIGUOUS = "execution_current_ambiguous"
    EXECUTION_CURRENT_CORRELATION_PARTIAL = "execution_current_correlation_partial"
    EXECUTION_MISSING_FOR_INTENT_STATUS = "execution_missing_for_intent_status"
    EXECUTION_STOP_UNCONFIRMED = "execution_stop_unconfirmed"
    EXECUTION_STOP_PROOF_INVALID = "execution_stop_proof_invalid"
    INTENT_EXECUTION_STATUS_MISMATCH = "intent_execution_status_mismatch"
    ARTIFACT_SCOPE_MISMATCH = "artifact_scope_mismatch"
    FINDING_EVIDENCE_UNRESOLVED = "finding_evidence_unresolved"
    EVENT_CORRELATION_PARTIAL = "event_correlation_partial"
    REPOSITORY_PARTIAL_REASON_INVALID = "repository_partial_reason_invalid"


@dataclass(frozen=True, slots=True)
class ActionPageKey:
    created_at: datetime
    action_id: str

    def as_tuple(self) -> tuple[datetime, str]:
        return self.created_at, self.action_id


@dataclass(frozen=True, slots=True)
class ActionCoverage:
    scanned: int
    limit: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ActionIntentRead:
    """Allowlisted ToolCallIntent fields safe for application projection."""

    action_id: str
    run_id: str
    session_id: str
    cycle_id: str
    step_id: str
    engine_call_id: str | None
    tool_id: str | None
    skill_id: str | None
    reason: str
    target_summary: str | None
    approval_level: ApprovalLevel | str | None
    status: ToolCallStatus | str | None
    arguments: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ActionApprovalRead:
    """The two durable sides of the RuntimeApprovalRequest -> Approval bridge."""

    approval_id: str
    runtime_status: ApprovalStatus | str | None
    public_status: ApprovalStatus | str | None
    runtime_decided_by: str | None
    public_decided_by: str | None
    runtime_decided_at: datetime | None
    public_decided_at: datetime | None
    feedback: str | None
    bridge_correlation_quality: ActionCorrelationQuality
    bridge_partial_reasons: tuple[ActionPartialReason | str, ...]


@dataclass(frozen=True, slots=True)
class ActionExecutionRead:
    """Allowlisted execution metadata; never contains commands, paths, env, or output."""

    execution_id: str
    attempt_group: str | None
    node_id: str
    status: ExecutionStatus | str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    correlation_quality: ActionCorrelationQuality
    error_summary: str | None
    physical_stop_confirmed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ActionEventRead:
    """Allowlisted Event metadata; raw payload and text are intentionally absent."""

    event_id: str
    sequence: int
    event_type: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ActionResultRead:
    """Result metadata; output fields require explicit durable output proof."""

    artifact_ids: tuple[str, ...]
    artifact_count: int
    output_size: int
    output_available: bool
    artifacts_truncated: bool


@dataclass(frozen=True, slots=True)
class ActionAggregateRead:
    intent: ActionIntentRead
    approval: ActionApprovalRead | None
    executions: tuple[ActionExecutionRead, ...]
    current_execution_id: str | None
    execution_count: int
    execution_coverage: ActionCoverage
    result: ActionResultRead
    finding_ids: tuple[str, ...]
    finding_count: int
    events: tuple[ActionEventRead, ...]
    event_count: int
    finding_coverage: ActionCoverage
    event_coverage: ActionCoverage
    correlation_quality: ActionCorrelationQuality
    partial_reasons: tuple[ActionPartialReason | str, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ActionListIntentRead:
    """Text-minimal Intent fields needed by the Action timeline."""

    action_id: str
    run_id: str
    session_id: str
    cycle_id: str
    step_id: str
    engine_call_id: str | None
    tool_id: str | None
    skill_id: str | None
    reason: str
    target_summary: str | None
    approval_level: ApprovalLevel | str | None
    status: ToolCallStatus | str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ActionListApprovalRead:
    """Approval metadata without feedback or other historical text."""

    approval_id: str
    runtime_status: ApprovalStatus | str | None
    public_status: ApprovalStatus | str | None
    runtime_decided_by: str | None
    public_decided_by: str | None
    runtime_decided_at: datetime | None
    public_decided_at: datetime | None
    bridge_correlation_quality: ActionCorrelationQuality
    bridge_partial_reasons: tuple[ActionPartialReason | str, ...]


@dataclass(frozen=True, slots=True)
class ActionListExecutionRead:
    """Execution state needed for attempt selection, with no command/error text."""

    execution_id: str
    attempt_group: str | None
    node_id: str
    status: ExecutionStatus | str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    correlation_quality: ActionCorrelationQuality
    physical_stop_confirmed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ActionListResultRead:
    """List result metadata; never infer output fields from Artifact sizes."""

    artifact_ids: tuple[str, ...]
    artifact_count: int
    output_size: int
    output_available: bool
    artifacts_truncated: bool


@dataclass(frozen=True, slots=True)
class ActionListAggregateRead:
    """Persistence list contract that structurally cannot carry detail text."""

    intent: ActionListIntentRead
    approval: ActionListApprovalRead | None
    executions: tuple[ActionListExecutionRead, ...]
    current_execution_id: str | None
    execution_count: int
    execution_coverage: ActionCoverage
    result: ActionListResultRead
    finding_count: int
    event_count: int
    finding_coverage: ActionCoverage
    event_coverage: ActionCoverage
    updated_at: datetime
    correlation_quality: ActionCorrelationQuality
    partial_reasons: tuple[ActionPartialReason | str, ...]


@dataclass(frozen=True, slots=True)
class ActionReadPage:
    items: tuple[ActionListAggregateRead, ...]
    has_more: bool
    snapshot: ActionPageKey | None


class _ActionViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


_ACTION_GRAPH_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+~-]{0,511}")


class ActionGraphRef(_ActionViewModel):
    """Exact server-derived pointer to this Action's Task graph node."""

    view: Literal["task"]
    node_id: str = Field(min_length=1, max_length=512)
    projection_quality: Literal["exact"]

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if _ACTION_GRAPH_NODE_ID.fullmatch(value) is None or not value.startswith("action:"):
            raise ValueError("Action graph node ID is invalid")
        return value


class ActionApprovalView(_ActionViewModel):
    approval_id: str
    status: ApprovalStatus | None
    actor: str | None
    decided_at: datetime | None
    feedback_summary: str | None
    correlation_quality: ActionCorrelationQuality


class ActionExecutionView(_ActionViewModel):
    execution_id: str
    attempt_group: str | None
    node_id: str
    status: ExecutionStatus | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    error_summary: str | None
    correlation_quality: ActionCorrelationQuality
    physical_stop_confirmed_at: datetime | None
    stop_confirmation: ActionStopConfirmation | None


class ActionEventView(_ActionViewModel):
    event_id: str
    sequence: int
    event_type: str
    created_at: datetime


class ActionListAttemptView(_ActionViewModel):
    execution_id: str
    attempt_group: str | None
    node_id: str
    status: ExecutionStatus | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    correlation_quality: ActionCorrelationQuality
    physical_stop_confirmed_at: datetime | None
    stop_confirmation: ActionStopConfirmation | None


class ActionResultView(_ActionViewModel):
    truncated: bool
    artifact_ids: tuple[str, ...]
    artifact_count: int
    output_size: int
    output_available: bool


class ActionEvidenceView(_ActionViewModel):
    finding_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    events: tuple[ActionEventView, ...]
    finding_count: int
    event_count: int
    finding_coverage: ActionCoverage
    event_coverage: ActionCoverage


class RunActionView(_ActionViewModel):
    graph_ref: ActionGraphRef | None = None
    action_id: str
    run_id: str
    session_id: str
    cycle_id: str
    step_id: str
    engine_call_id: str | None
    tool_id: str | None
    skill_id: str | None
    reason: str
    target_summary: str | None
    approval_level: ApprovalLevel | None
    arguments_summary: dict[str, object]
    approval: ActionApprovalView | None
    executions: tuple[ActionExecutionView, ...]
    execution_count: int
    attempt_coverage: ActionCoverage
    latest_execution_id: str | None
    current_execution_id: str | None
    latest_stop_confirmation: ActionStopConfirmation | None
    current_stop_confirmation: ActionStopConfirmation | None
    attempt_order_quality: ActionAttemptOrderQuality
    result: ActionResultView
    evidence: ActionEvidenceView
    lifecycle: ActionLifecycle
    lifecycle_sources: tuple[str, ...]
    correlation_quality: ActionCorrelationQuality
    partial_reasons: tuple[ActionPartialReason, ...]
    created_at: datetime
    updated_at: datetime
    version: str


class RunActionListItemView(_ActionViewModel):
    graph_ref: ActionGraphRef | None = None
    action_id: str
    run_id: str
    session_id: str
    cycle_id: str
    step_id: str
    engine_call_id: str | None
    tool_id: str | None
    skill_id: str | None
    reason: str
    target_summary: str | None
    approval_level: ApprovalLevel | None
    approval_id: str | None
    approval_status: ApprovalStatus | None
    approval_actor: str | None
    approval_decided_at: datetime | None
    approval_correlation_quality: ActionCorrelationQuality | None
    execution_count: int
    attempts: tuple[ActionListAttemptView, ...]
    attempt_coverage: ActionCoverage
    latest_execution_id: str | None
    latest_execution_status: ExecutionStatus | None
    current_execution_id: str | None
    current_execution_status: ExecutionStatus | None
    latest_stop_confirmation: ActionStopConfirmation | None
    current_stop_confirmation: ActionStopConfirmation | None
    attempt_order_quality: ActionAttemptOrderQuality
    artifact_ids: tuple[str, ...]
    artifact_count: int
    artifacts_truncated: bool
    output_size: int
    output_available: bool
    finding_count: int
    event_count: int
    finding_coverage: ActionCoverage
    event_coverage: ActionCoverage
    lifecycle: ActionLifecycle
    lifecycle_sources: tuple[str, ...]
    correlation_quality: ActionCorrelationQuality
    partial_reasons: tuple[ActionPartialReason, ...]
    created_at: datetime
    updated_at: datetime
    version: str


class RunActionListView(_ActionViewModel):
    items: tuple[RunActionListItemView, ...]
    limit: int
    sort: str
    has_more: bool
    next_cursor: str | None


class InvalidActionCursorError(ValueError):
    code = "invalid_action_cursor"

    def __init__(self) -> None:
        super().__init__("The Action cursor is invalid for this Run and sort order")
