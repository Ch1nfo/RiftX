"""Execution submission models and deterministic idempotency keys."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import Execution, ExecutorType
from riftx.executors import EnvironmentMode, ShellKind
from riftx.runner import ExecutionLaunchRequest


class SubmitExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    attempt_group: str = Field(default="initial", min_length=1, max_length=64)
    node_id: str = Field(min_length=1)
    executor_type: ExecutorType
    cwd: Path
    argv: list[str] = Field(default_factory=list)
    command_text: str | None = None
    tool_id: str | None = Field(default=None, min_length=1)
    tool_version: str | None = None
    shell: ShellKind | None = None
    shell_path: Path | None = None
    environment_mode: EnvironmentMode = EnvironmentMode.INHERIT
    env: dict[str, str | None] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)

    @property
    def execution_key(self) -> str:
        return build_execution_key(
            run_id=self.run_id,
            session_id=self.session_id,
            tool_call_id=self.tool_call_id,
            attempt_group=self.attempt_group,
        )

    def to_launch_request(self) -> ExecutionLaunchRequest:
        return ExecutionLaunchRequest(
            execution_key=self.execution_key,
            run_id=self.run_id,
            session_id=self.session_id,
            tool_call_id=self.tool_call_id,
            attempt_group=self.attempt_group,
            node_id=self.node_id,
            executor_type=self.executor_type,
            cwd=self.cwd,
            argv=self.argv,
            command_text=self.command_text,
            tool_id=self.tool_id,
            tool_version=self.tool_version,
            shell=self.shell,
            shell_path=self.shell_path,
            environment_mode=self.environment_mode,
            env=self.env,
            timeout_seconds=self.timeout_seconds,
        )


def build_execution_key(
    *,
    run_id: str,
    session_id: str,
    tool_call_id: str,
    attempt_group: str,
) -> str:
    """Build a bounded key from the complete logical execution identity."""

    identity = "\x1f".join((run_id, session_id, tool_call_id, attempt_group))
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"execution:v1:{digest}"


class ExecutionWaitStatus(StrEnum):
    """Stable outcome of waiting, distinct from the Execution lifecycle state."""

    WAIT_TIMEOUT = "wait_timeout"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_CANCELLED = "execution_cancelled"
    EXECUTION_LOST = "execution_lost"


class ExecutionWaitResult(BaseModel):
    """Bounded wait result returned to Runtime, API, CLI, and Agent tools."""

    execution: Execution
    wait_status: ExecutionWaitStatus
    partial_output: str | None = None
    next_poll_after_seconds: int | None = Field(default=None, gt=0)
    stdout_cursor: int = Field(default=0, ge=0)
    stderr_cursor: int = Field(default=0, ge=0)
