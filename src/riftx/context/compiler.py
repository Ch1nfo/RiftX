"""Observable wrapper for the Runtime Context Compiler boundary."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from riftx.runtime.lifecycle import (
    CompiledContext,
    ContextCompiler,
    ContextCompileRequest,
)

from .inspector import ContextApplicationService
from .manifest import (
    ContextCategory,
    ContextCategoryUsage,
    ContextCompilation,
    ContextManifest,
)
from .token_counter import estimate_context_tokens

_RUNTIME_CONTRACT = "You are the RiftX primary agent. Follow the authorized run contract."


@dataclass(slots=True)
class _CategoryAccumulator:
    payloads: list[object] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)

    def add(self, payload: object, source_refs: Iterable[str] = ()) -> None:
        if payload is None or payload == "" or payload == [] or payload == {}:
            return
        self.payloads.append(payload)
        self.source_refs.extend(str(ref) for ref in source_refs if str(ref))

    def usage(self, category: ContextCategory) -> ContextCategoryUsage:
        character_count = sum(len(_render_for_count(payload)) for payload in self.payloads)
        return ContextCategoryUsage(
            category=category,
            item_count=len(self.payloads),
            character_count=character_count,
            estimated_tokens=sum(estimate_context_tokens(payload) for payload in self.payloads),
            source_refs=list(dict.fromkeys(self.source_refs)),
        )


class ContextManifestBuilder:
    """Classify the model-visible payload into stable Context categories."""

    def build(
        self,
        request: ContextCompileRequest,
        compiled: CompiledContext,
    ) -> ContextManifest:
        categories = defaultdict(_CategoryAccumulator)
        runtime_instructions, stable_instructions = _instruction_layers(
            compiled.system_instructions,
            request.objective,
        )
        categories[ContextCategory.RUNTIME_CONTRACT].add(runtime_instructions)
        categories[ContextCategory.RUN_CONTRACT].add(
            f"Objective: {request.objective}" if request.objective else ""
        )
        categories[ContextCategory.STABLE_INSTRUCTIONS].add(stable_instructions)
        for payload in (
            compiled.available_skills,
            compiled.loaded_skill_documents,
            compiled.loaded_skill_references,
        ):
            categories[ContextCategory.STABLE_INSTRUCTIONS].add(payload)

        for item in compiled.input_items:
            category = _input_category(item)
            refs = item.get("source_refs", [])
            source_refs = refs if isinstance(refs, list) else []
            categories[category].add(item, source_refs)

        for schema in compiled.available_tools:
            tool_id = schema.get("name") or schema.get("id")
            categories[ContextCategory.TOOL_SCHEMAS].add(
                schema,
                [str(tool_id)] if tool_id else [],
            )

        usages = {category: categories[category].usage(category) for category in ContextCategory}
        return ContextManifest(
            run_id=request.run_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            model_profile=request.model_profile,
            purpose=request.purpose.value,
            categories=usages,
            loaded_memory_ids=compiled.loaded_memory_ids,
            checkpoint_id=compiled.checkpoint_id,
            metadata=dict(compiled.context_manifest),
        )


class ManifestingContextCompiler:
    """Persist a typed manifest for every delegated Context compilation."""

    def __init__(
        self,
        delegate: ContextCompiler,
        context_service: ContextApplicationService,
        *,
        builder: ContextManifestBuilder | None = None,
    ) -> None:
        self._delegate = delegate
        self._context_service = context_service
        self._builder = builder or ContextManifestBuilder()

    async def compile(self, request: ContextCompileRequest) -> CompiledContext:
        compiled = await self._delegate.compile(request)
        manifest = self._builder.build(request, compiled)
        compilation = await self._context_service.create(
            ContextCompilation(
                run_id=request.run_id,
                session_id=request.session_id,
                agent_id=request.agent_id,
                model_profile=request.model_profile,
                purpose=request.purpose.value,
                manifest=manifest,
            )
        )
        compiled.compilation_id = compilation.id
        compiled.token_estimate = compilation.estimated_tokens
        compiled.context_manifest = {
            **manifest.model_dump(mode="json"),
            "compilation_id": compilation.id,
        }
        return compiled

    async def record_usage(
        self,
        compilation_id: str,
        usage: Mapping[str, object],
    ) -> ContextCompilation:
        return await self._context_service.record_usage(compilation_id, usage)


def _instruction_layers(instructions: str, objective: str) -> tuple[str, str]:
    if not instructions:
        return "", ""
    runtime = _RUNTIME_CONTRACT if _RUNTIME_CONTRACT in instructions else instructions
    remainder = instructions.replace(_RUNTIME_CONTRACT, "", 1).strip()
    if objective:
        remainder = remainder.replace(f"Objective: {objective}", "", 1).strip()
    return runtime, remainder


def _input_category(item: dict[str, object]) -> ContextCategory:
    explicit = item.get("context_category")
    if isinstance(explicit, str):
        try:
            return ContextCategory(explicit)
        except ValueError:
            pass
    item_type = str(item.get("type") or "").lower()
    role = str(item.get("role") or "").lower()
    if item_type in {"tool_result", "function_call_output"} or role == "tool":
        return ContextCategory.TOOL_RESULTS
    if item_type == "subagent_result":
        return ContextCategory.SUBAGENT_RESULTS
    if item_type in {"memory", "retrieved_memory"}:
        return ContextCategory.RETRIEVED_MEMORY
    if item_type in {"working_memory", "working_memory_snapshot"}:
        return ContextCategory.WORKING_MEMORY
    return ContextCategory.CONVERSATION


def _render_for_count(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
