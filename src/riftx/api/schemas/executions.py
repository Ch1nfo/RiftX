"""Public execution inspection and output schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from riftx.domain import Execution, ExecutionStatus, ExecutorType
from riftx.runner import ExecutionOutput, OutputSlice


class ExecutionResponse(BaseModel):
    id: str
    execution_key: str
    run_id: str
    session_id: str | None
    tool_call_id: str | None
    attempt_group: str | None
    node_id: str
    executor_type: ExecutorType
    argv: list[str]
    command_text: str | None
    tool_id: str | None
    tool_version: str | None
    executable_path: str | None
    cwd: str
    env_diff: dict[str, str | None]
    platform_system: str
    platform_release: str
    platform_architecture: str
    status: ExecutionStatus
    pid: int | None
    process_group_id: int | None
    exit_code: int | None
    stdout_path: str
    stderr_path: str
    process_created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_domain(cls, execution: Execution) -> "ExecutionResponse":
        return cls.model_validate(execution.model_dump())


class ExecutionListResponse(BaseModel):
    items: list[ExecutionResponse]
    limit: int
    offset: int


class ExecutionOutputSliceResponse(BaseModel):
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    data: bytes
    cursor: int
    next_cursor: int
    eof: bool

    @classmethod
    def from_domain(cls, output: OutputSlice) -> "ExecutionOutputSliceResponse":
        return cls.model_validate(output.model_dump())


class ExecutionOutputResponse(BaseModel):
    stdout: ExecutionOutputSliceResponse
    stderr: ExecutionOutputSliceResponse

    @classmethod
    def from_domain(cls, output: ExecutionOutput) -> "ExecutionOutputResponse":
        return cls(
            stdout=ExecutionOutputSliceResponse.from_domain(output.stdout),
            stderr=ExecutionOutputSliceResponse.from_domain(output.stderr),
        )
