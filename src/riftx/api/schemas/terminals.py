"""Interactive terminal REST and WebSocket schemas."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, Field

from riftx.application.services import CreateTerminal, TerminalView
from riftx.domain import ExecutionStatus, TerminalOwner, TerminalStatus


class TerminalCreateRequest(BaseModel):
    argv: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str | None] = Field(default_factory=dict)
    cols: int = Field(default=120, ge=1, le=1000)
    rows: int = Field(default=40, ge=1, le=1000)
    owner: TerminalOwner = TerminalOwner.AGENT

    def to_command(self) -> CreateTerminal:
        return CreateTerminal(
            argv=self.argv,
            cwd=self.cwd,
            env=self.env,
            cols=self.cols,
            rows=self.rows,
            owner=self.owner,
        )


class TerminalResponse(BaseModel):
    id: str
    run_id: str
    execution_id: str
    status: TerminalStatus
    owner: TerminalOwner
    cols: int
    rows: int
    output_cursor: int
    transcript_artifact_id: str | None
    argv: list[str]
    cwd: str
    pid: int | None
    exit_code: int | None
    execution_status: ExecutionStatus
    created_at: AwareDatetime
    closed_at: AwareDatetime | None

    @classmethod
    def from_view(cls, view: TerminalView) -> TerminalResponse:
        return cls(
            id=view.terminal.id,
            run_id=view.terminal.run_id,
            execution_id=view.terminal.execution_id,
            status=view.terminal.status,
            owner=view.terminal.owner,
            cols=view.terminal.cols,
            rows=view.terminal.rows,
            output_cursor=view.terminal.output_cursor,
            transcript_artifact_id=view.terminal.transcript_artifact_id,
            argv=view.execution.argv,
            cwd=view.execution.cwd,
            pid=view.execution.pid,
            exit_code=view.execution.exit_code,
            execution_status=view.execution.status,
            created_at=view.terminal.created_at,
            closed_at=view.terminal.closed_at,
        )
