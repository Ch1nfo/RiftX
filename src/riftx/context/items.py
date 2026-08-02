"""Provider-neutral Context source items and deterministic layer ordering."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import Field, JsonValue, model_validator

from riftx.domain.base import DomainModel, new_id
from riftx.runtime.lifecycle import ContextCompileRequest

from .manifest import ContextCategory
from .token_counter import estimate_context_tokens


class ContextLayer(StrEnum):
    RUNTIME_CONTRACT = "runtime_contract"
    STABLE_INSTRUCTIONS = "stable_instructions"
    RUN_CONTRACT = "run_contract"
    WORKING_MEMORY = "working_memory"
    LATEST_CHECKPOINT = "latest_checkpoint"
    RECENT_CONVERSATION = "recent_conversation"
    RELEVANT_TOOL_RESULTS = "relevant_tool_results"
    RETRIEVED_MEMORY = "retrieved_memory"
    SUBAGENT_RESULTS = "subagent_results"
    DYNAMIC_TOOL_SCHEMAS = "dynamic_tool_schemas"
    CURRENT_INPUT = "current_input"


CONTEXT_LAYER_ORDER: tuple[ContextLayer, ...] = tuple(ContextLayer)
CONTEXT_LAYER_INDEX = {layer: index for index, layer in enumerate(CONTEXT_LAYER_ORDER)}


class ContextItemKind(StrEnum):
    RUNTIME_CONTRACT = "runtime_contract"
    STABLE_INSTRUCTION = "stable_instruction"
    RUN_CONTRACT = "run_contract"
    CURRENT_PLAN = "current_plan"
    PENDING_APPROVAL = "pending_approval"
    ACTIVE_EXECUTION = "active_execution"
    ACTIVE_TERMINAL = "active_terminal"
    FAILED_ATTEMPT = "failed_attempt"
    CURRENT_FOCUS = "current_focus"
    CONFIRMED_FACT = "confirmed_fact"
    HYPOTHESIS = "hypothesis"
    CHECKPOINT = "checkpoint"
    TOOL_PREVIEW = "tool_preview"
    DUPLICATE_TOOL_RESULT = "duplicate_tool_result"
    RETRIEVED_MEMORY = "retrieved_memory"
    SUBAGENT_RESULT = "subagent_result"
    ASSISTANT_DETAIL = "assistant_detail"
    CHITCHAT = "chitchat"
    COMPLETED_PLAN_DETAIL = "completed_plan_detail"
    TOOL_SCHEMA = "tool_schema"
    SKILL_SUMMARY = "skill_summary"
    SKILL_DOCUMENT = "skill_document"
    SKILL_REFERENCE = "skill_reference"
    CURRENT_INPUT = "current_input"
    GENERAL = "general"


_LAYER_CATEGORY = {
    ContextLayer.RUNTIME_CONTRACT: ContextCategory.RUNTIME_CONTRACT,
    ContextLayer.STABLE_INSTRUCTIONS: ContextCategory.STABLE_INSTRUCTIONS,
    ContextLayer.RUN_CONTRACT: ContextCategory.RUN_CONTRACT,
    ContextLayer.WORKING_MEMORY: ContextCategory.WORKING_MEMORY,
    ContextLayer.LATEST_CHECKPOINT: ContextCategory.WORKING_MEMORY,
    ContextLayer.RECENT_CONVERSATION: ContextCategory.CONVERSATION,
    ContextLayer.RELEVANT_TOOL_RESULTS: ContextCategory.TOOL_RESULTS,
    ContextLayer.RETRIEVED_MEMORY: ContextCategory.RETRIEVED_MEMORY,
    ContextLayer.SUBAGENT_RESULTS: ContextCategory.SUBAGENT_RESULTS,
    ContextLayer.DYNAMIC_TOOL_SCHEMAS: ContextCategory.TOOL_SCHEMAS,
    ContextLayer.CURRENT_INPUT: ContextCategory.CONVERSATION,
}


class ContextItem(DomainModel):
    id: str = Field(default_factory=new_id)
    layer: ContextLayer
    category: ContextCategory | None = None
    kind: ContextItemKind = ContextItemKind.GENERAL
    content: JsonValue
    priority: int = Field(default=50, ge=0, le=100)
    estimated_tokens: int = Field(default=0, ge=0)
    required: bool = False
    compressible: bool = True
    removable: bool = True
    source_refs: list[str] = Field(default_factory=list)
    relevance: float = Field(default=1.0, ge=0.0, le=1.0)
    sequence: int = Field(default=0, ge=0)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fill_derived_fields(self) -> ContextItem:
        if self.category is None:
            object.__setattr__(self, "category", _LAYER_CATEGORY[self.layer])
        if self.estimated_tokens == 0:
            object.__setattr__(
                self,
                "estimated_tokens",
                max(1, estimate_context_tokens(self.content)),
            )
        if self.required:
            object.__setattr__(self, "removable", False)
        object.__setattr__(self, "source_refs", list(dict.fromkeys(self.source_refs)))
        return self


@runtime_checkable
class ContextSource(Protocol):
    async def load(self, request: ContextCompileRequest) -> list[ContextItem]: ...


class StaticContextSource:
    """Simple source useful for fixed platform context and integration tests."""

    def __init__(self, items: list[ContextItem]) -> None:
        self._items = items

    async def load(self, request: ContextCompileRequest) -> list[ContextItem]:
        del request
        return [item.model_copy(deep=True) for item in self._items]
