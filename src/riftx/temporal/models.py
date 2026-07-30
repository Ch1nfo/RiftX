"""Small JSON-safe payloads crossing the Temporal workflow boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class WorkflowPhase(StrEnum):
    PREPARING = "preparing"
    AGENT_CYCLE = "agent_cycle"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    PAUSED = "paused"
    COMPACTING = "compacting"
    REPORTING = "reporting"
    CLEANUP = "cleanup"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AgentCycleActivityStatus(StrEnum):
    CONTINUE = "continue"
    WAITING_APPROVAL = "waiting_approval"
    NEEDS_INPUT = "needs_input"
    COMPLETED = "completed"


@dataclass
class PendingApproval:
    call_id: str
    tool_name: str
    arguments: str | None = None


@dataclass
class RunWorkflowInput:
    run_id: str


@dataclass
class PrepareRunInput:
    run_id: str


@dataclass
class PrepareRunResult:
    run_id: str
    prepared: bool = True


@dataclass
class AgentCycleActivityInput:
    run_id: str
    agent_step_id: str
    checkpoint_id: str | None = None
    approval_decisions: dict[str, bool] = field(default_factory=dict)
    user_messages: list[str] = field(default_factory=list)
    cancel_current_execution: bool = False


@dataclass
class AgentCycleActivityResult:
    status: AgentCycleActivityStatus
    checkpoint_id: str | None = None
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    active_execution_id: str | None = None
    summary: str | None = None


@dataclass
class CompactContextInput:
    run_id: str
    max_history_items: int = 100


@dataclass
class CompactContextResult:
    compacted: bool
    retained_items: int


@dataclass
class GenerateReportInput:
    run_id: str


@dataclass
class GenerateReportResult:
    report_id: str | None = None


@dataclass
class CleanupRunInput:
    run_id: str
    final_status: str


@dataclass
class CleanupRunResult:
    cleaned: bool = True


@dataclass
class RunWorkflowStatus:
    run_id: str
    phase: WorkflowPhase
    paused: bool
    finished: bool
    checkpoint_id: str | None = None
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    active_execution_id: str | None = None
    queued_user_messages: int = 0
    cancel_current_execution_requested: bool = False
    cancel_requested: bool = False
    compact_requested: bool = False


@dataclass
class RunWorkflowResult:
    run_id: str
    phase: WorkflowPhase
    report_id: str | None = None
