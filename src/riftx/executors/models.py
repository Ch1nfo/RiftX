"""Executor request and result models."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from riftx.domain import ExecutionStatus


class EnvironmentMode(StrEnum):
    INHERIT = "inherit"
    CLEAN = "clean"


class ShellKind(StrEnum):
    BASH = "bash"
    ZSH = "zsh"
    POWERSHELL = "powershell"
    CMD = "cmd"


class ProcessExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_key: str = Field(min_length=1)
    argv: list[str]
    cwd: Path
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    stdout_path: Path
    stderr_path: Path

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, argv: list[str]) -> list[str]:
        if not argv or any(not item for item in argv):
            raise ValueError("argv must contain non-empty elements")
        return argv

    @model_validator(mode="after")
    def validate_paths(self) -> ProcessExecutionRequest:
        if not self.cwd.is_dir():
            raise ValueError(f"cwd does not exist or is not a directory: {self.cwd}")
        return self


class ShellExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_key: str = Field(min_length=1)
    script: str = Field(min_length=1)
    shell: ShellKind
    cwd: Path
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    stdout_path: Path
    stderr_path: Path
    shell_path: Path | None = None


class ProcessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    exit_code: int | None
    timed_out: bool = False
