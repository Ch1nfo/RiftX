"""Runtime cycle request, result, limits, and context compiler contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import Field

from riftx.domain.base import DomainModel
from riftx.runtime.types import AgentSession, YieldReason


class ContextPurpose(StrEnum):
    PRIMARY_REASONING = "primary_reasoning"
    TOOL_RESULT_ANALYSIS = "tool_result_analysis"
    PLAN_UPDATE = "plan_update"
    COMPACTION = "compaction"
    MEMORY_EXTRACTION = "memory_extraction"
    SUBAGENT_DELEGATION = "subagent_delegation"
    REPORT_GENERATION = "report_generation"


class ContextCompileRequest(DomainModel):
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    purpose: ContextPurpose = ContextPurpose.PRIMARY_REASONING
    model_profile: str = Field(min_length=1)
    latest_user_message_id: str | None = None
    include_tool_schemas: bool = True
    objective: str = ""
    input_text: str | None = None
    input_items: list[dict[str, object]] = Field(default_factory=list)


class CompiledContext(DomainModel):
    system_instructions: str
    input_items: list[dict[str, object]] = Field(default_factory=list)
    available_tools: list[dict[str, object]] = Field(default_factory=list)
    token_estimate: int = Field(default=0, ge=0)
    loaded_memory_ids: list[str] = Field(default_factory=list)
    checkpoint_id: str | None = None
    context_manifest: dict[str, object] = Field(default_factory=dict)


class ContextCompiler(Protocol):
    async def compile(self, request: ContextCompileRequest) -> CompiledContext: ...


class MinimalContextCompiler:
    """Small RT-03 compiler that preserves the final compiler interface."""

    async def compile(self, request: ContextCompileRequest) -> CompiledContext:
        items = list(request.input_items)
        if request.input_text:
            items.append({"role": "user", "content": request.input_text})
        instructions = "You are the RiftX primary agent. Follow the authorized run contract."
        if request.objective:
            instructions += f"\nObjective: {request.objective}"
        estimate = max(1, (len(instructions) + sum(len(str(item)) for item in items)) // 4)
        return CompiledContext(
            system_instructions=instructions,
            input_items=items,
            token_estimate=estimate,
            context_manifest={
                "compiler": "minimal",
                "run_id": request.run_id,
                "session_id": request.session_id,
                "purpose": request.purpose.value,
            },
        )


class CycleLimits(DomainModel):
    max_model_calls: int = Field(default=8, ge=1)
    max_tool_calls: int = Field(default=12, ge=1)
    max_duration_seconds: float = Field(default=900, gt=0)


class RunCycleRequest(DomainModel):
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    input_text: str | None = None
    input_items: list[dict[str, object]] = Field(default_factory=list)
    latest_user_message_id: str | None = None
    compaction_required: bool = False


class RunCycleResult(DomainModel):
    run_id: str
    session_id: str
    cycle_id: str
    yield_reason: YieldReason
    model_call_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    provider_state_id: str | None = None


class RuntimeStateLoader(Protocol):
    async def load_session(self, run_id: str, session_id: str) -> AgentSession: ...
