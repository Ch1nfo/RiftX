from __future__ import annotations

from types import SimpleNamespace

import pytest

from riftx.context import (
    CONTEXT_LAYER_ORDER,
    ContextApplicationService,
    ContextCategory,
    ContextCompilation,
    ContextCompiler,
    ContextItem,
    ContextItemKind,
    ContextLayer,
    RequiredContextOverflowError,
    StaticContextSource,
    TokenBudgeter,
)
from riftx.runtime.lifecycle import ContextCompileRequest


def request(*, input_text: str = "Inspect the authorized target") -> ContextCompileRequest:
    return ContextCompileRequest(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        model_profile="test-model",
        objective="Inspect the authorized target",
        run_contract={
            "objective": "Inspect the authorized target",
            "scope": {"ips": ["192.0.2.10"], "exclusions": []},
            "approval_mode": "balanced",
        },
        input_text=input_text,
    )


def layered_item(
    item_id: str,
    layer: ContextLayer,
    *,
    content: object | None = None,
    kind: ContextItemKind = ContextItemKind.GENERAL,
    priority: int = 60,
    required: bool = False,
    compressible: bool = True,
    removable: bool = True,
    sequence: int = 1,
) -> ContextItem:
    return ContextItem(
        id=item_id,
        layer=layer,
        kind=kind,
        content=content if content is not None else {"value": item_id},
        priority=priority,
        required=required,
        compressible=compressible,
        removable=removable,
        source_refs=[f"source://{item_id}"],
        sequence=sequence,
    )


async def test_context_compiler_loads_every_layer_in_required_order() -> None:
    source = StaticContextSource(
        [
            layered_item("stable", ContextLayer.STABLE_INSTRUCTIONS),
            layered_item("memory", ContextLayer.WORKING_MEMORY),
            layered_item(
                "checkpoint",
                ContextLayer.LATEST_CHECKPOINT,
                kind=ContextItemKind.CHECKPOINT,
            ),
            layered_item("conversation", ContextLayer.RECENT_CONVERSATION),
            layered_item("tool-result", ContextLayer.RELEVANT_TOOL_RESULTS),
            layered_item("retrieved", ContextLayer.RETRIEVED_MEMORY),
            layered_item("subagent", ContextLayer.SUBAGENT_RESULTS),
            layered_item(
                "tool-schema:test",
                ContextLayer.DYNAMIC_TOOL_SCHEMAS,
                content={"name": "test", "parameters": {"type": "object"}},
                kind=ContextItemKind.TOOL_SCHEMA,
                compressible=False,
            ),
        ]
    )
    compiled = await ContextCompiler(sources=[source]).compile(request())

    assert CONTEXT_LAYER_ORDER == tuple(ContextLayer)
    assert compiled.system_instructions.index("[runtime_contract]") < (
        compiled.system_instructions.index("[stable_instructions]")
    ) < compiled.system_instructions.index("[run_contract]")
    assert [item["type"] for item in compiled.input_items[:-1]] == [
        "working_memory",
        "latest_checkpoint",
        "recent_conversation",
        "relevant_tool_results",
        "retrieved_memory",
        "subagent_results",
    ]
    assert compiled.input_items[-1]["role"] == "user"
    assert compiled.available_tools == [
        {"name": "test", "parameters": {"type": "object"}}
    ]
    assert compiled.checkpoint_id == "checkpoint"


async def test_oversized_tool_result_is_compressed_before_model_input() -> None:
    huge = layered_item(
        "old-tool-preview",
        ContextLayer.RELEVANT_TOOL_RESULTS,
        content="A" * 20_000,
        kind=ContextItemKind.TOOL_PREVIEW,
        priority=10,
    )
    compiler = ContextCompiler(
        sources=[StaticContextSource([huge])],
        budgeter=TokenBudgeter(max_input_tokens=400),
    )

    compiled = await compiler.compile(request(input_text="continue"))

    assert compiled.token_estimate <= 400
    assert "old-tool-preview" in compiled.context_manifest["compressed_context_item_ids"]
    tool_items = [
        item for item in compiled.input_items if item.get("context_item_id") == "old-tool-preview"
    ]
    assert len(tool_items) == 1
    assert "context item compressed" in str(tool_items[0]["content"])
    assert len(str(tool_items[0]["content"])) < 2_000


async def test_excess_tool_schemas_are_budgeted_independently() -> None:
    schemas = [
        layered_item(
            f"tool-schema:tool-{index}",
            ContextLayer.DYNAMIC_TOOL_SCHEMAS,
            content={
                "name": f"tool-{index}",
                "description": "D" * 400,
                "parameters": {"type": "object", "properties": {}},
            },
            kind=ContextItemKind.TOOL_SCHEMA,
            priority=50 + index,
            compressible=False,
            sequence=index,
        )
        for index in range(20)
    ]
    compiled = await ContextCompiler(
        sources=[StaticContextSource(schemas)],
        budgeter=TokenBudgeter(max_input_tokens=1_200),
    ).compile(request(input_text="select a suitable tool"))

    assert 0 < len(compiled.available_tools) < len(schemas)
    assert compiled.token_estimate <= 1_200
    assert any(
        item_id.startswith("tool-schema:")
        for item_id in compiled.context_manifest["dropped_context_item_ids"]
    )
    assert compiled.context_manifest["categories"]["tool_schemas"]["item_count"] == len(
        compiled.available_tools
    )


async def test_required_context_overflow_is_explicit_and_never_silently_drops_scope() -> None:
    required = layered_item(
        "pending-approval",
        ContextLayer.WORKING_MEMORY,
        content={"command": "X" * 4_000},
        kind=ContextItemKind.PENDING_APPROVAL,
        priority=100,
        required=True,
        compressible=False,
        removable=False,
    )
    compiler = ContextCompiler(
        sources=[StaticContextSource([required])],
        budgeter=TokenBudgeter(max_input_tokens=200),
    )

    with pytest.raises(RequiredContextOverflowError) as captured:
        await compiler.compile(request(input_text="approve?"))

    assert "pending-approval" in captured.value.item_ids
    assert "run-contract:run-1" in captured.value.item_ids
    assert captured.value.required_tokens > captured.value.budget


class FakeToolContext:
    def visibility(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            available_tools=[
                {"name": "search_tools", "parameters": {"type": "object"}},
                {"name": "nmap", "parameters": {"type": "object"}},
            ],
            always_visible_tools=["search_tools"],
            dynamically_loaded_tools=["nmap"],
            manifest=lambda: {
                "always_visible_tools": ["search_tools"],
                "dynamically_loaded_tools": ["nmap"],
                "hidden_available_tools": ["nuclei"],
            },
        )


async def test_dynamic_tool_schema_flows_through_the_same_compiler() -> None:
    compiler = ContextCompiler(tool_context=FakeToolContext())  # type: ignore[arg-type]

    compiled = await compiler.compile(request())

    assert [schema["name"] for schema in compiled.available_tools] == ["search_tools", "nmap"]
    assert compiled.context_manifest["always_visible_tools"] == ["search_tools"]
    assert compiled.context_manifest["dynamically_loaded_tools"] == ["nmap"]
    assert compiled.context_manifest["categories"]["tool_schemas"]["item_count"] == 2


async def test_manifest_exactly_accounts_for_selected_model_visible_items() -> None:
    source = StaticContextSource(
        [
            layered_item("stable", ContextLayer.STABLE_INSTRUCTIONS),
            layered_item("fact", ContextLayer.WORKING_MEMORY),
            layered_item("tool", ContextLayer.RELEVANT_TOOL_RESULTS),
            layered_item(
                "schema",
                ContextLayer.DYNAMIC_TOOL_SCHEMAS,
                content={"name": "inspect", "parameters": {"type": "object"}},
                kind=ContextItemKind.TOOL_SCHEMA,
                compressible=False,
            ),
        ]
    )
    compiled = await ContextCompiler(sources=[source]).compile(request())
    manifest = compiled.context_manifest
    categories = manifest["categories"]

    selected_count = len(manifest["selected_context_item_ids"])
    manifested_count = sum(category["item_count"] for category in categories.values())
    token_total = sum(category["estimated_tokens"] for category in categories.values())
    assert selected_count == manifested_count
    assert token_total == compiled.token_estimate == manifest["estimated_tokens"]
    assert sum(manifest["context_item_tokens"].values()) == compiled.token_estimate
    assert categories[ContextCategory.RUN_CONTRACT.value]["item_count"] == 1
    assert categories[ContextCategory.CONVERSATION.value]["item_count"] == 1
    assert categories[ContextCategory.TOOL_SCHEMAS.value]["item_count"] == len(
        compiled.available_tools
    )


class InMemoryCompilationRepository:
    def __init__(self) -> None:
        self.items: dict[str, ContextCompilation] = {}

    async def create(self, compilation: ContextCompilation) -> ContextCompilation:
        self.items[compilation.id] = compilation
        return compilation

    async def get(self, compilation_id: str) -> ContextCompilation | None:
        return self.items.get(compilation_id)

    async def latest_for_session(self, session_id: str) -> ContextCompilation | None:
        return next(
            (item for item in reversed(self.items.values()) if item.session_id == session_id),
            None,
        )

    async def latest_for_run(self, run_id: str) -> ContextCompilation | None:
        return next(
            (item for item in reversed(self.items.values()) if item.run_id == run_id),
            None,
        )

    async def update_usage(
        self,
        compilation_id: str,
        *,
        actual_input_tokens: int | None,
        actual_output_tokens: int | None,
    ) -> ContextCompilation:
        current = self.items[compilation_id]
        updated = current.model_copy(
            update={
                "actual_input_tokens": actual_input_tokens,
                "actual_output_tokens": actual_output_tokens,
            }
        )
        self.items[compilation_id] = updated
        return updated


async def test_canonical_compiler_persists_the_exact_final_manifest() -> None:
    repository = InMemoryCompilationRepository()
    compiler = ContextCompiler(
        sources=[
            StaticContextSource(
                [layered_item("fact", ContextLayer.WORKING_MEMORY)]
            )
        ],
        context_service=ContextApplicationService(repository),
    )

    compiled = await compiler.compile(request())

    assert compiled.compilation_id is not None
    persisted = await repository.get(compiled.compilation_id)
    assert persisted is not None
    assert persisted.estimated_tokens == compiled.token_estimate
    assert persisted.manifest.model_dump(mode="json") == {
        key: value
        for key, value in compiled.context_manifest.items()
        if key
        in {
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
    }
