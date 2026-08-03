"""Public execution inspection and output schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from riftx.domain import Execution, ExecutionStatus, ExecutorType
from riftx.execution import ExecutionWaitResult, ExecutionWaitStatus
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
    containment_id: str | None
    exit_code: int | None
    stdout_path: str
    stderr_path: str
    process_created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    physical_stop_confirmed_at: datetime | None

    @classmethod
    def from_domain(cls, execution: Execution) -> "ExecutionResponse":
        return cls.model_validate(execution.model_dump())


class CodeAuditExecutionResponse(BaseModel):
    """Positive allowlist for Audit-owned generic Execution reads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["code_audit"] = "code_audit"
    id: str
    run_id: str
    node_id: str
    executor_type: ExecutorType
    tool_id: str | None
    tool_version: str | None
    status: ExecutionStatus
    exit_code: int | None
    started_at: datetime | None
    finished_at: datetime | None
    physical_stop_confirmed_at: datetime | None

    @classmethod
    def from_domain(cls, execution: Execution) -> "CodeAuditExecutionResponse":
        return cls(
            id=execution.id,
            run_id=execution.run_id,
            node_id=execution.node_id,
            executor_type=execution.executor_type,
            tool_id=execution.tool_id,
            tool_version=execution.tool_version,
            status=execution.status,
            exit_code=execution.exit_code,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            physical_stop_confirmed_at=execution.physical_stop_confirmed_at,
        )


ExecutionReadResponse = ExecutionResponse | CodeAuditExecutionResponse


class ExecutionListResponse(BaseModel):
    items: list[ExecutionReadResponse]
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


class ExecutionWaitResponse(BaseModel):
    wait_status: ExecutionWaitStatus
    execution_status: ExecutionStatus
    execution_id: str
    partial_output: str | None
    next_poll_after_seconds: int | None
    stdout_cursor: int
    stderr_cursor: int

    @classmethod
    def from_domain(cls, result: ExecutionWaitResult) -> "ExecutionWaitResponse":
        return cls(
            wait_status=result.wait_status,
            execution_status=result.execution.status,
            execution_id=result.execution.id,
            partial_output=result.partial_output,
            next_poll_after_seconds=result.next_poll_after_seconds,
            stdout_cursor=result.stdout_cursor,
            stderr_cursor=result.stderr_cursor,
        )
