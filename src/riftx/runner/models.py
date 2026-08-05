"""Runner-facing request and output models."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from riftx.domain import ExecutorType, RunnerPrincipal, TerminalOwner
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
    # Local launches leave this unset. Remote admission binds the exact Runner
    # generation before dispatch so a cloned node ID cannot adopt the effect.
    runner_principal: RunnerPrincipal | None = None
    # The Control Plane does not place these facts in the immutable command
    # payload because the command ID is allocated during enqueue.  The Runner
    # daemon injects the leased command's verified ownership immediately
    # before local durable admission.  They are intentionally excluded from
    # ``launch_fingerprint``: the fingerprint describes launch semantics,
    # while these fields prove which immutable transport envelope admitted it.
    runner_command_id: str | None = Field(default=None, min_length=1, max_length=64)
    runner_effect_binding_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    runner_binding_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    runner_envelope_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
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
        _validate_runner_callback_binding(self)
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

    @property
    def launch_fingerprint(self) -> str:
        return _launch_fingerprint(
            "execution",
            {
                "execution_id": self.execution_id,
                "execution_key": self.execution_key,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "tool_call_id": self.tool_call_id,
                "attempt_group": self.attempt_group,
                "node_id": self.node_id,
                "executor_type": self.executor_type.value,
                "argv": self.argv,
                "command_text": self.command_text,
                "tool_id": self.tool_id,
                "tool_version": self.tool_version,
                "cwd": _canonical_path(self.cwd),
                "environment_mode": self.environment_mode.value,
                "env": self.env,
                "shell": self.shell.value if self.shell is not None else None,
                "shell_path": (
                    _canonical_path(self.shell_path) if self.shell_path is not None else None
                ),
                "timeout_seconds": self.timeout_seconds,
            },
        )


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
    execution_key: str | None = Field(default=None, min_length=1, max_length=255)
    agent_session_id: str | None = Field(default=None, min_length=1)
    tool_call_id: str | None = Field(default=None, min_length=1)
    attempt_group: str | None = Field(default=None, min_length=1, max_length=64)
    run_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    runner_principal: RunnerPrincipal | None = None
    runner_command_id: str | None = Field(default=None, min_length=1, max_length=64)
    runner_effect_binding_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    runner_binding_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    runner_envelope_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
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
        _validate_runner_callback_binding(self)
        if not self.cwd.is_dir():
            raise ValueError(f"cwd does not exist or is not a directory: {self.cwd}")
        if not self.argv or any(not item for item in self.argv):
            raise ValueError("terminal execution requires non-empty argv")
        if (self.session_id is None) != (self.execution_id is None):
            raise ValueError("terminal session_id and execution_id must be supplied together")
        if self.tool_call_id is not None and (
            self.execution_key is None
            or self.attempt_group is None
            or self.agent_session_id is None
        ):
            raise ValueError(
                "tool-bound terminal execution requires agent_session_id, "
                "execution_key, and attempt_group"
            )
        if self.tool_call_id is None and self.attempt_group is not None:
            raise ValueError("terminal attempt_group requires tool_call_id")
        return self

    @property
    def launch_fingerprint(self) -> str:
        return _launch_fingerprint(
            "terminal",
            {
                "session_id": self.session_id,
                "execution_id": self.execution_id,
                "execution_key": self.execution_key,
                "run_id": self.run_id,
                "agent_session_id": self.agent_session_id,
                "tool_call_id": self.tool_call_id,
                "attempt_group": self.attempt_group,
                "node_id": self.node_id,
                "argv": self.argv,
                "tool_id": self.tool_id,
                "tool_version": self.tool_version,
                "cwd": _canonical_path(self.cwd),
                "environment_mode": self.environment_mode.value,
                "env": self.env,
                "cols": self.cols,
                "rows": self.rows,
                "owner": self.owner.value,
            },
        )


def _canonical_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def _validate_runner_callback_binding(
    request: ExecutionLaunchRequest | TerminalLaunchRequest,
) -> None:
    binding = (
        request.runner_command_id,
        request.runner_effect_binding_id,
        request.runner_binding_digest,
        request.runner_envelope_digest,
    )
    if any(item is None for item in binding) and any(item is not None for item in binding):
        raise ValueError("Runner callback binding must be all absent or all present")


def _launch_fingerprint(kind: str, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        {"kind": kind, **payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"launch:v1:{hashlib.sha256(canonical.encode()).hexdigest()}"
