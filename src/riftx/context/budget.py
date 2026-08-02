"""Deterministic token budgeting for provider-neutral Context Items."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence

from pydantic import Field

from riftx.domain.base import DomainModel

from .items import ContextItem, ContextItemKind
from .manifest import ContextCategory
from .token_counter import estimate_context_tokens


class RequiredContextOverflowError(RuntimeError):
    def __init__(
        self,
        *,
        budget: int,
        required_tokens: int,
        item_ids: list[str],
    ) -> None:
        super().__init__(
            f"required Context Items need {required_tokens} tokens but the input budget is "
            f"{budget}; protected items: {item_ids}"
        )
        self.budget = budget
        self.required_tokens = required_tokens
        self.item_ids = item_ids


class ContextBudgetResult(DomainModel):
    selected_items: list[ContextItem]
    dropped_item_ids: list[str] = Field(default_factory=list)
    compressed_item_ids: list[str] = Field(default_factory=list)
    estimated_tokens_before: int = Field(ge=0)
    estimated_tokens_after: int = Field(ge=0)
    input_budget: int = Field(gt=0)
    category_tokens: dict[ContextCategory, int] = Field(default_factory=dict)


_DEFAULT_CATEGORY_FRACTIONS: dict[ContextCategory, float] = {
    ContextCategory.RUNTIME_CONTRACT: 0.05,
    ContextCategory.STABLE_INSTRUCTIONS: 0.08,
    ContextCategory.RUN_CONTRACT: 0.05,
    ContextCategory.WORKING_MEMORY: 0.15,
    ContextCategory.CONVERSATION: 0.22,
    ContextCategory.TOOL_RESULTS: 0.20,
    ContextCategory.RETRIEVED_MEMORY: 0.10,
    ContextCategory.SUBAGENT_RESULTS: 0.08,
    ContextCategory.TOOL_SCHEMAS: 0.15,
}

_EVICTION_ORDER: dict[ContextItemKind, int] = {
    ContextItemKind.TOOL_PREVIEW: 10,
    ContextItemKind.DUPLICATE_TOOL_RESULT: 20,
    ContextItemKind.RETRIEVED_MEMORY: 30,
    ContextItemKind.TOOL_SCHEMA: 35,
    ContextItemKind.ASSISTANT_DETAIL: 40,
    ContextItemKind.CHITCHAT: 50,
    ContextItemKind.COMPLETED_PLAN_DETAIL: 60,
    ContextItemKind.SKILL_SUMMARY: 65,
    ContextItemKind.SKILL_REFERENCE: 70,
    ContextItemKind.SKILL_DOCUMENT: 75,
    ContextItemKind.SUBAGENT_RESULT: 80,
    ContextItemKind.GENERAL: 85,
    ContextItemKind.CURRENT_FOCUS: 90,
    ContextItemKind.CONFIRMED_FACT: 92,
    ContextItemKind.HYPOTHESIS: 94,
}


class TokenBudgeter:
    """Apply category caps and then a global cap without dropping protected state."""

    def __init__(
        self,
        max_input_tokens: int = 81_920,
        *,
        category_fractions: Mapping[ContextCategory, float] | None = None,
        minimum_compressed_tokens: int = 64,
    ) -> None:
        if max_input_tokens < 1:
            raise ValueError("max_input_tokens must be positive")
        if minimum_compressed_tokens < 8:
            raise ValueError("minimum_compressed_tokens must be at least 8")
        self.max_input_tokens = max_input_tokens
        self.category_fractions = dict(category_fractions or _DEFAULT_CATEGORY_FRACTIONS)
        self.minimum_compressed_tokens = minimum_compressed_tokens

    def fit(self, items: Sequence[ContextItem]) -> ContextBudgetResult:
        selected = [item.model_copy(deep=True) for item in items]
        before = _total_tokens(selected)
        protected = [item for item in selected if item.required or not item.removable]
        protected_tokens = _total_tokens(protected)
        if protected_tokens > self.max_input_tokens:
            raise RequiredContextOverflowError(
                budget=self.max_input_tokens,
                required_tokens=protected_tokens,
                item_ids=[item.id for item in protected],
            )

        dropped: list[str] = []
        compressed: list[str] = []
        for category, fraction in self.category_fractions.items():
            category_limit = max(1, int(self.max_input_tokens * fraction))
            self._trim(
                selected,
                limit=category_limit,
                category=category,
                dropped=dropped,
                compressed=compressed,
            )
        self._trim(
            selected,
            limit=self.max_input_tokens,
            category=None,
            dropped=dropped,
            compressed=compressed,
        )

        selected_ids = {item.id for item in selected}
        ordered = [item for item in items if item.id in selected_ids]
        selected_by_id = {item.id: item for item in selected}
        ordered = [selected_by_id[item.id] for item in ordered]
        category_tokens: dict[ContextCategory, int] = defaultdict(int)
        for item in ordered:
            assert item.category is not None
            category_tokens[item.category] += item.estimated_tokens
        return ContextBudgetResult(
            selected_items=ordered,
            dropped_item_ids=dropped,
            compressed_item_ids=compressed,
            estimated_tokens_before=before,
            estimated_tokens_after=_total_tokens(ordered),
            input_budget=self.max_input_tokens,
            category_tokens=dict(category_tokens),
        )

    def _trim(
        self,
        selected: list[ContextItem],
        *,
        limit: int,
        category: ContextCategory | None,
        dropped: list[str],
        compressed: list[str],
    ) -> None:
        def scoped() -> list[ContextItem]:
            if category is None:
                return selected
            return [item for item in selected if item.category is category]

        while _total_tokens(scoped()) > limit:
            candidates = [
                item
                for item in scoped()
                if item.removable and not item.required
            ]
            if not candidates:
                break
            candidate = min(candidates, key=_eviction_key)
            excess = _total_tokens(scoped()) - limit
            if candidate.compressible and candidate.id not in compressed:
                target = max(
                    self.minimum_compressed_tokens,
                    min(
                        candidate.estimated_tokens // 2,
                        candidate.estimated_tokens - excess,
                    ),
                )
                replacement = _compress(candidate, target)
                if replacement.estimated_tokens < candidate.estimated_tokens:
                    selected[selected.index(candidate)] = replacement
                    compressed.append(candidate.id)
                    continue
            selected.remove(candidate)
            dropped.append(candidate.id)


def _eviction_key(item: ContextItem) -> tuple[int, int, float, int, str]:
    return (
        _EVICTION_ORDER.get(item.kind, 88),
        item.priority,
        item.relevance,
        item.sequence,
        item.id,
    )


def _compress(item: ContextItem, target_tokens: int) -> ContextItem:
    rendered = _render(item.content)
    max_characters = max(32, target_tokens * 4)
    if len(rendered) <= max_characters:
        return item
    marker = "\n… context item compressed …\n"
    available = max(16, max_characters - len(marker))
    head = max(8, int(available * 0.65))
    tail = max(8, available - head)
    content = rendered[:head] + marker + rendered[-tail:]
    return item.model_copy(
        update={
            "content": content,
            "estimated_tokens": max(1, estimate_context_tokens(content)),
            "metadata": {**item.metadata, "compressed": True},
        }
    )


def _render(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _total_tokens(items: Sequence[ContextItem]) -> int:
    return sum(item.estimated_tokens for item in items)
