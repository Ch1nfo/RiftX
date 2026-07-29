"""Structured Primary Agent cycle results."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AgentCycleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_message: str
    plan_summary: str
    run_summary: str | None = None
    completed: bool = False
    needs_input: bool = False


class AgentCycleStatus(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


class AgentInterruption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    tool_name: str
    arguments: str | None = None


class AgentCycleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AgentCycleStatus
    output: AgentCycleOutput | None = None
    checkpoint_id: str | None = None
    interruptions: list[AgentInterruption] = Field(default_factory=list)
