"""Single layered Context Compiler and durable compilation observability."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from riftx.runtime.lifecycle import (
    CompiledContext,
    ContextCompileRequest,
)
from riftx.runtime.lifecycle import (
    ContextCompiler as RuntimeContextCompiler,
)
from riftx.skills import ProgressiveSkillContextManager
from riftx.tools import ToolContextManager

from .budget import ContextBudgetResult, TokenBudgeter
from .inspector import ContextApplicationService
from .items import (
    CONTEXT_LAYER_INDEX,
    ContextItem,
    ContextItemKind,
    ContextLayer,
    ContextSource,
)
from .manifest import (
    ContextCategory,
    ContextCategoryUsage,
    ContextCompilation,
    ContextManifest,
)
from .token_counter import estimate_context_tokens

_RUNTIME_CONTRACT = "You are the RiftX primary agent. Follow the authorized run contract."
_SYSTEM_LAYERS = {
    ContextLayer.RUNTIME_CONTRACT,
    ContextLayer.STABLE_INSTRUCTIONS,
    ContextLayer.RUN_CONTRACT,
}


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
    """Classify an already-rendered model payload into stable Context categories."""

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


class ContextCompiler:
    """Load every Context layer, enforce one token budget, render, and manifest it."""

    def __init__(
        self,
        *,
        sources: Sequence[ContextSource] = (),
        stable_instruction_source: ContextSource | None = None,
        budgeter: TokenBudgeter | None = None,
        tool_context: ToolContextManager | None = None,
        skill_context: ProgressiveSkillContextManager | None = None,
        context_service: ContextApplicationService | None = None,
        runtime_contract: str = _RUNTIME_CONTRACT,
    ) -> None:
        self._sources = (
            *((stable_instruction_source,) if stable_instruction_source is not None else ()),
            *sources,
        )
        self._budgeter = budgeter or TokenBudgeter()
        self._tool_context = tool_context
        self._skill_context = skill_context
        self._context_service = context_service
        self._runtime_contract = runtime_contract

    async def compile(self, request: ContextCompileRequest) -> CompiledContext:
        items = self._request_contract_items(request)
        for source in self._sources:
            items.extend(await source.load(request))
        visibility_metadata: dict[str, object] = {}
        items.extend(self._request_input_items(request))
        dynamic_items, dynamic_metadata = self._dynamic_items(request)
        items.extend(dynamic_items)
        visibility_metadata.update(dynamic_metadata)
        _require_unique_item_ids(items)
        items = [self._normalize_estimate(item) for item in items]
        items.sort(key=_context_item_order)

        budget = self._budgeter.fit(items)
        selected = budget.selected_items
        compiled = self._render(selected, budget)
        compiled.loaded_memory_ids = list(
            dict.fromkeys(
                str(memory_id)
                for item in selected
                if (memory_id := item.metadata.get("memory_id")) is not None
            )
        )
        metadata = self._manifest_metadata(budget, visibility_metadata)
        manifest = self._manifest(request, selected, metadata, compiled)
        compiled.token_estimate = manifest.estimated_tokens
        compiled.context_manifest = {
            **manifest.model_dump(mode="json"),
            **metadata,
        }
        if self._context_service is not None:
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
            compiled.context_manifest["compilation_id"] = compilation.id
        return compiled

    async def record_usage(
        self,
        compilation_id: str,
        usage: Mapping[str, object],
    ) -> ContextCompilation:
        if self._context_service is None:
            raise RuntimeError("Context Compiler has no persistence service for usage recording")
        return await self._context_service.record_usage(compilation_id, usage)

    def _request_contract_items(self, request: ContextCompileRequest) -> list[ContextItem]:
        run_contract = dict(request.run_contract)
        run_contract.setdefault("run_id", request.run_id)
        if request.objective:
            run_contract.setdefault("objective", request.objective)
        return [
            ContextItem(
                id="runtime-contract",
                layer=ContextLayer.RUNTIME_CONTRACT,
                kind=ContextItemKind.RUNTIME_CONTRACT,
                content=self._runtime_contract,
                priority=100,
                required=True,
                compressible=False,
                source_refs=["runtime://contract"],
            ),
            ContextItem(
                id=f"run-contract:{request.run_id}",
                layer=ContextLayer.RUN_CONTRACT,
                kind=ContextItemKind.RUN_CONTRACT,
                content=run_contract,
                priority=100,
                required=True,
                compressible=False,
                source_refs=[f"run://{request.run_id}"],
            ),
        ]

    def _request_input_items(self, request: ContextCompileRequest) -> list[ContextItem]:
        items: list[ContextItem] = []
        for sequence, payload in enumerate(request.input_items, start=1):
            item_id = str(payload.get("id") or f"request-input:{sequence}")
            layer, kind = _request_payload_layer(payload)
            required = item_id == request.latest_user_message_id
            if required:
                layer = ContextLayer.CURRENT_INPUT
                kind = ContextItemKind.CURRENT_INPUT
            refs = payload.get("source_refs")
            source_refs = [str(ref) for ref in refs] if isinstance(refs, list) else []
            items.append(
                ContextItem(
                    id=item_id,
                    layer=layer,
                    kind=kind,
                    content=payload,
                    priority=100 if required else int(payload.get("priority", 60)),
                    required=required or bool(payload.get("required", False)),
                    compressible=not required and bool(payload.get("compressible", True)),
                    removable=not required and bool(payload.get("removable", True)),
                    source_refs=source_refs,
                    relevance=float(payload.get("relevance", 1.0)),
                    sequence=sequence,
                )
            )
        if request.input_text is not None:
            items.append(
                ContextItem(
                    id=request.latest_user_message_id or "current-input",
                    layer=ContextLayer.CURRENT_INPUT,
                    kind=ContextItemKind.CURRENT_INPUT,
                    content={"role": "user", "content": request.input_text},
                    priority=100,
                    required=True,
                    compressible=False,
                    source_refs=(
                        [f"message://{request.latest_user_message_id}"]
                        if request.latest_user_message_id
                        else ["request://current-input"]
                    ),
                    sequence=len(request.input_items) + 1,
                )
            )
        return items

    def _dynamic_items(
        self,
        request: ContextCompileRequest,
    ) -> tuple[list[ContextItem], dict[str, object]]:
        items: list[ContextItem] = []
        metadata: dict[str, object] = {}
        if self._tool_context is not None:
            visibility = self._tool_context.visibility(
                run_id=request.run_id,
                session_id=request.session_id,
                agent_id=request.agent_id,
            )
            metadata.update(visibility.manifest())
            if request.include_tool_schemas:
                residents = set(visibility.always_visible_tools)
                for sequence, schema in enumerate(visibility.available_tools, start=1):
                    tool_id = str(schema.get("name") or schema.get("id") or sequence)
                    resident = tool_id in residents
                    items.append(
                        ContextItem(
                            id=f"tool-schema:{tool_id}",
                            layer=ContextLayer.DYNAMIC_TOOL_SCHEMAS,
                            kind=ContextItemKind.TOOL_SCHEMA,
                            content=schema,
                            priority=100 if resident else 85,
                            required=resident,
                            compressible=False,
                            removable=not resident,
                            source_refs=[f"tool://{tool_id}"],
                            sequence=sequence,
                            metadata={"tool_id": tool_id, "resident": resident},
                        )
                    )
        if self._skill_context is not None:
            visibility = self._skill_context.visibility(
                run_id=request.run_id,
                session_id=request.session_id,
                agent_id=request.agent_id,
            )
            metadata.update(visibility.manifest())
            for sequence, summary in enumerate(visibility.available_skills, start=1):
                items.append(
                    ContextItem(
                        id=f"skill-summary:{summary.id}",
                        layer=ContextLayer.STABLE_INSTRUCTIONS,
                        kind=ContextItemKind.SKILL_SUMMARY,
                        content=summary.model_dump(mode="json"),
                        priority=65,
                        source_refs=[f"skill://{summary.id}"],
                        sequence=sequence,
                        metadata={"skill_payload": "summary"},
                    )
                )
            for sequence, document in enumerate(visibility.loaded_skill_documents, start=1):
                items.append(
                    ContextItem(
                        id=f"skill-document:{document.id}",
                        layer=ContextLayer.STABLE_INSTRUCTIONS,
                        kind=ContextItemKind.SKILL_DOCUMENT,
                        content=document.model_dump(mode="json"),
                        priority=90,
                        source_refs=[f"skill://{document.id}/document"],
                        sequence=10_000 + sequence,
                        metadata={"skill_payload": "document"},
                    )
                )
            for sequence, reference in enumerate(visibility.loaded_skill_references, start=1):
                items.append(
                    ContextItem(
                        id=f"skill-reference:{reference.skill_id}",
                        layer=ContextLayer.STABLE_INSTRUCTIONS,
                        kind=ContextItemKind.SKILL_REFERENCE,
                        content=reference.model_dump(mode="json"),
                        priority=80,
                        source_refs=[f"skill://{reference.skill_id}/references"],
                        sequence=20_000 + sequence,
                        metadata={"skill_payload": "reference"},
                    )
                )
        return items, metadata

    def _normalize_estimate(self, item: ContextItem) -> ContextItem:
        return item.model_copy(
            update={"estimated_tokens": max(1, estimate_context_tokens(_visible_payload(item)))}
        )

    def _render(
        self,
        items: Sequence[ContextItem],
        budget: ContextBudgetResult,
    ) -> CompiledContext:
        system_instructions = "\n\n".join(
            str(_visible_payload(item)) for item in items if item.layer in _SYSTEM_LAYERS
        )
        input_items = [
            _render_input_item(item)
            for item in items
            if item.layer not in _SYSTEM_LAYERS
            and item.layer is not ContextLayer.DYNAMIC_TOOL_SCHEMAS
        ]
        tools = [
            item.content
            for item in items
            if item.layer is ContextLayer.DYNAMIC_TOOL_SCHEMAS
            and isinstance(item.content, dict)
        ]
        skill_summaries = [
            item.content
            for item in items
            if item.kind is ContextItemKind.SKILL_SUMMARY and isinstance(item.content, dict)
        ]
        skill_documents = [
            item.content
            for item in items
            if item.kind is ContextItemKind.SKILL_DOCUMENT and isinstance(item.content, dict)
        ]
        skill_references = [
            item.content
            for item in items
            if item.kind is ContextItemKind.SKILL_REFERENCE and isinstance(item.content, dict)
        ]
        loaded_memory_ids = list(
            dict.fromkeys(
                str(item.metadata["memory_id"])
                for item in items
                if "memory_id" in item.metadata
            )
        )
        checkpoint = next(
            (item for item in items if item.kind is ContextItemKind.CHECKPOINT),
            None,
        )
        checkpoint_id = (
            str(checkpoint.metadata.get("checkpoint_id") or checkpoint.id)
            if checkpoint is not None
            else None
        )
        return CompiledContext(
            system_instructions=system_instructions,
            input_items=input_items,
            available_tools=tools,
            available_skills=skill_summaries,
            loaded_skill_documents=skill_documents,
            loaded_skill_references=skill_references,
            token_estimate=budget.estimated_tokens_after,
            loaded_memory_ids=loaded_memory_ids,
            checkpoint_id=checkpoint_id,
        )

    def _manifest_metadata(
        self,
        budget: ContextBudgetResult,
        visibility: Mapping[str, object],
    ) -> dict[str, object]:
        instruction_metadata: dict[str, object] = {}
        for item in budget.selected_items:
            if item.kind is ContextItemKind.STABLE_INSTRUCTION:
                instruction_metadata.update(item.metadata)
        return {
            "compiler": "layered",
            "input_budget": budget.input_budget,
            "estimated_tokens_before_budget": budget.estimated_tokens_before,
            "estimated_tokens_after_budget": budget.estimated_tokens_after,
            "selected_context_item_ids": [item.id for item in budget.selected_items],
            "dropped_context_item_ids": budget.dropped_item_ids,
            "compressed_context_item_ids": budget.compressed_item_ids,
            "context_item_tokens": {
                item.id: item.estimated_tokens for item in budget.selected_items
            },
            **instruction_metadata,
            **visibility,
        }

    def _manifest(
        self,
        request: ContextCompileRequest,
        items: Sequence[ContextItem],
        metadata: Mapping[str, object],
        compiled: CompiledContext,
    ) -> ContextManifest:
        usages: dict[ContextCategory, ContextCategoryUsage] = {}
        for category in ContextCategory:
            category_items = [item for item in items if item.category is category]
            usages[category] = ContextCategoryUsage(
                category=category,
                item_count=len(category_items),
                character_count=sum(
                    len(_render_for_count(_visible_payload(item))) for item in category_items
                ),
                estimated_tokens=sum(item.estimated_tokens for item in category_items),
                source_refs=list(
                    dict.fromkeys(ref for item in category_items for ref in item.source_refs)
                ),
            )
        return ContextManifest(
            run_id=request.run_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            model_profile=request.model_profile,
            purpose=request.purpose.value,
            categories=usages,
            loaded_memory_ids=compiled.loaded_memory_ids,
            checkpoint_id=compiled.checkpoint_id,
            metadata=dict(metadata),
        )


class ManifestingContextCompiler:
    """Persist a typed manifest for delegated legacy compilers."""

    def __init__(
        self,
        delegate: RuntimeContextCompiler,
        context_service: ContextApplicationService,
        *,
        builder: ContextManifestBuilder | None = None,
    ) -> None:
        self._delegate = delegate
        self._context_service = context_service
        self._builder = builder or ContextManifestBuilder()

    async def compile(self, request: ContextCompileRequest) -> CompiledContext:
        compiled = await self._delegate.compile(request)
        if compiled.compilation_id is not None:
            return compiled
        manifest = _typed_manifest_from_compiled(compiled)
        if manifest is None:
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
            **dict(compiled.context_manifest),
            "compilation_id": compilation.id,
        }
        return compiled

    async def record_usage(
        self,
        compilation_id: str,
        usage: Mapping[str, object],
    ) -> ContextCompilation:
        return await self._context_service.record_usage(compilation_id, usage)


def _typed_manifest_from_compiled(compiled: CompiledContext) -> ContextManifest | None:
    payload = compiled.context_manifest
    if payload.get("compiler") != "layered" or "categories" not in payload:
        return None
    fields = {
        "run_id",
        "session_id",
        "agent_id",
        "model_profile",
        "purpose",
        "categories",
        "estimated_tokens",
        "loaded_memory_ids",
        "checkpoint_id",
        "metadata",
    }
    return ContextManifest.model_validate(
        {key: value for key, value in payload.items() if key in fields}
    )


def _context_item_order(item: ContextItem) -> tuple[int, int, str]:
    return CONTEXT_LAYER_INDEX[item.layer], item.sequence, item.id


def _require_unique_item_ids(items: Sequence[ContextItem]) -> None:
    ids = [item.id for item in items]
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        raise ValueError(f"Context Item IDs must be unique: {duplicates}")


def _request_payload_layer(
    payload: Mapping[str, object],
) -> tuple[ContextLayer, ContextItemKind]:
    item_type = str(payload.get("type") or "").lower()
    role = str(payload.get("role") or "").lower()
    if item_type in {"tool_result", "function_call_output"} or role == "tool":
        kind = (
            ContextItemKind.DUPLICATE_TOOL_RESULT
            if bool(payload.get("duplicate"))
            else ContextItemKind.TOOL_PREVIEW
        )
        return ContextLayer.RELEVANT_TOOL_RESULTS, kind
    if item_type == "subagent_result":
        return ContextLayer.SUBAGENT_RESULTS, ContextItemKind.SUBAGENT_RESULT
    if item_type in {"memory", "retrieved_memory"}:
        return ContextLayer.RETRIEVED_MEMORY, ContextItemKind.RETRIEVED_MEMORY
    if item_type in {"working_memory", "working_memory_snapshot"}:
        return ContextLayer.WORKING_MEMORY, ContextItemKind.GENERAL
    if item_type in {"checkpoint", "context_checkpoint"}:
        return ContextLayer.LATEST_CHECKPOINT, ContextItemKind.CHECKPOINT
    if role == "assistant":
        return ContextLayer.RECENT_CONVERSATION, ContextItemKind.ASSISTANT_DETAIL
    if role == "user":
        return ContextLayer.RECENT_CONVERSATION, ContextItemKind.CHITCHAT
    return ContextLayer.RECENT_CONVERSATION, ContextItemKind.GENERAL


def _visible_payload(item: ContextItem) -> object:
    if item.layer in _SYSTEM_LAYERS:
        return f"[{item.layer.value}]\n{_render_for_count(item.content)}"
    if item.layer is ContextLayer.DYNAMIC_TOOL_SCHEMAS:
        return item.content
    return _render_input_item(item)


def _render_input_item(item: ContextItem) -> dict[str, object]:
    if isinstance(item.content, dict) and "role" in item.content and "content" in item.content:
        rendered = dict(item.content)
        if item.source_refs:
            rendered["source_refs"] = item.source_refs
        return rendered
    return {
        "type": item.layer.value,
        "content": item.content,
        "source_refs": item.source_refs,
        "context_item_id": item.id,
    }


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
    if (
        item_type in {"tool_result", "function_call_output", "relevant_tool_results"}
        or role == "tool"
    ):
        return ContextCategory.TOOL_RESULTS
    if item_type in {"subagent_result", "subagent_results"}:
        return ContextCategory.SUBAGENT_RESULTS
    if item_type in {"memory", "retrieved_memory"}:
        return ContextCategory.RETRIEVED_MEMORY
    if item_type in {
        "working_memory",
        "working_memory_snapshot",
        "latest_checkpoint",
    }:
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
