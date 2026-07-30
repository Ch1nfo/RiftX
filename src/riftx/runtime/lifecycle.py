"""Runtime cycle request, result, limits, and context compiler contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import Field

from riftx.domain.base import DomainModel
from riftx.runtime.types import AgentSession, YieldReason
from riftx.skills import ProgressiveSkillContextManager
from riftx.tools import ToolContextManager


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
    run_contract: dict[str, object] = Field(default_factory=dict)
    engagement_path: str | None = None
    workspace_path: str | None = None
    current_path: str | None = None
    input_text: str | None = None
    input_items: list[dict[str, object]] = Field(default_factory=list)
    selected_fact_ids: list[str] = Field(default_factory=list)
    selected_memory_ids: list[str] = Field(default_factory=list)


class CompiledContext(DomainModel):
    compilation_id: str | None = None
    system_instructions: str
    input_items: list[dict[str, object]] = Field(default_factory=list)
    available_tools: list[dict[str, object]] = Field(default_factory=list)
    available_skills: list[dict[str, object]] = Field(default_factory=list)
    loaded_skill_documents: list[dict[str, object]] = Field(default_factory=list)
    loaded_skill_references: list[dict[str, object]] = Field(default_factory=list)
    token_estimate: int = Field(default=0, ge=0)
    loaded_memory_ids: list[str] = Field(default_factory=list)
    checkpoint_id: str | None = None
    context_manifest: dict[str, object] = Field(default_factory=dict)


class ContextCompiler(Protocol):
    async def compile(self, request: ContextCompileRequest) -> CompiledContext: ...


class MinimalContextCompiler:
    """Compatibility facade over the single layered Context Compiler."""

    def __init__(self) -> None:
        self._delegate: ContextCompiler | None = None

    async def compile(self, request: ContextCompileRequest) -> CompiledContext:
        if self._delegate is None:
            from riftx.context.compiler import ContextCompiler as LayeredContextCompiler
            from riftx.context.instructions import StableInstructionSource

            self._delegate = LayeredContextCompiler(
                stable_instruction_source=StableInstructionSource()
            )
        return await self._delegate.compile(request)


class DynamicToolContextCompiler(MinimalContextCompiler):
    """Compatibility facade configuring dynamic Tool and Progressive Skill sources."""

    def __init__(
        self,
        tool_context: ToolContextManager,
        skill_context: ProgressiveSkillContextManager | None = None,
    ) -> None:
        super().__init__()
        self._tool_context = tool_context
        self._skill_context = skill_context

    async def compile(self, request: ContextCompileRequest) -> CompiledContext:
        if self._delegate is None:
            from riftx.context.compiler import ContextCompiler as LayeredContextCompiler
            from riftx.context.instructions import StableInstructionSource

            self._delegate = LayeredContextCompiler(
                tool_context=self._tool_context,
                skill_context=self._skill_context,
                stable_instruction_source=StableInstructionSource(),
            )
        return await self._delegate.compile(request)


class CycleLimits(DomainModel):
    max_model_calls: int = Field(default=8, ge=1)
    max_tool_calls: int = Field(default=12, ge=1)
    max_duration_seconds: float = Field(default=900, gt=0)


class RunCycleRequest(DomainModel):
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    cycle_id: str | None = None
    input_text: str | None = None
    input_items: list[dict[str, object]] = Field(default_factory=list)
    latest_user_message_id: str | None = None
    approval_id: str | None = None
    engagement_path: str | None = None
    current_path: str | None = None
    compaction_required: bool = False
    subagent_mode: bool = False


class RunCycleResult(DomainModel):
    run_id: str
    session_id: str
    cycle_id: str
    yield_reason: YieldReason
    model_call_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    provider_state_id: str | None = None
    waiting_object_id: str | None = None
    waiting_execution_id: str | None = None


class RuntimeStateLoader(Protocol):
    async def load_session(self, run_id: str, session_id: str) -> AgentSession: ...
