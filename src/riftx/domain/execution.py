"""Agent steps, tool calls, executions, and terminal sessions."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import AwareDatetime, Field

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

_EXECUTION_TRANSITIONS: Mapping[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.CREATED: frozenset({ExecutionStatus.STARTING, ExecutionStatus.CANCELLED}),
    ExecutionStatus.STARTING: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.LOST,
        }
    ),
    ExecutionStatus.EXITED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
    ExecutionStatus.LOST: frozenset(),
}

_TERMINAL_TRANSITIONS: Mapping[TerminalStatus, frozenset[TerminalStatus]] = {
    TerminalStatus.CREATED: frozenset({TerminalStatus.OPEN, TerminalStatus.CLOSED}),
    TerminalStatus.OPEN: frozenset({TerminalStatus.CLOSED, TerminalStatus.LOST}),
    TerminalStatus.CLOSED: frozenset(),
    TerminalStatus.LOST: frozenset(),
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
    run_id: str
    node_id: str
    executor_type: ExecutorType
    argv: list[str] = Field(default_factory=list)
    command_text: str | None = None
    cwd: str
    env_diff: dict[str, str | None] = Field(default_factory=dict)
    status: ExecutionStatus = ExecutionStatus.CREATED
    pid: int | None = Field(default=None, gt=0)
    process_group_id: int | None = Field(default=None, gt=0)
    exit_code: int | None = None
    stdout_path: str
    stderr_path: str
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

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
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
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
    status: TerminalStatus = TerminalStatus.CREATED
    owner: TerminalOwner = TerminalOwner.AGENT
    cols: int = Field(default=120, gt=0)
    rows: int = Field(default=40, gt=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    closed_at: AwareDatetime | None = None

    def transition_to(self, target: TerminalStatus, *, at: AwareDatetime | None = None) -> None:
        if target not in _TERMINAL_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError("TerminalSession", self.status, target)
        self.status = target
        if target in {TerminalStatus.CLOSED, TerminalStatus.LOST}:
            self.closed_at = at or utc_now()
