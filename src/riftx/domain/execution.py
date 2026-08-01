"""Agent steps, tool calls, executions, and terminal sessions."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import AwareDatetime, Field, model_validator

from .base import DomainModel, new_id, utc_now
from .enums import (
    AgentStepStatus,
    ApprovalStatus,
    ExecutionStatus,
    ExecutorType,
    TerminalOwner,
    TerminalStatus,
)
from .errors import InvalidStateTransitionError
from .runner import RunnerPrincipal

_EXECUTION_TRANSITIONS: Mapping[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.QUEUED: frozenset({ExecutionStatus.STARTING, ExecutionStatus.CANCELLED}),
    ExecutionStatus.CREATED: frozenset({ExecutionStatus.STARTING, ExecutionStatus.CANCELLED}),
    ExecutionStatus.STARTING: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.COMPLETED: frozenset(),
    ExecutionStatus.EXITED: frozenset(),
    # FAILED describes the execution result, not physical-stop evidence.  A
    # later safety cancellation may inspect the owning Runner's local process
    # and converge to CANCELLED only after absence/termination is confirmed.
    ExecutionStatus.FAILED: frozenset({ExecutionStatus.CANCELLED}),
    ExecutionStatus.CANCELLED: frozenset(),
    ExecutionStatus.HARD_TIMEOUT: frozenset(),
    # LOST records are intentionally allowed to converge to CANCELLED after a
    # Runner reconnects and acknowledges the durable cancellation tombstone.
    ExecutionStatus.LOST: frozenset({ExecutionStatus.CANCELLED}),
}

_TERMINAL_TRANSITIONS: Mapping[TerminalStatus, frozenset[TerminalStatus]] = {
    TerminalStatus.CREATED: frozenset(
        {TerminalStatus.OPEN, TerminalStatus.CLOSED, TerminalStatus.LOST}
    ),
    TerminalStatus.OPEN: frozenset({TerminalStatus.CLOSED, TerminalStatus.LOST}),
    TerminalStatus.CLOSED: frozenset(),
    # A restarted Runner may later use durable kernel-containment identity to
    # prove a previously LOST native terminal stopped and converge it CLOSED.
    TerminalStatus.LOST: frozenset({TerminalStatus.CLOSED}),
}

_PHYSICAL_STOP_PROOF_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
}


class AgentStep(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    sequence: int = Field(ge=1)
    status: AgentStepStatus = AgentStepStatus.CREATED
    summary: str = ""
    created_at: AwareDatetime = Field(default_factory=utc_now)
    completed_at: AwareDatetime | None = None


class ToolCall(DomainModel):
    id: str = Field(default_factory=new_id)
    sdk_call_id: str = Field(min_length=1)
    run_id: str
    agent_step_id: str
    tool_id: str
    skill_id: str | None = None
    arguments: dict[str, object] = Field(default_factory=dict)
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    execution_id: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)


class Execution(DomainModel):
    id: str = Field(default_factory=new_id)
    execution_key: str = Field(min_length=1)
    launch_fingerprint: str | None = Field(default=None, min_length=1, max_length=80)
    run_id: str
    session_id: str | None = None
    tool_call_id: str | None = None
    attempt_group: str | None = None
    node_id: str
    # Local and pre-fencing legacy executions have no remote owner. Remote
    # admission will bind this before dispatch in the Phase 2 wiring.
    owner: RunnerPrincipal | None = None
    executor_type: ExecutorType
    argv: list[str] = Field(default_factory=list)
    command_text: str | None = None
    tool_id: str | None = None
    tool_version: str | None = None
    executable_path: str | None = None
    cwd: str
    env_diff: dict[str, str | None] = Field(default_factory=dict)
    platform_system: str = ""
    platform_release: str = ""
    platform_architecture: str = ""
    status: ExecutionStatus = ExecutionStatus.CREATED
    pid: int | None = Field(default=None, gt=0)
    process_group_id: int | None = Field(default=None, gt=0)
    containment_id: str | None = Field(default=None, min_length=1, max_length=255)
    exit_code: int | None = None
    stdout_path: str
    stderr_path: str
    # New executions persist this immutable admission timestamp. Legacy rows
    # intentionally remain NULL because their chronological order cannot be
    # reconstructed safely from process or finish timestamps.
    created_at: AwareDatetime | None = Field(default_factory=utc_now, frozen=True)
    process_created_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    physical_stop_confirmed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_physical_stop_proof(self) -> Execution:
        if (
            self.physical_stop_confirmed_at is not None
            and self.status not in _PHYSICAL_STOP_PROOF_STATUSES
        ):
            raise ValueError(
                "physical stop proof requires completed, exited, cancelled, "
                "or hard-timeout execution status"
            )
        return self

    def transition_to(
        self,
        target: ExecutionStatus,
        *,
        at: AwareDatetime | None = None,
        exit_code: int | None = None,
    ) -> None:
        if target not in _EXECUTION_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError("Execution", self.status, target)

        changed_at = at or utc_now()
        self.status = target
        if target is ExecutionStatus.RUNNING and self.started_at is None:
            self.started_at = changed_at
        if target in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
            ExecutionStatus.LOST,
        }:
            self.finished_at = changed_at
            self.exit_code = exit_code

    def can_transition_to(self, target: ExecutionStatus) -> bool:
        return target in _EXECUTION_TRANSITIONS[self.status]


class TerminalSession(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str
    execution_id: str
    runner_id: str = ""
    shell: str = ""
    cwd: str = ""
    status: TerminalStatus = TerminalStatus.CREATED
    owner: TerminalOwner = TerminalOwner.AGENT
    cols: int = Field(default=120, gt=0)
    rows: int = Field(default=40, gt=0)
    output_cursor: int = Field(default=0, ge=0)
    takeover_cursor: int | None = Field(default=None, ge=0)
    takeover_started_at: AwareDatetime | None = None
    transcript_artifact_id: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    closed_at: AwareDatetime | None = None

    def transition_to(self, target: TerminalStatus, *, at: AwareDatetime | None = None) -> None:
        if target not in _TERMINAL_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError("TerminalSession", self.status, target)
        self.status = target
        if target in {TerminalStatus.CLOSED, TerminalStatus.LOST}:
            self.closed_at = at or utc_now()

    def take_over(self, *, cursor: int | None = None) -> None:
        if self.status is not TerminalStatus.OPEN:
            raise InvalidStateTransitionError("TerminalSession", self.status, TerminalOwner.USER)
        self.owner = TerminalOwner.USER
        self.takeover_cursor = self.output_cursor if cursor is None else cursor
        self.takeover_started_at = utc_now()

    def release(self, *, cursor: int | None = None) -> None:
        if self.status is not TerminalStatus.OPEN:
            raise InvalidStateTransitionError("TerminalSession", self.status, TerminalOwner.AGENT)
        self.owner = TerminalOwner.AGENT
        if cursor is not None:
            self.output_cursor = cursor
        self.takeover_cursor = None
        self.takeover_started_at = None

    def resize(self, cols: int, rows: int) -> None:
        if self.status is not TerminalStatus.OPEN:
            raise InvalidStateTransitionError("TerminalSession", self.status, "resize")
        if cols < 1 or rows < 1:
            raise ValueError("terminal dimensions must be positive")
        self.cols = cols
        self.rows = rows


class TerminalTakeoverSummary(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    terminal_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    started_cursor: int = Field(ge=0)
    ended_cursor: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    artifact_id: str = Field(min_length=1)
    summary: str
    takeover_started_at: AwareDatetime | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_cursors(self) -> TerminalTakeoverSummary:
        if self.ended_cursor < self.started_cursor:
            raise ValueError("takeover summary ended cursor precedes its start")
        if self.byte_count != self.ended_cursor - self.started_cursor:
            raise ValueError("takeover summary byte count does not match its cursor range")
        return self
