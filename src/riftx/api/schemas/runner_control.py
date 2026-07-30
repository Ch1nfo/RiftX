"""Schemas for the authenticated remote Runner control channel."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import ExecutionStatus, RunnerCommand, RunnerCommandKind, RunnerCommandStatus


class RunnerCommandResponse(BaseModel):
    id: str
    node_id: str
    kind: RunnerCommandKind
    payload: dict[str, object]
    status: RunnerCommandStatus
    attempts: int
    lease_id: str
    lease_expires_at: datetime

    @classmethod
    def from_domain(cls, command: RunnerCommand) -> "RunnerCommandResponse":
        if command.lease_id is None or command.lease_expires_at is None:
            raise ValueError("leased command is missing lease metadata")
        return cls(
            id=command.id,
            node_id=command.node_id,
            kind=command.kind,
            payload=command.payload,
            status=command.status,
            attempts=command.attempts,
            lease_id=command.lease_id,
            lease_expires_at=command.lease_expires_at,
        )


class RunnerPollResponse(BaseModel):
    command: RunnerCommandResponse | None = None


class FinishRunnerCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(min_length=1, max_length=64)
    succeeded: bool
    result: dict[str, object] = Field(default_factory=dict)
    error: str = Field(default="", max_length=8192)


class FinishRunnerCommandResponse(BaseModel):
    id: str
    status: RunnerCommandStatus
    completed_at: datetime | None


class ExecutionStatusReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    pid: int | None = Field(default=None, gt=0)
    process_group_id: int | None = Field(default=None, gt=0)
    exit_code: int | None = None
    executable_path: str | None = None
    tool_id: str | None = None
    tool_version: str | None = None
    platform_system: str = ""
    platform_release: str = ""
    platform_architecture: str = ""
    process_created_at: datetime | None = None


class ExecutionOutputReportRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    stream: str = Field(pattern="^(stdout|stderr)$")
    offset: int = Field(ge=0)
    data: bytes = Field(max_length=256 * 1024)


class ExecutionOutputReportResponse(BaseModel):
    next_offset: int


class RunnerCommandOutputReportRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    lease_id: str = Field(min_length=1, max_length=64)
    offset: int = Field(ge=0)
    data: bytes = Field(max_length=256 * 1024)
