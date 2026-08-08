from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from riftx.context import (
    AttemptRecord,
    AttemptStatus,
    ContextApplicationService,
    ContextCategory,
    ContextCompilation,
    ContextCompiler,
    ContextItem,
    DuplicateAttemptError,
    OutputStream,
    ProcessedToolResult,
    RawArtifactReference,
    StableInstructionSource,
    StreamPreview,
    TokenBudgeter,
    WorkingMemory,
    WorkingMemoryContextSource,
    WorkingMemoryReducer,
    processed_tool_result_context_item,
)
from riftx.domain import ExecutionStatus
from riftx.runtime.lifecycle import ContextCompileRequest
from riftx.tools import RESIDENT_TOOL_IDS


class MutableToolResultSource:
    def __init__(self) -> None:
        self.items: list[ContextItem] = []

    async def load(self, request: ContextCompileRequest) -> list[ContextItem]:
        del request
        return [item.model_copy(deep=True) for item in self.items]


class MemoryRepository:
    def __init__(self, memory: WorkingMemory) -> None:
        self.memory = memory

    async def get_for_run(self, run_id: str) -> WorkingMemory | None:
        return self.memory if self.memory.run_id == run_id else None


class CompilationRepository:
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


class EightyToolContext:
    async def visibility(self, **_: object) -> SimpleNamespace:
        residents = list(RESIDENT_TOOL_IDS)
        hidden = [f"catalog-tool-{index:02d}" for index in range(80 - len(residents))]
        return SimpleNamespace(
            available_tools=[
                {
                    "name": tool_id,
                    "description": f"Resident control tool {tool_id}",
                    "parameters": {"type": "object", "properties": {}},
                }
                for tool_id in residents
            ],
            always_visible_tools=residents,
            dynamically_loaded_tools=[],
            manifest=lambda: {
                "always_visible_tools": residents,
                "dynamically_loaded_tools": [],
                "hidden_available_tools": hidden,
                "hidden_unavailable_tools": [],
                "catalog_tool_count": 80,
            },
        )


def _write_instruction(root: Path, marker: str) -> Path:
    path = root / ".riftx" / "RIFTX.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(marker, encoding="utf-8")
    return path


def _processed_result(index: int, raw_marker: str) -> ProcessedToolResult:
    uri = f"artifact://runs/run-1/executions/execution-{index}/stdout"
    return ProcessedToolResult(
        execution_id=f"execution-{index}",
        status=ExecutionStatus.COMPLETED,
        tool_id="run_shell",
        exit_code=0,
        parser="shell_result",
        raw_artifacts=[
            RawArtifactReference(
                uri=uri,
                stream=OutputStream.STDOUT,
                mime_type="text/plain",
                size=1_000_000,
            )
        ],
        context_summary=f"Tool call {index} completed; bounded evidence is available at {uri}.",
        previews=[
            StreamPreview(
                stream=OutputStream.STDOUT,
                size=1_000_000,
                mime_type="text/plain",
                text=raw_marker,
                truncated=True,
            )
        ],
    )


def test_processed_tool_result_exposes_opaque_artifact_ids() -> None:
    result = _processed_result(1, "bounded")
    reference = result.raw_artifacts[0].model_copy(update={"artifact_id": "artifact-1"})

    item = processed_tool_result_context_item(
        result.model_copy(update={"raw_artifacts": [reference]})
    )

    assert isinstance(item.content, dict)
    assert item.content["artifact_ids"] == {reference.uri: "artifact-1"}
    assert item.content["artifact_refs"] == [reference.uri]


async def test_wave_c_thirty_tool_call_context_gate(tmp_path: Path) -> None:
    config = tmp_path / "config"
    engagement = tmp_path / "engagement"
    workspace = engagement / "workspace"
    current = workspace / "target"
    global_path = config / "riftx" / "RIFTX.md"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("GLOBAL-SAFETY", encoding="utf-8")
    _write_instruction(engagement, "ENGAGEMENT-SCOPE")
    _write_instruction(workspace, "WORKSPACE-WORKFLOW")
    _write_instruction(current, "CURRENT-PATH-RULE")

    failed = AttemptRecord(
        id="attempt-1",
        action_signature="scan-directories:v1",
        target="https://192.0.2.10",
        tool_id="run_shell",
        normalized_arguments={"wordlist": "large.txt"},
        result_status=AttemptStatus.FAILED,
        result_summary="The high-noise scan failed and must not be repeated.",
        retryable=False,
    )
    memory = WorkingMemory(run_id="run-1", attempts=[failed])
    results = MutableToolResultSource()
    compilations = CompilationRepository()
    compiler = ContextCompiler(
        stable_instruction_source=StableInstructionSource(
            environment={"XDG_CONFIG_HOME": str(config)},
            max_tokens=512,
        ),
        sources=[WorkingMemoryContextSource(MemoryRepository(memory)), results],
        budgeter=TokenBudgeter(max_input_tokens=8_000),
        tool_context=EightyToolContext(),  # type: ignore[arg-type]
        context_service=ContextApplicationService(compilations),
    )
    raw_markers: list[str] = []
    compiled_calls = []

    for index in range(30):
        marker = f"RAW-TOOL-OUTPUT-MUST-NOT-ENTER-MODEL-{index}"
        raw_markers.append(marker)
        result = _processed_result(index, marker)
        results.items.append(processed_tool_result_context_item(result, sequence=index + 1))
        compiled = await compiler.compile(
            ContextCompileRequest(
                run_id="run-1",
                session_id="session-1",
                agent_id="primary",
                model_profile="gate-model",
                objective="Inspect the authorized target without leaving scope",
                run_contract={
                    "objective": "Inspect the authorized target without leaving scope",
                    "scope": {"ips": ["192.0.2.10"], "exclusions": ["192.0.2.11"]},
                },
                engagement_path=str(engagement),
                workspace_path=str(workspace),
                current_path=str(current),
                input_text=f"Continue after Tool Call {index + 1}",
            )
        )
        compiled_calls.append(compiled)

    assert len(compiled_calls) == 30
    assert len(compilations.items) == 30
    for compiled in compiled_calls:
        rendered = json.dumps(compiled.model_dump(mode="json"), ensure_ascii=False)
        assert not any(marker in rendered for marker in raw_markers)
        assert "Inspect the authorized target without leaving scope" in rendered
        assert "192.0.2.10" in rendered
        assert "scan-directories:v1" in rendered
        assert compiled.compilation_id is not None
        assert compiled.token_estimate <= 8_000
        assert len(compiled.available_tools) == len(RESIDENT_TOOL_IDS)
        assert compiled.context_manifest["catalog_tool_count"] == 80
        assert compiled.context_manifest["categories"][ContextCategory.TOOL_SCHEMAS.value][
            "item_count"
        ] == len(RESIDENT_TOOL_IDS)
    assert len(compiled_calls[-1].context_manifest["instruction_paths"]) == 4
    selected_ids = compiled_calls[-1].context_manifest["selected_context_item_ids"]
    non_tool_schema_ids = [
        item_id for item_id in selected_ids if not item_id.startswith("tool-schema:")
    ]
    assert len(non_tool_schema_ids) < 22

    with pytest.raises(DuplicateAttemptError, match="already failed"):
        WorkingMemoryReducer().reduce(
            memory,
            expected_version=memory.version,
            attempts=[failed.model_copy(update={"id": "attempt-duplicate"})],
        )
