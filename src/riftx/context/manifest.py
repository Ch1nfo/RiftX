"""Typed Context Manifest and durable compilation records."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, model_validator

from riftx.domain.base import DomainModel, new_id, utc_now


class ContextCategory(StrEnum):
    RUNTIME_CONTRACT = "runtime_contract"
    STABLE_INSTRUCTIONS = "stable_instructions"
    RUN_CONTRACT = "run_contract"
    WORKING_MEMORY = "working_memory"
    CONVERSATION = "conversation"
    TOOL_RESULTS = "tool_results"
    RETRIEVED_MEMORY = "retrieved_memory"
    SUBAGENT_RESULTS = "subagent_results"
    TOOL_SCHEMAS = "tool_schemas"


class ContextCategoryUsage(DomainModel):
    category: ContextCategory
    item_count: int = Field(default=0, ge=0)
    character_count: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    source_refs: list[str] = Field(default_factory=list)


class ContextManifest(DomainModel):
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    model_profile: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    categories: dict[ContextCategory, ContextCategoryUsage] = Field(default_factory=dict)
    estimated_tokens: int = Field(default=0, ge=0)
    loaded_memory_ids: list[str] = Field(default_factory=list)
    checkpoint_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ensure_all_categories(self) -> ContextManifest:
        normalized: dict[ContextCategory, ContextCategoryUsage] = {}
        for category in ContextCategory:
            usage = self.categories.get(category)
            if usage is None:
                usage = ContextCategoryUsage(category=category)
            elif usage.category is not category:
                raise ValueError(
                    f"Context category key {category.value!r} does not match "
                    f"usage category {usage.category.value!r}"
                )
            normalized[category] = usage
        object.__setattr__(self, "categories", normalized)
        object.__setattr__(
            self,
            "estimated_tokens",
            sum(usage.estimated_tokens for usage in normalized.values()),
        )
        return self

    @classmethod
    def empty(
        cls,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        model_profile: str,
        purpose: str,
    ) -> ContextManifest:
        return cls(
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
            model_profile=model_profile,
            purpose=purpose,
        )


class ContextCompilation(DomainModel):
    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    model_profile: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    manifest: ContextManifest
    estimated_tokens: int = Field(default=0, ge=0)
    actual_input_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    loaded_memory_ids: list[str] = Field(default_factory=list)
    checkpoint_id: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def synchronize_manifest_totals(self) -> ContextCompilation:
        object.__setattr__(self, "estimated_tokens", self.manifest.estimated_tokens)
        object.__setattr__(self, "loaded_memory_ids", list(self.manifest.loaded_memory_ids))
        object.__setattr__(self, "checkpoint_id", self.manifest.checkpoint_id)
        return self


class ContextCompilationRepository(Protocol):
    async def create(self, compilation: ContextCompilation) -> ContextCompilation: ...

    async def get(self, compilation_id: str) -> ContextCompilation | None: ...

    async def latest_for_session(self, session_id: str) -> ContextCompilation | None: ...

    async def latest_for_run(self, run_id: str) -> ContextCompilation | None: ...

    async def update_usage(
        self,
        compilation_id: str,
        *,
        actual_input_tokens: int | None,
        actual_output_tokens: int | None,
    ) -> ContextCompilation: ...


@runtime_checkable
class ContextUsageRecorder(Protocol):
    async def record_usage(
        self,
        compilation_id: str,
        usage: Mapping[str, object],
    ) -> ContextCompilation: ...


def usage_token_counts(usage: Mapping[str, object]) -> tuple[int | None, int | None]:
    """Normalize common provider usage names into input/output token totals."""

    nested = usage.get("usage")
    payload = nested if isinstance(nested, Mapping) else usage
    input_tokens = _first_non_negative_int(payload, "input_tokens", "prompt_tokens")
    output_tokens = _first_non_negative_int(payload, "output_tokens", "completion_tokens")
    return input_tokens, output_tokens


def _first_non_negative_int(
    payload: Mapping[str, object],
    *keys: str,
) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None
