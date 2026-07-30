"""Runner-facing request and output models."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from riftx.domain import ExecutorType, TerminalOwner
from riftx.executors import EnvironmentMode, ShellKind


class ExecutionLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_key: str = Field(min_length=1)
    execution_id: str | None = Field(default=None, min_length=1)
    run_id: str = Field(min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    tool_call_id: str | None = Field(default=None, min_length=1)
    attempt_group: str | None = Field(default=None, min_length=1)
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

    @model_validator(mode="after")
    def validate_executor_payload(self) -> ExecutionLaunchRequest:
        if not self.cwd.is_dir():
            raise ValueError(f"cwd does not exist or is not a directory: {self.cwd}")
        if self.executor_type is ExecutorType.PROCESS:
            if not self.argv or any(not item for item in self.argv):
                raise ValueError("process execution requires non-empty argv")
            if self.command_text is not None or self.shell is not None:
                raise ValueError("process execution cannot include shell fields")
        elif self.executor_type is ExecutorType.SHELL:
            if not self.command_text or self.shell is None:
                raise ValueError("shell execution requires command_text and shell")
            if self.argv:
                raise ValueError("shell execution builds argv from command_text")
        else:
            raise ValueError("PTY execution is handled by the terminal subsystem")
        return self


class OutputSlice(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    data: bytes
    cursor: int = Field(ge=0)
    next_cursor: int = Field(ge=0)
    eof: bool


class ExecutionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stdout: OutputSlice
    stderr: OutputSlice


class TerminalLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, min_length=1)
    execution_id: str | None = Field(default=None, min_length=1)
    run_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    cwd: Path
    argv: list[str]
    tool_id: str | None = Field(default=None, min_length=1)
    tool_version: str | None = None
    environment_mode: EnvironmentMode = EnvironmentMode.INHERIT
    env: dict[str, str | None] = Field(default_factory=dict)
    cols: int = Field(default=120, gt=0, le=1000)
    rows: int = Field(default=40, gt=0, le=1000)
    owner: TerminalOwner = TerminalOwner.AGENT

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> TerminalLaunchRequest:
        if not self.cwd.is_dir():
            raise ValueError(f"cwd does not exist or is not a directory: {self.cwd}")
        if not self.argv or any(not item for item in self.argv):
            raise ValueError("terminal execution requires non-empty argv")
        return self
